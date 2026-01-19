from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SetHistoryTTLRequest
from telethon.errors import FloodWaitError
import os
import time
import asyncio
import logging
import random
import io
from typing import Callable, List, Set, TypeVar, Awaitable, Optional

from . import crypto_handler as cr
from . import file_processor as fp

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

async def upload_file_to_cloud(client, group_id: int, file_path: str, original_file_hash: str, task_id: str, 
                               progress_callback: Callable | None = None, resume_context: List = None,
                               chunk_callback: Callable[[int, int, str], None] = None) -> list:
    """Streams, encrypts, and uploads a file with async I/O."""    
    split_files_info = list(resume_context) if resume_context else []
    completed_parts = {item[0] for item in split_files_info}
    
    loop = asyncio.get_running_loop()

    try:
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        total_size = os.path.getsize(file_path)
        
        uploaded_bytes_base = 0
        for part_num in completed_parts:
            uploaded_bytes_base += fp.CHUNK_SIZE 
        if uploaded_bytes_base > total_size:
            uploaded_bytes_base = total_size

        last_update_time = 0
        current_uploaded_accumulated = uploaded_bytes_base

        def callback(current, total):
            nonlocal last_update_time, current_uploaded_accumulated
            now = time.time()
            elapsed = now - last_update_time
            
            if elapsed > CALLBACK_ELAPSED:
                last_update_time = now
                real_current = current_uploaded_accumulated + current
                if real_current > total_size: real_current = total_size
                if progress_callback:
                    progress_callback(real_current, total_size)

        generator = fp.stream_split_and_encrypt(file_path, key, completed_parts)

        while True:
            await asyncio.sleep(0) 

            try:
                result = await loop.run_in_executor(None, next, generator, None)
                if result is None:
                    break
                part_num, part_bytes = result
            except StopIteration:
                break
            except Exception as e:
                logger.error(f"Error in encryption stream: {e}")
                raise

            if part_num in completed_parts:
                continue

            part_hash = await loop.run_in_executor(None, cr.hash_bytes, part_bytes)
            
            async def _upload_chunk():
                return await client.send_file(
                    group_id,
                    file=part_bytes, 
                    progress_callback=callback
                )

            message = await _retry_with_backoff(_upload_chunk)
            
            split_files_info.append([part_num, message.id, part_hash])
            current_uploaded_accumulated += len(part_bytes)
            
            if chunk_callback:
                try:
                    chunk_callback(part_num, message.id, part_hash)
                except Exception as e:
                    logger.warning(f"Chunk callback failed: {e}")

        split_files_info.sort(key=lambda x: x[0])
        return split_files_info

    except asyncio.CancelledError:
        logger.info(f"Upload cancelled for task {task_id}")
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise

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

async def download_file(client, group_id: int, file_details: dict, download_dir: str, task_id: str, 
                        progress_callback: Callable | None = None, completed_parts: Set[int] = None,
                        chunk_callback: Callable[[int], None] = None):
    """Downloads, decrypts, and reassembles a file."""
    if completed_parts is None:
        completed_parts = set()

    file_name = file_details['name']
    chunks = file_details['chunks'] # Expecting list of dicts: {'part_num', 'message_id', 'part_hash'}
    
    part_info_map = {part['message_id']: {"num": part['part_num'], "hash": part['part_hash']} for part in chunks}
    
    message_ids = []
    for chunk in chunks:
        if chunk['part_num'] not in completed_parts:
            message_ids.append(chunk['message_id'])
    
    final_path = os.path.join(download_dir, file_name)
    key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])

    loop = asyncio.get_running_loop()

    try:
        if not message_ids and len(completed_parts) == len(chunks):
             logger.info("All parts marked as completed. Skipping download loop.")
        elif message_ids:
            messages_to_download = await client.get_messages(group_id, ids=message_ids)
            messages_to_download = [m for m in messages_to_download if m]
            
            if len(messages_to_download) != len(message_ids):
                 logger.warning(f"Requested {len(message_ids)} chunks but got {len(messages_to_download)}. Some cloud messages might be missing.")

        total_size = int(file_details['size'])
        
        await loop.run_in_executor(None, fp.prepare_download_file, final_path, total_size)
        
        downloaded_bytes_base = 0
        for chunk in chunks:
            if chunk['part_num'] in completed_parts:
                downloaded_bytes_base += fp.CHUNK_SIZE
        if downloaded_bytes_base > total_size:
            downloaded_bytes_base = total_size

        last_update_time = 0
        current_downloaded_accumulated = downloaded_bytes_base

        def callback(current, total):
            nonlocal last_update_time, current_downloaded_accumulated
            now = time.time()
            elapsed = now - last_update_time
            
            if elapsed > CALLBACK_ELAPSED:
                last_update_time = now
                real_current = current_downloaded_accumulated + current
                if progress_callback:
                    progress_callback(real_current, total_size)

        if message_ids:
            for message in messages_to_download:
                await asyncio.sleep(0)

                part_num = part_info_map[message.id]["num"]
                expected_part_hash = part_info_map[message.id]["hash"]
                
                async def _process_part():
                    nonlocal current_downloaded_accumulated
                    encrypted_bytes = await message.download_media(file=bytes, progress_callback=callback)
                    if not encrypted_bytes: raise ValueError("Telegram returned empty response")

                    actual_part_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_bytes)
                    if actual_part_hash != expected_part_hash: raise ValueError(f"Part {part_num} checksum mismatch.")

                    offset = (part_num - 1) * fp.CHUNK_SIZE
                    await loop.run_in_executor(
                        None, 
                        fp.decrypt_bytes_and_write, 
                        encrypted_bytes, final_path, key, offset
                    )
                    
                    current_downloaded_accumulated += message.document.size
                    if chunk_callback:
                        chunk_callback(part_num)

                await _retry_with_backoff(_process_part)

        logger.info(f"All parts of '{file_name}' processed. Performing final integrity check.")
        final_hash = await loop.run_in_executor(None, cr.hash_data, final_path)
        
        if final_hash != file_details['hash']:
            raise ValueError(f"'{file_name}' final checksum mismatch.")
        
        logger.info(f"'{file_name}' successfully downloaded and verified.")

    except asyncio.CancelledError:
        logger.info(f"Download task for '{file_name}' (ID: {task_id}) cancelled.")
        raise
    except Exception as e:
        logger.error(f"Download failed for '{file_name}' (task_id: {task_id}): {e}", exc_info=True)
        raise
