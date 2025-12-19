import logging
import asyncio
import threading
from typing import TYPE_CHECKING, Optional
from telethon import TelegramClient

from core_app.api import telegram_comms

# Use a forward reference for type hinting to avoid circular imports.
if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState

logger = logging.getLogger(__name__)


async def ensure_client_connected(shared_state: 'SharedState') -> Optional[TelegramClient]:
    """
    Ensures the Telegram client is connected.

    If the client is disconnected, it attempts to reconnect in a loop, emitting
    signals to the UI to indicate the connection status ('lost' and 'restored').
    
    Returns:
        An active and authorized TelegramClient instance, or None if reconnection fails.
    """
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
            shared_state.connection_emitter('restored') # Restore UI interaction
        return None

    while True:
        try:
            if shared_state.client:
                try:
                    await shared_state.client.disconnect()
                except Exception:
                    pass # Ignore errors on disconnecting a faulty client

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

def _upload_db(shared_state: 'SharedState'):
    """
    The actual workhorse function that performs the database upload.
    This function is scheduled to run in the main asyncio event loop.
    """
    async def upload_task():
        try:
            logger.info("Executing delayed database upload task...")
            client = await ensure_client_connected(shared_state)
            if not client or not shared_state.api_id:
                logger.error("Aborting DB upload task: client connection or api_id is missing.")
                return

            await telegram_comms.sync_database_file(client, shared_state.group_id, mode='upload')
            logger.info("Background database upload task completed.")
        except Exception as e:
            logger.error(f"Background database upload task failed: {e}", exc_info=True)

    if shared_state.loop and shared_state.loop.is_running():
        # Safely schedule the async task from a potentially different thread.
        shared_state.loop.call_soon_threadsafe(lambda: asyncio.create_task(upload_task()))
    else:
        logger.warning("Event loop is not running. Cannot schedule database upload.")

async def trigger_db_upload_in_background(shared_state: 'SharedState'):
    """
    Triggers a debounced database upload in the background.

    This function uses a threading.Timer to delay the upload. If called
    multiple times within a short period (2 seconds), it cancels the previous
    timer and starts a new one, effectively coalescing multiple database
    modifications into a single upload operation.
    """
    # Cancel any previously scheduled timer.
    if shared_state.db_upload_timer:
        shared_state.db_upload_timer.cancel()
        logger.debug("Cancelled previous database upload timer.")

    shared_state.db_upload_timer = threading.Timer(2.0, lambda: _upload_db(shared_state))
    shared_state.db_upload_timer.start()
    logger.debug("Scheduled a new database upload in 2 seconds.")

