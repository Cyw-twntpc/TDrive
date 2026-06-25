import asyncio
import re
import random
import string
from telethon import TelegramClient
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.types import ChatAdminRights
from telethon.sessions import StringSession
import logging
from typing import List, Dict, Optional
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)

class BotManager:
    """
    Manages the lifecycle, provisioning, and storage of Worker Bots for the Token Pool.
    """
    def __init__(self, db: DatabaseConnection = None):
        self.db = db if db else DatabaseConnection()

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


    async def init_saved_bots(self, shared_state: 'SharedState'):
        """Initializes all registered bots from the database and connects them."""
        
        bots = self.get_all_bots()
        if not bots:
            logger.info("No saved worker bots found.")
            return

        logger.info(f"Initializing {len(bots)} saved worker bots...")
        for bot_info in bots:
            try:
                # Initialize bot client. We use StringSession("") so it stays in memory
                from telethon.sessions import StringSession
                bot_client = TelegramClient(
                    StringSession(""), 
                    shared_state.api_id, 
                    shared_state.api_hash
                )
                
                # Start as bot using token
                await bot_client.start(bot_token=bot_info['token'])
                
                # Verify connection and authorization
                if await bot_client.is_user_authorized():
                    # Ensure it's in the group
                    if bot_info.get('group_joined') == 1:
                        if not hasattr(shared_state, 'clients_pool'):
                            shared_state.clients_pool = []
                        shared_state.clients_pool.append(bot_client)
                        logger.info(f"Worker bot {bot_info['username']} connected successfully.")
                    else:
                        logger.warning(f"Worker bot {bot_info['username']} is authorized but not joined to the group yet. Skipping.")
                else:
                    logger.warning(f"Bot {bot_info['username']} failed authorization. Token might be revoked.")
            except Exception as e:
                logger.error(f"Failed to initialize bot {bot_info['username']}: {e}", exc_info=True)


    async def recover_lost_bots(self, shared_state: 'SharedState', target_count: int = 5):
        """Scans BotFather history for previously created TDrive bots that are missing from the local database."""
        
        client = shared_state.client
        if not client or not client.is_connected():
            return

        self.db.sync_manager.set_busy(True)
        try:
            current_bots = self.get_all_bots()
            existing_bot_ids = {b['bot_id'] for b in current_bots}
            
            if len(current_bots) >= target_count:
                return

            logger.info("Scanning @BotFather history for lost bots...")
            
            # Fetch last 50 messages from BotFather
            messages = await client.get_messages('BotFather', limit=50)
            
            recovered_count = 0
            for msg in messages:
                if not msg.text: continue
                
                # Look for the success message containing the username and token
                if 'Done! Congratulations on your new bot.' in msg.text and 'tdrive_worker_' in msg.text:
                    # Extract username
                    username_match = re.search(r't\.me/(tdrive_worker_[a-zA-Z0-9_]+_bot)', msg.text)
                    # Extract token
                    token_match = re.search(r'(\d{8,10}:[a-zA-Z0-9_-]{35,})', msg.text)
                    
                    if username_match and token_match:
                        username = username_match.group(1)
                        token = token_match.group(1)
                        bot_id = int(token.split(':')[0])
                        
                        if bot_id not in existing_bot_ids:
                            logger.info(f"Found lost bot: {username}. Recovering...")
                            # Add to database
                            if self.add_bot(bot_id, token, username):
                                existing_bot_ids.add(bot_id)
                                recovered_count += 1
                                
                                # Boot it up
                                old_threshold = getattr(client, 'flood_sleep_threshold', 60)
                                try:
                                    
                                    # Temporarily disable main client auto-sleep for invite operation
                                    client.flood_sleep_threshold = 0
                                    
                                    bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
                                    bot_client.flood_sleep_threshold = 0  # Disable auto-sleep on rate limits
                                    
                                    # 10s timeout for booting
                                    await asyncio.wait_for(bot_client.start(bot_token=token), timeout=10.0)
                                    bot_client.tdrive_worker_name = username
                                    
                                    # 10s timeout for inviting and promoting
                                    success = await asyncio.wait_for(
                                        self._invite_and_promote_bot(shared_state, username),
                                        timeout=10.0
                                    )
                                    
                                    if success:
                                        shared_state.clients_pool.append(bot_client)
                                        self.mark_bot_joined(bot_id)
                                        logger.info(f"Successfully recovered and hot-plugged bot: {username}")
                                    else:
                                        logger.warning(f"Recovered bot {username} but failed to invite to group.")
                                        await bot_client.disconnect()
                                        
                                    if len(existing_bot_ids) >= target_count:
                                        break
                                except asyncio.TimeoutError:
                                    logger.error(f"Timeout while booting or inviting recovered bot {username}. Skipping for now.")
                                except Exception as e:
                                    logger.error(f"Error booting recovered bot {username}: {e}")
                                finally:
                                    # Always restore the original threshold
                                    client.flood_sleep_threshold = old_threshold
                                    
            if recovered_count > 0:
                logger.info(f"Recovered {recovered_count} lost bots from history.")
            else:
                logger.info("No lost bots found in history.")
                
        except Exception as e:
            logger.error(f"Error recovering lost bots: {e}")
        finally:
            self.db.sync_manager.set_busy(False)

    async def provision_missing_bots(self, shared_state: 'SharedState', target_count: int = 5):
        """Automatically provisions missing bots via @BotFather up to the target count."""
        
        self.db.sync_manager.set_busy(True)
        try:
            current_bots = self.get_all_bots()
            needed = target_count - len(current_bots)
            if needed <= 0:
                logger.info("Bot Token pool is full. No provisioning needed.")
                return

            if not shared_state.client or not shared_state.client.is_connected():
                logger.warning("Main client not connected. Cannot provision bots.")
                return

            logger.info(f"Attempting to auto-provision {needed} worker bots...")

            for i in range(needed):
                # 1. Ask BotFather for a new bot
                try:
                    async with shared_state.client.conversation('BotFather', timeout=15) as conv:
                        await conv.send_message('/newbot')
                        resp = await conv.get_response()
                        
                        if 'How are we going to call it' not in resp.text:
                            logger.warning(f"BotFather refused to create new bot. Response: {resp.text:.200s}")
                            if 'too many attempts' in resp.text.lower():
                                # Handle flood wait
                                match = re.search(r'in (\d+) seconds', resp.text)
                                wait_time = int(match.group(1)) if match else 10
                                logger.info(f"Rate limited by BotFather. Sleeping for {wait_time}s...")
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
                        
                        if 'choose a username' not in resp.text:
                            logger.error(f"Unexpected response when setting name: {resp.text:.200s}")
                            continue
                            
                        # 3. Choose unique username
                        random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                        username = f"tdrive_worker_{random_suffix}_bot"
                        await conv.send_message(username)
                        resp = await conv.get_response()
                        
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
                                logger.error("Failed to extract token from BotFather's success message.")
                                continue
                            
                            token = match.group(1)
                            bot_id = int(token.split(':')[0])
                            
                            # 5. Save to DB
                            self.add_bot(bot_id, token, username)
                            logger.info(f"Successfully created and registered bot: {username}")
                            
                            # 6. Add bot to TDrive Private Group and Promote
                            success = await self._invite_and_promote_bot(shared_state, username)
                            if success:
                                self.mark_bot_joined(bot_id)
                                
                                # 7. Initialize immediately (Hot-plug)
                                bot_client = TelegramClient(StringSession(""), shared_state.api_id, shared_state.api_hash)
                                await bot_client.start(bot_token=token)
                                bot_client.tdrive_worker_name = username
                                if await bot_client.is_user_authorized():
                                    if not hasattr(shared_state, 'clients_pool'):
                                        shared_state.clients_pool = []
                                    shared_state.clients_pool.append(bot_client)
                                    logger.info(f"Bot {username} hot-plugged into Transfer Pool.")
                            else:
                                logger.error(f"Failed to invite newly created bot {username} to the group. It will not be added to the pool.")
                        else:
                            logger.error(f"Failed to create bot {username}. Response: {resp.text:.200s}")

                except asyncio.TimeoutError:
                    logger.error("Timeout waiting for BotFather response.")
                except Exception as e:
                    logger.error(f"Error during bot provisioning: {e}", exc_info=True)
                    
                # Small delay to prevent spamming Telegram servers
                await asyncio.sleep(10.0)

        except Exception as overall_e:
            logger.error(f"Provisioning error: {overall_e}")
        finally:
            self.db.sync_manager.set_busy(False)

    async def _invite_and_promote_bot(self, shared_state: 'SharedState', username: str):
        """Invites a bot to the TDrive group and gives it admin rights."""
        try:
            if not shared_state.group_id:
                return
                
            client = shared_state.client
            # Resolve bot entity
            bot_entity = await client.get_input_entity(username)
            
            # Invite to group
            try:
                from telethon.errors.rpcerrorlist import UserAlreadyParticipantError
                await client(InviteToChannelRequest(
                    channel=shared_state.group_id,
                    users=[bot_entity]
                ))
                logger.info(f"Invited {username} to TDrive group.")
            except UserAlreadyParticipantError:
                logger.info(f"{username} is already a participant.")
            except Exception as e:
                logger.warning(f"Invite warning for {username}: {e}")
            
            # Promote to admin
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
            await client(EditAdminRequest(
                channel=shared_state.group_id,
                user_id=bot_entity,
                admin_rights=rights,
                rank="Worker Node"
            ))
            logger.info(f"Promoted {username} to admin in TDrive group.")
            return True
        except Exception as e:
            logger.error(f"Failed to invite or promote bot {username}: {e}", exc_info=True)
            return False
