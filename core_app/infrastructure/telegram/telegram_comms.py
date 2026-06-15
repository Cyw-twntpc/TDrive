from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SetHistoryTTLRequest
from telethon.errors import FloodWaitError
import asyncio
import logging
import random
import io
from typing import Callable, List, TypeVar, Awaitable, Optional

from core_app.core import crypto_handler as cr
from core_app.infrastructure.local_fs import file_processor as fp

logger = logging.getLogger(__name__)

CALLBACK_ELAPSED = 0.5 # seconds - Throttle UI updates

T = TypeVar('T')

async def _retry_with_backoff(
    func: Callable[[], Awaitable[T]], 
    max_retries: int = 5, 
    base_delay: float = 1.0, 
    max_delay: float = 32.0
) -> T:
    """Executes async function with exponential backoff and FloodWait handling."""
    attempt = 0
    while True:
        try:
            return await func()
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError: Sleeping for {e.seconds} seconds.")
            await asyncio.sleep(e.seconds)
        except (OSError, ValueError, asyncio.TimeoutError) as e: 
            attempt += 1
            if attempt > max_retries:
                logger.error(f"Operation failed after {max_retries} attempts: {e}")
                raise
            
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = random.uniform(0, 0.5 * delay)
            sleep_time = delay + jitter
            
            logger.warning(f"Operation failed (Attempt {attempt}/{max_retries}): {e}. Retrying in {sleep_time:.2f}s...")
            await asyncio.sleep(sleep_time)
        except Exception as e:
             logger.error(f"Non-retriable error: {e}")
             raise

async def _ensure_no_ttl(client, group_id: int):
    """Disables auto-delete (TTL) on the storage group."""
    try:
        entity = await client.get_entity(group_id)
        current_ttl = getattr(entity, 'ttl_period', 0)
        
        if current_ttl and current_ttl > 0:
            logger.info(f"Auto-delete is enabled (TTL: {current_ttl}s) for group {group_id}. Disabling it...")
            await client(SetHistoryTTLRequest(peer=entity, period=0))
            logger.info("Auto-delete successfully disabled.")
    except Exception as e:
        logger.warning(f"Failed to check or disable auto-delete (TTL) for group {group_id}: {e}")

# Note: Group ID saving/loading logic should be moved to AuthService or similar high-level manager.
# telegram_comms should just be given a group_id to work with.
# For compatibility with existing calls, we might keep get_group helper here but purely functional.

async def get_group_id(client, group_name: str = "TDrive") -> int | None:
    """Finds or creates the dedicated storage group on Telegram."""
    try:
        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            if dialog.is_group and dialog.name == group_name:
                await _ensure_no_ttl(client, dialog.id)
                return dialog.id

        logger.info(f"Group '{group_name}' not found, creating a new one...")
        result = await client(CreateChannelRequest(
            title=group_name,
            about="TDrive Storage Group. Do not delete.",
            megagroup=True
        ))
        channel = result.chats[0]
        group_id = int(f"-100{channel.id}")
        await _ensure_no_ttl(client, group_id)
        return group_id
    except Exception as e:
        logger.error(f"Failed to find or create group: {e}", exc_info=True)
        return None

async def upload_single_chunk(client, group_id: int, part_bytes: bytes, progress_callback: Callable | None = None):
    """Uploads a single byte sequence with backoff and retry."""
    async def _upload_chunk():
        return await client.send_file(
            group_id,
            file=part_bytes,
            progress_callback=progress_callback
        )
    return await _retry_with_backoff(_upload_chunk)

async def upload_data_as_file(client, group_id: int, data_bytes: bytes, original_hash: str, 
                              progress_callback: Callable | None = None) -> list:
    """
    Uploads raw bytes as a file (stream split and encrypted).
    Used for thumbnails DB, preview images, and Map Files.
    """
    loop = asyncio.get_running_loop()
    try:
        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        total_size = len(data_bytes)
        
        chunks = []
        for i in range(0, total_size, fp.CHUNK_SIZE):
            chunks.append(data_bytes[i:i + fp.CHUNK_SIZE])
            
        split_files_info = []
        uploaded_accumulated = 0
        
        for idx, chunk in enumerate(chunks):
            part_num = idx + 1
            encrypted_chunk = cr.encrypt(chunk, key)
            part_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_chunk)
            
            async def _upload_chunk():
                return await client.send_file(
                    group_id,
                    file=encrypted_chunk,
                    progress_callback=lambda c, t: progress_callback(c, total_size) if progress_callback else None
                )

            message = await _retry_with_backoff(_upload_chunk)
            split_files_info.append([part_num, message.id, part_hash])
            
            uploaded_accumulated += len(encrypted_chunk)
            if progress_callback:
                progress_callback(uploaded_accumulated, total_size)

        return split_files_info

    except Exception as e:
        logger.error(f"Data upload failed: {e}", exc_info=True)
        raise

async def download_data_as_bytes(client, group_id: int, msg_ids: List[int], original_hash: str) -> Optional[bytes]:
    """
    Downloads messages and reassembles them into bytes in memory.
    """
    try:
        messages = await client.get_messages(group_id, ids=msg_ids)
        messages = [m for m in messages if m]
        
        if not messages:
            return None
            
        if len(messages) != len(msg_ids):
            logger.error(f"Requested {len(msg_ids)} chunks but only got {len(messages)}. Aborting to prevent data corruption.")
            return None
            
        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        final_buffer = io.BytesIO()
        
        for message in messages:
            encrypted_bytes = await message.download_media(file=bytes)
            if not encrypted_bytes: continue
            
            decrypted_chunk = cr.decrypt(encrypted_bytes, key)
            final_buffer.write(decrypted_chunk)
            
        return final_buffer.getvalue()

    except Exception as e:
        logger.error(f"Data download failed: {e}", exc_info=True)
        return None

async def download_single_chunk(client, message, progress_callback: Callable | None = None) -> bytes:
    """Downloads a single media chunk with backoff and retry."""
    async def _download_chunk():
        return await client.download_media(message, file=bytes, progress_callback=progress_callback)
    return await _retry_with_backoff(_download_chunk)
