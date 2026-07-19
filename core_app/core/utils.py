import logging
import asyncio
import os
import py7zr
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
    session_file = f'./file/user_data/user_{api_id}.session'

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
                except Exception as e:
                    logger.warning(f"Error disconnecting old client: {e}") 

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


def check_path_exists(path: str) -> bool:
    return os.path.exists(path)


VENDOR_DIR = os.path.join(os.path.dirname(__file__), '..', 'vendor')
_extracted_cache = {}

def ensure_extracted(name: str) -> str:
    """Extract name.7z -> name/ on first call. Returns path to extracted dir."""
    if name in _extracted_cache:
        return _extracted_cache[name]
    archive = os.path.join(VENDOR_DIR, f'{name}.7z')
    target = os.path.join(VENDOR_DIR, name)
    if not os.path.isdir(target) and os.path.isfile(archive):
        with py7zr.SevenZipFile(archive, mode='r') as z:
            z.extractall(path=target)
    _extracted_cache[name] = target
    return target

