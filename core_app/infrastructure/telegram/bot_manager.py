import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.types import ChatAdminRights

from core_app.core.shared_state import SharedState
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.telegram.bot_repository import BotRepository
from core_app.infrastructure.telegram.botfather_client import BotFatherClient

logger = logging.getLogger(__name__)


class BotManager:
    """Orchestrates worker bot persistence, @BotFather provisioning, recovery, and pool startup."""

    def __init__(self, db: DatabaseConnection = None):
        self.db = db if db else DatabaseConnection()
        self._repo = BotRepository(self.db)
        self._botfather = BotFatherClient()

    @property
    def _cached_mybots(self):
        return self._botfather._cached_mybots

    @_cached_mybots.setter
    def _cached_mybots(self, value) -> None:
        self._botfather._cached_mybots = value

    # --- Repository Delegation ---

    def get_all_bots(self) -> List[Dict]:
        """Retrieve all registered worker bots from the database."""
        return self._repo.get_all_bots()

    def add_bot(self, bot_id: int, token: str, username: str) -> bool:
        """Register a newly provisioned bot into the database."""
        return self._repo.add_bot(bot_id, token, username)

    def mark_bot_joined(self, bot_id: int) -> bool:
        """Mark a bot as successfully joined to TDrive storage groups."""
        return self._repo.mark_bot_joined(bot_id)

    def remove_bot(self, bot_id: int) -> bool:
        """Remove a bot from the database."""
        return self._repo.remove_bot(bot_id)

    def update_bot_token(self, bot_id: int, new_token: str) -> bool:
        """Update the token for an existing bot in the database."""
        return self._repo.update_bot_token(bot_id, new_token)

    # --- BotFather Delegation ---

    async def _query_mybots_list(self, shared_state: SharedState) -> Tuple[Optional[int], List[Dict]]:
        return await self._botfather.query_mybots_list(shared_state)

    def _invalidate_mybots_cache(self) -> None:
        self._botfather.invalidate_mybots_cache()

    async def _click_bot_button(self, shared_state: SharedState, bf_msg_id: int, callback_data: bytes) -> Optional[int]:
        return await self._botfather.click_bot_button(shared_state, bf_msg_id, callback_data)

    async def _get_token_and_navigate_back(self, shared_state: SharedState, options_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        return await self._botfather.get_token_and_navigate_back(shared_state, options_msg_id)

    async def _revoke_token_and_navigate_back(self, shared_state: SharedState, token_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        return await self._botfather.revoke_token_and_navigate_back(shared_state, token_msg_id)

    @staticmethod
    async def _safe_disconnect(bot_client: Optional[TelegramClient]) -> None:
        """Disconnect a TelegramClient instance safely, logging any teardown error."""
        if not bot_client:
            return
        try:
            if bot_client.is_connected():
                await bot_client.disconnect()
        except Exception as e:
            logger.debug(f"Failed to disconnect worker bot cleanly: {e}")

    async def _start_worker_client(
        self,
        shared_state: SharedState,
        username: str,
        token: str,
        group_joined: int = 1,
        require_get_me: bool = False,
        log_prefix: str = "[init]",
    ) -> Tuple[bool, bool]:
        """Connect, authorize, and append a worker TelegramClient to shared_state.clients_pool.

        Returns (added, token_expired).
        """
        bot_client = None
        added = False
        try:
            bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
            t_boot = time.time()
            await asyncio.wait_for(bot_client.start(bot_token=token), timeout=30.0)
            logger.info(f"{log_prefix} Bot {username} started in {time.time() - t_boot:.1f}s, connected={bot_client.is_connected()}")

            if not await bot_client.is_user_authorized():
                logger.warning(f"{log_prefix} Bot {username} not authorized. Token may be revoked.")
                await self._safe_disconnect(bot_client)
                return False, False

            if group_joined != 1:
                logger.warning(f"{log_prefix} Bot {username} authorized but group_joined=0. Skipping.")
                await self._safe_disconnect(bot_client)
                return False, False

            if require_get_me:
                me = await bot_client.get_me()
                logger.info(f"{log_prefix} Bot {username} authorized as @{me.username}")

            bot_client.tdrive_worker_name = username
            if not hasattr(shared_state, 'clients_pool'):
                shared_state.clients_pool = []
            shared_state.clients_pool.append(bot_client)
            added = True
            return True, False
        except asyncio.CancelledError:
            if not added:
                await self._safe_disconnect(bot_client)
            raise
        except asyncio.TimeoutError:
            logger.error(f"{log_prefix} TIMEOUT starting bot {username} (30s). Skipping.")
            await self._safe_disconnect(bot_client)
            return False, False
        except Exception as e:
            await self._safe_disconnect(bot_client)
            if 'expired' in str(e).lower():
                return False, True
            logger.error(f"{log_prefix} Failed to start bot @{username}: {e}")
            return False, False

    async def _refresh_and_retry_expired_bot(
        self, shared_state: SharedState, bot_id: int, username: str, group_joined: int
    ) -> bool:
        """Query @BotFather /mybots for a fresh token and retry starting an expired worker bot."""
        logger.warning(f"[init] Token expired for @{username}. Attempting refresh via /mybots...")
        bf_msg_id, bf_bots = await self._query_mybots_list(shared_state)
        bf_bot = next((b for b in bf_bots if b['username'] == username), None) if bf_bots else None
        if not bf_bot:
            logger.error(f"[init] @{username} not found in BotFather /mybots list.")
            return False

        options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bf_bot['callback_data'])
        if not options_msg_id:
            logger.error(f"[init] Could not open options for @{username} in /mybots.")
            return False

        new_token, _ = await self._get_token_and_navigate_back(shared_state, options_msg_id)
        if not new_token:
            logger.error(f"[init] Refresh failed for @{username}. Could not get new token.")
            return False

        self.update_bot_token(bot_id, new_token)
        logger.info(f"[init] Retrying @{username} with refreshed token...")
        added, _ = await self._start_worker_client(
            shared_state, username, new_token, group_joined=group_joined, require_get_me=False, log_prefix="[init]"
        )
        if added:
            logger.info(f"[init] Bot @{username} loaded with refreshed token")
        return added

    async def init_saved_bots(self, shared_state: SharedState) -> None:
        """Initialize all registered bots from the database and connect them."""
        bots = self.get_all_bots()
        if not bots:
            logger.info("[init] No saved worker bots found in DB.")
            return

        logger.info(f"[init] Loading {len(bots)} saved worker bots from DB...")
        loaded_count = 0
        for bot_info in bots:
            username = bot_info.get('username', '?')
            bot_id = bot_info.get('bot_id', '?')
            group_joined = bot_info.get('group_joined', 0)
            logger.info(f"[init] Bot {username} (id={bot_id}): group_joined={group_joined}")

            added, token_expired = await self._start_worker_client(
                shared_state, username, bot_info['token'],
                group_joined=group_joined, require_get_me=True, log_prefix="[init]"
            )
            if added:
                loaded_count += 1
            elif token_expired and await self._refresh_and_retry_expired_bot(shared_state, bot_id, username, group_joined):
                loaded_count += 1

        logger.info(f"[init] Done: {loaded_count}/{len(bots)} bots loaded into pool")

    async def recover_lost_bots(self, shared_state: SharedState, target_count: int = 5) -> None:
        """Sync DB with @BotFather /mybots and boot missing or stale worker bots."""
        del target_count
        client = shared_state.client
        if not client or not client.is_connected():
            logger.warning("[recover] Main client not connected. Skipping bot recovery.")
            return

        self.db.sync_manager.set_busy(True)
        try:
            logger.info("[recover] Querying /mybots from BotFather...")
            bf_msg_id, bf_bots = await self._query_mybots_list(shared_state)
            if not bf_bots:
                logger.warning("[recover] No bots found in BotFather. Cannot recover.")
                return

            bf_usernames = {b['username'] for b in bf_bots}
            current_bots = self.get_all_bots()
            db_usernames = {b['username'] for b in current_bots}

            deleted = db_usernames - bf_usernames
            for username in deleted:
                bot_info = next((b for b in current_bots if b['username'] == username), None)
                if bot_info:
                    logger.warning(f"[recover] @{username} in DB but not in BotFather. Removing from DB.")
                    self.remove_bot(bot_info['bot_id'])

            for username in bf_usernames - db_usernames:
                bf_msg_id = await self._recover_missing_bot(shared_state, bf_msg_id, bf_bots, username)

            for bot_info in current_bots:
                username = bot_info['username']
                if username in deleted:
                    continue
                if any(getattr(c, 'tdrive_worker_name', '') == username for c in shared_state.clients_pool):
                    logger.debug(f"[recover] @{username} already in pool. Skipping.")
                    continue
                bf_msg_id = await self._refresh_stale_bot(shared_state, bf_msg_id, bf_bots, bot_info)

            active_count = len([b for b in shared_state.clients_pool if b.is_connected()])
            logger.info(f"[recover] Final: {active_count} active bots in pool")
        except Exception as e:
            logger.error(f"[recover] Error recovering lost bots: {e}", exc_info=True)
        finally:
            self.db.sync_manager.set_busy(False)

    async def _recover_missing_bot(
        self, shared_state: SharedState, bf_msg_id: Optional[int], bf_bots: List[Dict], username: str
    ) -> Optional[int]:
        """Fetch token for a BotFather bot absent from DB, save it, and boot/invite it."""
        bot_data = next((b for b in bf_bots if b['username'] == username), None)
        if not bot_data:
            return bf_msg_id
        logger.info(f"[recover] @{username} found in BotFather but not in DB. Getting token...")
        options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bot_data['callback_data'])
        if not options_msg_id:
            logger.error(f"[recover] Could not open options for @{username}")
            return bf_msg_id

        token, next_bf_msg_id = await self._get_token_and_navigate_back(shared_state, options_msg_id)
        if not token:
            logger.error(f"[recover] Could not get token for @{username}")
            return next_bf_msg_id

        bot_id = int(token.split(':')[0])
        if self.add_bot(bot_id, token, username):
            logger.info(f"[recover] Added @{username} (bot_id={bot_id}) to DB")
            await self._boot_and_invite_bot(shared_state, username, token)
        return next_bf_msg_id

    async def _refresh_stale_bot(
        self, shared_state: SharedState, bf_msg_id: Optional[int], bf_bots: List[Dict], bot_info: Dict
    ) -> Optional[int]:
        """Refresh token for a saved bot not currently active in clients_pool and boot/invite it."""
        username = bot_info['username']
        logger.info(f"[recover] Verifying @{username} (token may be expired)...")
        bf_bot = next((b for b in bf_bots if b['username'] == username), None)
        if not bf_bot:
            return bf_msg_id

        options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bf_bot['callback_data'])
        if not options_msg_id:
            return bf_msg_id

        token, next_bf_msg_id = await self._get_token_and_navigate_back(shared_state, options_msg_id)
        if not token:
            return next_bf_msg_id

        if token != bot_info['token']:
            logger.info(f"[recover] Token changed for @{username}. Updating DB.")
            self.update_bot_token(bot_info['bot_id'], token)
        await self._boot_and_invite_bot(shared_state, username, token)
        return next_bf_msg_id

    async def _boot_and_invite_bot(self, shared_state: SharedState, username: str, token: str) -> bool:
        """Boot a bot client, invite/promote it to storage groups, and append to clients_pool."""
        bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
        try:
            logger.info(f"[recover] Booting @{username}...")
            t_boot = time.time()
            await asyncio.wait_for(bot_client.start(bot_token=token), timeout=30.0)
            logger.info(f"[recover] @{username} booted in {time.time() - t_boot:.1f}s, connected={bot_client.is_connected()}")
            bot_client.tdrive_worker_name = username

            t_invite = time.time()
            success = await asyncio.wait_for(self._invite_and_promote_bot(shared_state, username), timeout=30.0)
            logger.info(f"[recover] Invite result for @{username}: success={success}, elapsed={time.time() - t_invite:.1f}s")

            if success:
                shared_state.clients_pool.append(bot_client)
                bot_info = next((b for b in self.get_all_bots() if b['username'] == username), None)
                if bot_info:
                    self.mark_bot_joined(bot_info['bot_id'])
                logger.info(f"[recover] Hot-plugged @{username} into pool. Pool size={len(shared_state.clients_pool)}")
                return True

            logger.warning(f"[recover] @{username} invite returned False. Disconnecting.")
            await bot_client.disconnect()
            return False
        except asyncio.CancelledError:
            if bot_client not in getattr(shared_state, 'clients_pool', []):
                await self._safe_disconnect(bot_client)
            raise
        except asyncio.TimeoutError:
            logger.error(f"[recover] TIMEOUT booting @{username} (30s).")
            await self._safe_disconnect(bot_client)
            return False
        except Exception as e:
            logger.error(f"[recover] Error booting @{username}: {e}", exc_info=True)
            await self._safe_disconnect(bot_client)
            return False

    async def provision_missing_bots(self, shared_state: SharedState, target_count: int = 5) -> None:
        """Provision missing worker bots via @BotFather up to target_count."""
        self.db.sync_manager.set_busy(True)
        try:
            current_bots = self.get_all_bots()
            needed = target_count - len(current_bots)
            logger.info(f"[provision] DB has {len(current_bots)} bots, target={target_count}, needed={needed}")
            if needed <= 0:
                logger.info("[provision] Bot Token pool is full. No provisioning needed.")
                return
            if not shared_state.client or not shared_state.client.is_connected():
                logger.warning("[provision] Main client not connected. Cannot provision bots.")
                return

            logger.info(f"[provision] Starting auto-provision of {needed} worker bots...")
            for i in range(needed):
                logger.info(f"[provision] Bot {i+1}/{needed}: starting BotFather conversation...")
                try:
                    result = await self._botfather.create_new_bot(shared_state)
                    status = result["status"]
                    if status == "rate_limited":
                        wait_time = result["wait_seconds"]
                        logger.info(f"[provision] Rate limited. Sleeping {wait_time}s...")
                        await asyncio.sleep(wait_time + 1)
                        continue
                    if status == "limit_reached":
                        if shared_state.connection_emitter:
                            shared_state.connection_emitter('bot_limit_reached')
                        break
                    if status != "created":
                        continue

                    bot_id, token, username = result["bot_id"], result["token"], result["username"]
                    self.add_bot(bot_id, token, username)
                    logger.info(f"[provision] Created and registered bot: {username} (bot_id={bot_id})")

                    if await self._invite_and_promote_bot(shared_state, username):
                        self.mark_bot_joined(bot_id)
                        await self._start_worker_client(
                            shared_state, username, token,
                            group_joined=1, require_get_me=False, log_prefix="[provision]"
                        )
                    else:
                        logger.error(f"[provision] Failed to invite {username}. Not added to pool.")
                except asyncio.TimeoutError:
                    logger.error("[provision] Timeout waiting for BotFather response.")
                    break
                except Exception as e:
                    logger.error(f"[provision] Error during provisioning: {e}", exc_info=True)
                    break
        except Exception as overall_e:
            logger.error(f"[provision] Error: {overall_e}")
        finally:
            self.db.sync_manager.set_busy(False)

    def _get_target_group_ids(self, shared_state: SharedState) -> List[int]:
        """Collect all registered storage group IDs across shared_state and database."""
        group_ids = set()
        if hasattr(shared_state, 'group_id_map') and shared_state.group_id_map:
            for tg_id in shared_state.group_id_map.values():
                if tg_id:
                    group_ids.add(tg_id)
        if hasattr(shared_state, 'group_id') and shared_state.group_id:
            group_ids.add(shared_state.group_id)
        group_ids.update(self._repo.get_storage_group_ids())
        return list(group_ids)

    async def _invite_and_promote_in_group(self, client, bot_entity, username: str, group_id: int) -> bool:
        """Invite a resolved bot entity to one storage group and grant admin rights."""
        from telethon.errors.rpcerrorlist import FloodWaitError, UserAlreadyParticipantError

        rights = ChatAdminRights(
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            manage_call=False,
            invite_users=False,
            ban_users=False,
            pin_messages=False,
            add_admins=False,
        )

        logger.info(f"[_invite] Inviting {username} to group {group_id}...")
        t_rpc = time.time()
        try:
            await client(InviteToChannelRequest(channel=group_id, users=[bot_entity]))
            logger.info(f"[_invite] Invite OK in {time.time() - t_rpc:.1f}s for group {group_id}")
        except UserAlreadyParticipantError:
            logger.info(f"[_invite] Already participant in group {group_id} ({time.time() - t_rpc:.1f}s)")
        except FloodWaitError as e:
            logger.warning(f"[_invite] FloodWait on invite group {group_id}: wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"[_invite] Invite FAILED in {time.time() - t_rpc:.1f}s for group {group_id}: {e}")

        logger.info(f"[_invite] Promoting {username} in group {group_id}...")
        t_rpc = time.time()
        try:
            await client(EditAdminRequest(channel=group_id, user_id=bot_entity, admin_rights=rights, rank="Worker Node"))
            logger.info(f"[_invite] Promote OK in {time.time() - t_rpc:.1f}s for group {group_id}")
            return True
        except FloodWaitError as e:
            logger.warning(f"[_invite] FloodWait on promote group {group_id}: wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"[_invite] Promote FAILED in {time.time() - t_rpc:.1f}s for group {group_id}: {e}")
        return False

    async def invite_and_promote_bot_to_group(self, shared_state: SharedState, username: str, group_id: int) -> bool:
        """Invite and promote a worker bot to a single specified storage group."""
        try:
            client = shared_state.client
            if not client:
                logger.error(f"[_invite] No client available for {username}.")
                return False
            bot_entity = await client.get_input_entity(username)
            return await self._invite_and_promote_in_group(client, bot_entity, username, group_id)
        except Exception as e:
            logger.error(f"[_invite] Exception inviting {username} to group {group_id}: {e}", exc_info=True)
            return False

    async def _invite_and_promote_bot(self, shared_state: SharedState, username: str) -> bool:
        """Invite a bot to all registered TDrive storage groups and grant admin rights."""
        t_start = time.time()
        try:
            target_group_ids = self._get_target_group_ids(shared_state)
            logger.info(f"[_invite] Start: {username}, target groups={target_group_ids}")
            if not target_group_ids:
                logger.warning(f"[_invite] No storage groups found for {username}.")
                return False

            client = shared_state.client
            if not client:
                logger.error(f"[_invite] No client available for {username}.")
                return False

            t_rpc = time.time()
            try:
                bot_entity = await client.get_input_entity(username)
                logger.info(f"[_invite] Entity resolved in {time.time() - t_rpc:.1f}s: {bot_entity}")
            except Exception as e:
                logger.error(f"[_invite] Failed to resolve entity for {username} in {time.time() - t_rpc:.1f}s: {e}")
                return False

            overall_success = False
            for group_id in target_group_ids:
                if await self._invite_and_promote_in_group(client, bot_entity, username, group_id):
                    overall_success = True

            logger.info(f"[_invite] Done: {username}, overall_success={overall_success}, elapsed={time.time() - t_start:.1f}s")
            return overall_success
        except Exception as e:
            logger.error(f"[_invite] Exception for {username}: {e}", exc_info=True)
            return False
