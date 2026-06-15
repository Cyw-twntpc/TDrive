import logging
import asyncio
import os
from typing import TYPE_CHECKING, Optional
from telethon import TelegramClient

# Use a forward reference for type hinting to avoid circular imports.
if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)


async def ensure_client_connected(shared_state: 'SharedState') -> Optional[TelegramClient]:
    if shared_state.client and shared_state.client.is_connected():
        return shared_state.client

    logger.warning("Connection lost. Locking UI and attempting to reconnect to Telegram...")
    
    if shared_state.connection_emitter:
        shared_state.connection_emitter('lost')

    api_id = shared_state.api_id
    api_hash = shared_state.api_hash
    session_file = f'./file/user_{api_id}.session'

    if not (api_id and api_hash):
        logger.error("Cannot reconnect: API credentials not found in SharedState.")
        if shared_state.connection_emitter:
            shared_state.connection_emitter('restored') 
        return None

    while True:
        try:
            if shared_state.client:
                try:
                    await shared_state.client.disconnect()
                except Exception:
                    pass 

            new_client = TelegramClient(session_file, api_id, api_hash)
            await new_client.connect()
            
            if await new_client.is_user_authorized():
                logger.info("Successfully reconnected to Telegram.")
                shared_state.client = new_client
                
                if shared_state.connection_emitter:
                    shared_state.connection_emitter('restored')
                return new_client
            else:
                logger.error("Reconnection failed: user authorization is invalid. Re-login is required.")
                break

        except Exception as e:
            logger.error(f"Telegram reconnection attempt failed: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
    
    if shared_state.connection_emitter:
        shared_state.connection_emitter('restored')
    return None

async def ensure_clients_pool_connected(shared_state: 'SharedState', pool_size: int = 4) -> bool:
    if not shared_state.client or not shared_state.client.is_connected():
        await ensure_client_connected(shared_state)
    
    if not shared_state.client:
        return False

    api_id = shared_state.api_id
    api_hash = shared_state.api_hash
    session_str = shared_state.client.session.save()
    
    from telethon.sessions import StringSession
    
    connected_count = sum(1 for c in shared_state.clients_pool if c and c.is_connected())
    if connected_count >= pool_size:
        return True

    logger.info(f"Initializing {pool_size} background clients for concurrency pool...")
    
    # Cleanup old disconnected pool if any
    for c in shared_state.clients_pool:
        if c and c.is_connected():
            await c.disconnect()
    shared_state.clients_pool = []
    
    for i in range(pool_size):
        try:
            pool_client = TelegramClient(StringSession(session_str), api_id, api_hash)
            await pool_client.connect()
            if await pool_client.is_user_authorized():
                shared_state.clients_pool.append(pool_client)
            else:
                logger.warning(f"Pool client {i} failed authorization.")
        except Exception as e:
            logger.error(f"Failed to initialize pool client {i}: {e}")
            
    logger.info(f"Successfully initialized {len(shared_state.clients_pool)} clients in the pool.")
    return len(shared_state.clients_pool) > 0

def check_path_exists(path: str) -> bool:
    return os.path.exists(path)

