import asyncio
import re
import random
import string
import time
from telethon import TelegramClient
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.types import ChatAdminRights
from telethon.sessions import StringSession
import logging
from typing import List, Dict, Optional, Tuple
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)

class BotManager:
    """
    Manages the lifecycle, provisioning, and storage of Worker Bots for the Token Pool.
    """
    def __init__(self, db: DatabaseConnection = None):
        self.db = db if db else DatabaseConnection()
        self._cached_mybots = None  # (msg_id, [{'username', 'callback_data'}])

    def get_all_bots(self) -> List[Dict]:
        """Retrieves all registered worker bots from the database."""
        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT bot_id, token, username, group_joined FROM worker_bots")
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error retrieving worker bots: {e}")
            return []

    def add_bot(self, bot_id: int, token: str, username: str) -> bool:
        """Registers a newly provisioned bot into the database."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(
                        cursor,
                        "INSERT OR IGNORE INTO worker_bots (bot_id, token, username, group_joined) VALUES (?, ?, ?, ?)",
                        (bot_id, token, username, 0)
                    )
            return True
        except Exception as e:
            logger.error(f"Error adding bot to DB: {e}")
            return False

    def mark_bot_joined(self, bot_id: int) -> bool:
        """Marks a bot as successfully joined to the TDrive group."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "UPDATE worker_bots SET group_joined = 1 WHERE bot_id = ?", (bot_id,))
            return True
        except Exception as e:
            logger.error(f"Error marking bot as joined: {e}")
            return False

    def remove_bot(self, bot_id: int) -> bool:
        """Removes a bot from the database (e.g. if the token was revoked)."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "DELETE FROM worker_bots WHERE bot_id = ?", (bot_id,))
            return True
        except Exception as e:
            logger.error(f"Error removing bot from DB: {e}")
            return False

    def update_bot_token(self, bot_id: int, new_token: str) -> bool:
        """Updates the token for an existing bot in the database."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "UPDATE worker_bots SET token = ? WHERE bot_id = ?", (new_token, bot_id))
            logger.info(f"[db] Updated token for bot_id={bot_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating bot token: {e}")
            return False

    async def _query_mybots_list(self, shared_state: 'SharedState') -> Tuple[Optional[int], List[Dict]]:
        """
        Send /mybots to @BotFather and parse the inline keyboard.
        Uses cache to avoid redundant calls. Call _invalidate_mybots_cache() after revoke.
        Returns: (msg_id, [{'username': str, 'callback_data': bytes}])
        """
        if self._cached_mybots:
            logger.debug("[mybots] Using cached /mybots result.")
            return self._cached_mybots

        client = shared_state.client
        await client.send_message('BotFather', '/mybots')
        await asyncio.sleep(1)

        messages = await client.get_messages('BotFather', limit=1)
        if not messages or not messages[0].reply_markup:
            logger.warning("[mybots] No inline keyboard found in BotFather response.")
            return None, []

        msg = messages[0]
        bots = []
        for row in msg.reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'data') and button.data:
                    bots.append({
                        'username': button.text.lstrip('@'),
                        'callback_data': button.data
                    })

        logger.info(f"[mybots] Found {len(bots)} bots: {[b['username'] for b in bots]}")
        self._cached_mybots = (msg.id, bots)
        return self._cached_mybots

    def _invalidate_mybots_cache(self):
        """Invalidate cached /mybots result (call after revoke or bot creation)."""
        self._cached_mybots = None

    async def _click_bot_button(self, shared_state: 'SharedState', bf_msg_id: int, callback_data: bytes) -> Optional[int]:
        """
        Click a bot name in the /mybots list (Level 0 → Level 1).
        Returns the msg_id of the bot options menu, or None on failure.
        """
        client = shared_state.client
        await client(GetBotCallbackAnswerRequest(
            peer='BotFather', msg_id=bf_msg_id, data=callback_data
        ))
        await asyncio.sleep(0.5)

        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            logger.warning("[mybots] No inline keyboard after clicking bot button.")
            return None

        return resp[0].id

    async def _get_token_and_navigate_back(self, shared_state: 'SharedState', options_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        """
        From bot options menu (Level 1): click API Token → parse token →
        Back to Bot → Back to Bot List → return to Level 0.
        Returns: (token, new_bf_msg_id)
        """
        client = shared_state.client

        # Level 1: find "API Token" button
        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            return None, None

        options_msg = resp[0]
        token_btn_data = None
        for row in options_msg.reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'data') and button.text == 'API Token':
                    token_btn_data = button.data
                    break

        if not token_btn_data:
            logger.warning("[mybots] 'API Token' button not found.")
            return None, None

        # Click "API Token" → Level 2
        await client(GetBotCallbackAnswerRequest(
            peer='BotFather', msg_id=options_msg.id, data=token_btn_data
        ))
        await asyncio.sleep(0.5)

        # Level 2: parse token
        token_resp = await client.get_messages('BotFather', limit=1)
        token = None
        token_msg_id = None
        if token_resp:
            token_msg = token_resp[0]
            token_msg_id = token_msg.id
            text = token_msg.text or ''
            logger.debug("[mybots] Received token response from BotFather (length=%d).", len(text))
            match = re.search(r'(\d{8,10}:[a-zA-Z0-9_-]{35,})', text)
            if match:
                token = match.group(1)
            else:
                logger.warning(f"[mybots] Could not parse token from: {repr(text[:200])}")

        # Click "<< Back to Bot" → back to Level 1
        back_to_bot_data = None
        if token_resp and token_resp[0].reply_markup:
            for row in token_resp[0].reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and 'Back to Bot' in button.text:
                        back_to_bot_data = button.data
                        break

        if back_to_bot_data and token_msg_id:
            await client(GetBotCallbackAnswerRequest(
                peer='BotFather', msg_id=token_msg_id, data=back_to_bot_data
            ))
            await asyncio.sleep(0.5)

        # Click "<< Back to Bot List" → back to Level 0
        options_resp = await client.get_messages('BotFather', limit=1)
        new_bf_msg_id = None
        if options_resp and options_resp[0].reply_markup:
            for row in options_resp[0].reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and 'Back to Bot List' in button.text:
                        await client(GetBotCallbackAnswerRequest(
                            peer='BotFather', msg_id=options_resp[0].id, data=button.data
                        ))
                        await asyncio.sleep(0.5)
                        break

        # Get new Level 0 msg_id
        bf_resp = await client.get_messages('BotFather', limit=1)
        if bf_resp:
            new_bf_msg_id = bf_resp[0].id

        return token, new_bf_msg_id

    async def _revoke_token_and_navigate_back(self, shared_state: 'SharedState', token_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        """
        From token screen (Level 2): click Revoke current token → confirm →
        parse new token → Back to Bot → Back to Bot List → return to Level 0.
        Returns: (new_token, new_bf_msg_id)
        """
        client = shared_state.client

        # Level 2: find "Revoke current token" button
        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            return None, None

        revoke_btn_data = None
        for row in resp[0].reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'data') and 'Revoke' in button.text:
                    revoke_btn_data = button.data
                    break

        if not revoke_btn_data:
            logger.warning("[mybots] 'Revoke current token' button not found.")
            return None, None

        # Click "Revoke current token"
        await client(GetBotCallbackAnswerRequest(
            peer='BotFather', msg_id=resp[0].id, data=revoke_btn_data
        ))
        await asyncio.sleep(0.5)

        # Confirm revoke (BotFather may show a confirmation dialog)
        confirm_resp = await client.get_messages('BotFather', limit=1)
        if confirm_resp and confirm_resp[0].reply_markup:
            for row in confirm_resp[0].reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and ('yes' in button.text.lower() or 'confirm' in button.text.lower() or 'revoke' in button.text.lower()):
                        await client(GetBotCallbackAnswerRequest(
                            peer='BotFather', msg_id=confirm_resp[0].id, data=button.data
                        ))
                        await asyncio.sleep(0.5)
                        break

        # Parse new token
        new_token_resp = await client.get_messages('BotFather', limit=1)
        new_token = None
        new_token_msg_id = None
        if new_token_resp:
            new_token_msg = new_token_resp[0]
            new_token_msg_id = new_token_msg.id
            text = new_token_msg.text or ''
            logger.debug("[mybots] Received revoked token response from BotFather (length=%d).", len(text))
            match = re.search(r'(\d{8,10}:[a-zA-Z0-9_-]{35,})', text)
            if match:
                new_token = match.group(1)
            else:
                logger.warning(f"[mybots] Could not parse new token from: {repr(text[:200])}")

        # Click "<< Back to Bot" → back to Level 1
        back_to_bot_data = None
        if new_token_resp and new_token_resp[0].reply_markup:
            for row in new_token_resp[0].reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and 'Back to Bot' in button.text:
                        back_to_bot_data = button.data
                        break

        if back_to_bot_data and new_token_msg_id:
            await client(GetBotCallbackAnswerRequest(
                peer='BotFather', msg_id=new_token_msg_id, data=back_to_bot_data
            ))
            await asyncio.sleep(0.5)

        # Click "<< Back to Bot List" → back to Level 0
        options_resp = await client.get_messages('BotFather', limit=1)
        new_bf_msg_id = None
        if options_resp and options_resp[0].reply_markup:
            for row in options_resp[0].reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and 'Back to Bot List' in button.text:
                        await client(GetBotCallbackAnswerRequest(
                            peer='BotFather', msg_id=options_resp[0].id, data=button.data
                        ))
                        await asyncio.sleep(0.5)
                        break

        # Get new Level 0 msg_id
        bf_resp = await client.get_messages('BotFather', limit=1)
        if bf_resp:
            new_bf_msg_id = bf_resp[0].id

        # Invalidate cache since bot list/tokens changed
        self._invalidate_mybots_cache()

        return new_token, new_bf_msg_id


    @staticmethod
    async def _safe_disconnect(bot_client: Optional[TelegramClient]) -> None:
        """Disconnect a TelegramClient instance safely, suppressing disconnection errors."""
        if not bot_client:
            return
        try:
            if bot_client.is_connected():
                await bot_client.disconnect()
        except Exception:
            pass

    async def init_saved_bots(self, shared_state: 'SharedState'):
        """Initializes all registered bots from the database and connects them."""
        
        bots = self.get_all_bots()
        if not bots:
            logger.info("[init] No saved worker bots found in DB.")
            return

        logger.info(f"[init] Loading {len(bots)} saved worker bots from DB...")
        loaded_count = 0
        for bot_info in bots:
            bot_client = None
            added_to_pool = False
            try:
                username = bot_info.get('username', '?')
                bot_id = bot_info.get('bot_id', '?')
                group_joined = bot_info.get('group_joined', 0)
                token = bot_info['token']
                
                logger.info(f"[init] Bot {username} (id={bot_id}): group_joined={group_joined}")
                
                bot_client = TelegramClient(
                    StringSession(""), 
                    shared_state.api_id, 
                    shared_state.api_hash
                )
                
                # Start as bot using token (30s timeout)
                t_boot = time.time()
                await asyncio.wait_for(bot_client.start(bot_token=token), timeout=30.0)
                elapsed_boot = time.time() - t_boot
                logger.info(f"[init] Bot {username} started in {elapsed_boot:.1f}s, connected={bot_client.is_connected()}")
                
                # Verify connection and authorization
                if await bot_client.is_user_authorized():
                    if group_joined == 1:
                        try:
                            me = await bot_client.get_me()
                            logger.info(f"[init] Bot {username} authorized as @{me.username}")
                            bot_client.tdrive_worker_name = username
                            if not hasattr(shared_state, 'clients_pool'):
                                shared_state.clients_pool = []
                            shared_state.clients_pool.append(bot_client)
                            added_to_pool = True
                            loaded_count += 1
                        except Exception as e:
                            logger.warning(f"[init] Bot {username} authorized but get_me failed: {e}")
                            await self._safe_disconnect(bot_client)
                    else:
                        logger.warning(f"[init] Bot {username} authorized but group_joined=0. Skipping.")
                        await self._safe_disconnect(bot_client)
                else:
                    logger.warning(f"[init] Bot {username} not authorized. Token may be revoked.")
                    await self._safe_disconnect(bot_client)
            except asyncio.CancelledError:
                if not added_to_pool:
                    await self._safe_disconnect(bot_client)
                raise
            except asyncio.TimeoutError:
                logger.error(f"[init] TIMEOUT starting bot {username} (30s). Skipping.")
                await self._safe_disconnect(bot_client)
            except Exception as e:
                # Token expired — attempt refresh via /mybots
                if 'expired' in str(e).lower():
                    logger.warning(f"[init] Token expired for @{username}. Attempting refresh via /mybots...")
                    await self._safe_disconnect(bot_client)
                    # Query /mybots to get fresh token
                    bf_msg_id, bf_bots = await self._query_mybots_list(shared_state)
                    bf_bot = next((b for b in bf_bots if b['username'] == username), None) if bf_bots else None
                    if bf_bot:
                        options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bf_bot['callback_data'])
                        if options_msg_id:
                            new_token, _ = await self._get_token_and_navigate_back(shared_state, options_msg_id)
                            if new_token:
                                self.update_bot_token(bot_id, new_token)
                                logger.info(f"[init] Retrying @{username} with refreshed token...")
                                bot_client2 = None
                                added_retry = False
                                try:
                                    bot_client2 = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
                                    await asyncio.wait_for(bot_client2.start(bot_token=new_token), timeout=30.0)
                                    if await bot_client2.is_user_authorized():
                                        if group_joined == 1:
                                            bot_client2.tdrive_worker_name = username
                                            if not hasattr(shared_state, 'clients_pool'):
                                                shared_state.clients_pool = []
                                            shared_state.clients_pool.append(bot_client2)
                                            added_retry = True
                                            loaded_count += 1
                                            logger.info(f"[init] Bot @{username} loaded with refreshed token")
                                        else:
                                            await self._safe_disconnect(bot_client2)
                                    else:
                                        await self._safe_disconnect(bot_client2)
                                except asyncio.CancelledError:
                                    if not added_retry:
                                        await self._safe_disconnect(bot_client2)
                                    raise
                                except Exception as retry_e:
                                    logger.error(f"[init] Retry failed for @{username}: {retry_e}")
                                    if not added_retry:
                                        await self._safe_disconnect(bot_client2)
                            else:
                                logger.error(f"[init] Refresh failed for @{username}. Could not get new token.")
                        else:
                            logger.error(f"[init] Could not open options for @{username} in /mybots.")
                    else:
                        logger.error(f"[init] @{username} not found in BotFather /mybots list.")
                else:
                    logger.error(f"[init] Failed to init bot @{username}: {e}")
                    if not added_to_pool:
                        await self._safe_disconnect(bot_client)
        
        logger.info(f"[init] Done: {loaded_count}/{len(bots)} bots loaded into pool")


    async def recover_lost_bots(self, shared_state: 'SharedState', target_count: int = 5):
        """
        Queries @BotFather /mybots to discover actual bots, syncs with DB,
        and boots all valid bots. Replaces the old history-parsing approach.
        """
        client = shared_state.client
        if not client or not client.is_connected():
            logger.warning("[recover] Main client not connected. Skipping bot recovery.")
            return

        self.db.sync_manager.set_busy(True)
        try:
            # Step 1: Query /mybots to get actual bot list from BotFather
            logger.info("[recover] Querying /mybots from BotFather...")
            bf_msg_id, bf_bots = await self._query_mybots_list(shared_state)
            if not bf_bots:
                logger.warning("[recover] No bots found in BotFather. Cannot recover.")
                return

            bf_usernames = {b['username'] for b in bf_bots}
            logger.info(f"[recover] BotFather has {len(bf_bots)} bots: {list(bf_usernames)}")

            # Step 2: Compare with DB
            current_bots = self.get_all_bots()
            db_usernames = {b['username'] for b in current_bots}
            logger.info(f"[recover] DB has {len(current_bots)} bots: {list(db_usernames)}")

            # Bots in DB but not in BotFather → deleted externally
            deleted = db_usernames - bf_usernames
            for username in deleted:
                bot_info = next((b for b in current_bots if b['username'] == username), None)
                if bot_info:
                    logger.warning(f"[recover] @{username} in DB but not in BotFather. Removing from DB.")
                    self.remove_bot(bot_info['bot_id'])

            recovered_count = 0

            # Bots in BotFather but not in DB → missing from DB, need token
            missing = bf_usernames - db_usernames
            for username in missing:
                bot_data = next((b for b in bf_bots if b['username'] == username), None)
                if not bot_data:
                    continue

                logger.info(f"[recover] @{username} found in BotFather but not in DB. Getting token...")
                # Navigate to this bot and get its token
                options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bot_data['callback_data'])
                if not options_msg_id:
                    logger.error(f"[recover] Could not open options for @{username}")
                    continue

                token, bf_msg_id = await self._get_token_and_navigate_back(shared_state, options_msg_id)
                if not token:
                    logger.error(f"[recover] Could not get token for @{username}")
                    continue

                bot_id = int(token.split(':')[0])
                if self.add_bot(bot_id, token, username):
                    logger.info(f"[recover] Added @{username} (bot_id={bot_id}) to DB")
                    # Boot and invite
                    success = await self._boot_and_invite_bot(shared_state, username, token)
                    if success:
                        recovered_count += 1

            # Step 3: Verify existing DB bots (check if tokens still work)
            refreshed_count = 0
            for bot_info in current_bots:
                username = bot_info['username']
                if username in deleted:
                    continue  # Already removed

                # Skip if already in pool from init_saved_bots
                if any(getattr(c, 'tdrive_worker_name', '') == username for c in shared_state.clients_pool):
                    logger.debug(f"[recover] @{username} already in pool. Skipping.")
                    continue

                logger.info(f"[recover] Verifying @{username} (token may be expired)...")
                # Find this bot in BotFather to get fresh token
                bf_bot = next((b for b in bf_bots if b['username'] == username), None)
                if not bf_bot:
                    continue

                options_msg_id = await self._click_bot_button(shared_state, bf_msg_id, bf_bot['callback_data'])
                if not options_msg_id:
                    continue

                token, bf_msg_id = await self._get_token_and_navigate_back(shared_state, options_msg_id)
                if not token:
                    continue

                # Update token in DB if changed
                if token != bot_info['token']:
                    logger.info(f"[recover] Token changed for @{username}. Updating DB.")
                    self.update_bot_token(bot_info['bot_id'], token)

                # Try booting with this token
                success = await self._boot_and_invite_bot(shared_state, username, token)
                if success:
                    refreshed_count += 1

            active_count = len([b for b in shared_state.clients_pool if b.is_connected()])
            logger.info(f"[recover] Final: {active_count} active bots in pool")

        except Exception as e:
            logger.error(f"[recover] Error recovering lost bots: {e}", exc_info=True)
        finally:
            self.db.sync_manager.set_busy(False)

    async def _boot_and_invite_bot(self, shared_state: 'SharedState', username: str, token: str) -> bool:
        """
        Boot a bot with the given token, invite to groups, and add to pool.
        Returns True if successfully hot-plugged.
        """
        bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
        try:
            logger.info(f"[recover] Booting @{username}...")
            t_boot = time.time()
            await asyncio.wait_for(bot_client.start(bot_token=token), timeout=30.0)
            elapsed_boot = time.time() - t_boot
            logger.info(f"[recover] @{username} booted in {elapsed_boot:.1f}s, connected={bot_client.is_connected()}")
            bot_client.tdrive_worker_name = username

            # Invite and promote (30s timeout)
            logger.info(f"[recover] Inviting @{username} to groups...")
            t_invite = time.time()
            success = await asyncio.wait_for(
                self._invite_and_promote_bot(shared_state, username),
                timeout=30.0
            )
            logger.info(f"[recover] Invite result for @{username}: success={success}, elapsed={time.time()-t_invite:.1f}s")

            if success:
                shared_state.clients_pool.append(bot_client)
                bot_info = next((b for b in self.get_all_bots() if b['username'] == username), None)
                if bot_info:
                    self.mark_bot_joined(bot_info['bot_id'])
                logger.info(f"[recover] Hot-plugged @{username} into pool. Pool size={len(shared_state.clients_pool)}")
                return True
            else:
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

    async def provision_missing_bots(self, shared_state: 'SharedState', target_count: int = 5):
        """Automatically provisions missing bots via @BotFather up to the target count."""
        
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
                # 1. Ask BotFather for a new bot
                try:
                    async with shared_state.client.conversation('BotFather', timeout=15) as conv:
                        await conv.send_message('/newbot')
                        resp = await conv.get_response()
                        logger.info(f"[provision] BotFather: {resp.text[:200]}")
                        
                        if 'How are we going to call it' not in resp.text:
                            logger.warning(f"[provision] BotFather refused to create new bot. Response: {resp.text:.200s}")
                            if 'too many attempts' in resp.text.lower():
                                # Handle flood wait
                                match = re.search(r'in (\d+) seconds', resp.text)
                                wait_time = int(match.group(1)) if match else 10
                                logger.info(f"[provision] Rate limited. Sleeping {wait_time}s...")
                                await asyncio.sleep(wait_time + 1)
                                continue # retry
                            else:
                                if shared_state.connection_emitter:
                                    shared_state.connection_emitter('bot_limit_reached')
                                break # Stop attempting to create more
                            
                        # 2. Choose Name
                        name = "TDrive Worker Node"
                        await conv.send_message(name)
                        resp = await conv.get_response()
                        logger.info(f"[provision] Name response: {resp.text[:200]}")
                        
                        if 'choose a username' not in resp.text:
                            logger.error(f"[provision] Unexpected response when setting name: {resp.text:.200s}")
                            continue
                            
                        # 3. Choose unique username
                        random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                        username = f"tdrive_worker_{random_suffix}_bot"
                        await conv.send_message(username)
                        resp = await conv.get_response()
                        logger.info(f"[provision] Username response: {resp.text[:200]}")
                        
                        # Handle duplicate username scenario just in case
                        if 'Sorry, this username is already taken' in resp.text:
                            random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
                            username = f"tdrive_{random_suffix}_bot"
                            await conv.send_message(username)
                            resp = await conv.get_response()

                        if 'Done!' in resp.text:
                            # 4. Extract Token using Regex
                            match = re.search(r'(\d{8,10}:[a-zA-Z0-9_-]{35,})', resp.text)
                            if not match:
                                logger.error("[provision] Failed to extract token from BotFather.")
                                continue
                            
                            token = match.group(1)
                            bot_id = int(token.split(':')[0])
                            
                            # 5. Save to DB
                            self.add_bot(bot_id, token, username)
                            logger.info(f"[provision] Created and registered bot: {username} (bot_id={bot_id})")
                            
                            # 6. Invite bot to groups and promote
                            t_invite = time.time()
                            success = await self._invite_and_promote_bot(shared_state, username)
                            logger.info(f"[provision] Invite result for {username}: success={success}, elapsed={time.time()-t_invite:.1f}s")
                            
                            if success:
                                self.mark_bot_joined(bot_id)
                                
                                # 7. Initialize immediately (Hot-plug)
                                bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
                                added_hotplug = False
                                try:
                                    logger.info(f"[provision] Starting hot-plug for {username}...")
                                    t_boot = time.time()
                                    await asyncio.wait_for(bot_client.start(bot_token=token), timeout=30.0)
                                    elapsed_boot = time.time() - t_boot
                                    logger.info(f"[provision] {username} hot-plug started in {elapsed_boot:.1f}s")
                                    bot_client.tdrive_worker_name = username
                                    if await bot_client.is_user_authorized():
                                        if not hasattr(shared_state, 'clients_pool'):
                                            shared_state.clients_pool = []
                                        shared_state.clients_pool.append(bot_client)
                                        added_hotplug = True
                                        logger.info(f"[provision] {username} hot-plugged into pool. Pool size={len(shared_state.clients_pool)}")
                                    else:
                                        logger.warning(f"[provision] {username} hot-plug started but not authorized.")
                                        await self._safe_disconnect(bot_client)
                                except asyncio.CancelledError:
                                    if not added_hotplug:
                                        await self._safe_disconnect(bot_client)
                                    raise
                                except asyncio.TimeoutError:
                                    logger.error(f"[provision] TIMEOUT starting {username} (30s). Skipping hot-plug.")
                                    await self._safe_disconnect(bot_client)
                                except Exception as e:
                                    logger.error(f"[provision] Error starting {username}: {e}")
                                    await self._safe_disconnect(bot_client)
                            else:
                                logger.error(f"[provision] Failed to invite {username}. Not added to pool.")
                        else:
                            logger.error(f"[provision] Failed to create bot {username}. Response: {resp.text:.200s}")

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

    def _get_target_group_ids(self, shared_state: 'SharedState') -> List[int]:
        """Collects all registered storage group IDs across shared_state and database."""
        group_ids = set()
        if hasattr(shared_state, 'group_id_map') and shared_state.group_id_map:
            for tg_id in shared_state.group_id_map.values():
                if tg_id:
                    group_ids.add(tg_id)

        if hasattr(shared_state, 'group_id') and shared_state.group_id:
            group_ids.add(shared_state.group_id)

        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_group_id FROM storage_groups")
            for row in cursor.fetchall():
                if row['telegram_group_id']:
                    group_ids.add(row['telegram_group_id'])
        except Exception as e:
            logger.debug(f"Could not query storage_groups table for bot promotion: {e}")

        return list(group_ids)

    async def _invite_and_promote_in_group(self, client, bot_entity, username: str, group_id: int) -> bool:
        """Invite a resolved bot entity to a single storage group and grant admin rights."""
        rights = ChatAdminRights(
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            manage_call=False,
            invite_users=False,
            ban_users=False,
            pin_messages=False,
            add_admins=False
        )

        # RPC 2: Invite to group
        logger.info(f"[_invite] Inviting {username} to group {group_id}...")
        t_rpc = time.time()
        try:
            from telethon.errors.rpcerrorlist import UserAlreadyParticipantError, FloodWaitError
            await client(InviteToChannelRequest(
                channel=group_id,
                users=[bot_entity]
            ))
            logger.info(f"[_invite] Invite OK in {time.time()-t_rpc:.1f}s for group {group_id}")
        except UserAlreadyParticipantError:
            logger.info(f"[_invite] Already participant in group {group_id} ({time.time()-t_rpc:.1f}s)")
        except FloodWaitError as e:
            logger.warning(f"[_invite] FloodWait on invite group {group_id}: wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"[_invite] Invite FAILED in {time.time()-t_rpc:.1f}s for group {group_id}: {e}")

        # RPC 3: Promote to admin
        logger.info(f"[_invite] Promoting {username} in group {group_id}...")
        t_rpc = time.time()
        try:
            from telethon.errors.rpcerrorlist import FloodWaitError
            await client(EditAdminRequest(
                channel=group_id,
                user_id=bot_entity,
                admin_rights=rights,
                rank="Worker Node"
            ))
            logger.info(f"[_invite] Promote OK in {time.time()-t_rpc:.1f}s for group {group_id}")
            return True
        except FloodWaitError as e:
            logger.warning(f"[_invite] FloodWait on promote group {group_id}: wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"[_invite] Promote FAILED in {time.time()-t_rpc:.1f}s for group {group_id}: {e}")
        return False

    async def invite_and_promote_bot_to_group(self, shared_state: 'SharedState', username: str, group_id: int) -> bool:
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

    async def _invite_and_promote_bot(self, shared_state: 'SharedState', username: str):
        """Invites a bot to all registered TDrive storage groups and gives it admin rights."""
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

            # RPC 1: Resolve bot entity
            logger.info(f"[_invite] Resolving entity for {username}...")
            t_rpc = time.time()
            try:
                bot_entity = await client.get_input_entity(username)
                logger.info(f"[_invite] Entity resolved in {time.time()-t_rpc:.1f}s: {bot_entity}")
            except Exception as e:
                logger.error(f"[_invite] Failed to resolve entity for {username} in {time.time()-t_rpc:.1f}s: {e}")
                return False

            overall_success = False
            for group_id in target_group_ids:
                if await self._invite_and_promote_in_group(client, bot_entity, username, group_id):
                    overall_success = True

            elapsed = time.time() - t_start
            logger.info(f"[_invite] Done: {username}, overall_success={overall_success}, elapsed={elapsed:.1f}s")
            return overall_success
        except Exception as e:
            logger.error(f"[_invite] Exception for {username}: {e}", exc_info=True)
            return False
