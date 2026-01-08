from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SetHistoryTTLRequest
from telethon.errors import FloodWaitError
import os
import zipfile
import time
import asyncio
import json
import logging
import random
import io
from typing import Callable, List, Set, TypeVar, Awaitable, Optional

from . import crypto_handler as cr
from . import file_processor as fp
from ..data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

# Prevent concurrent database syncs
update_lock = asyncio.Lock()

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

def _save_group_id(api_id: int, group_id: int):
    """Caches group_id in local encrypted info.json."""
    try:
        info_path = './file/info.json'
        current_info = {}
        if os.path.exists(info_path):
            with open(info_path, 'r') as f:
                current_info = json.load(f)
        
        # The api_hash is required to re-encrypt the data blob.
        decrypted_blob = cr.decrypt_secure_data(current_info.get("secure_data_blob"), str(api_id))
        if not decrypted_blob or 'api_hash' not in decrypted_blob:
            logger.error("Failed to save group_id: Could not decrypt existing data to retrieve api_hash.")
            return

        # Add the group_id to the dictionary and re-encrypt.
        secure_data = {"api_hash": decrypted_blob['api_hash'], "group_id": group_id}
        encrypted_blob = cr.encrypt_secure_data(secure_data, str(api_id))
        
        final_info = {
            "api_id": api_id,
            "secure_data_blob": encrypted_blob
        }
        with open(info_path, 'w') as f:
            json.dump(final_info, f)
        logger.info(f"Group ID {group_id} has been successfully saved to local cache.")

    except Exception as e:
        logger.error(f"Failed to save group_id: {e}", exc_info=True)

async def _ensure_no_ttl(client, group_id: int):
    """Disables auto-delete (TTL) on the storage group."""
    try:
        # Get the entity (chat/channel)
        entity = await client.get_entity(group_id)
        
        # Check if 'ttl_period' attribute exists and is > 0
        current_ttl = getattr(entity, 'ttl_period', 0)
        
        if current_ttl and current_ttl > 0:
            logger.info(f"Auto-delete is enabled (TTL: {current_ttl}s) for group {group_id}. Disabling it...")
            # Set TTL to 0 to disable auto-delete
            await client(SetHistoryTTLRequest(peer=entity, period=0))
            logger.info("Auto-delete successfully disabled.")
        else:
            logger.debug(f"Auto-delete check passed for group {group_id} (TTL is 0 or unset).")
            
    except Exception as e:
        # Log warning but don't fail the entire startup process
        logger.warning(f"Failed to check or disable auto-delete (TTL) for group {group_id}: {e}")

async def get_group(client, app_api_id: int) -> int | None:
    """Finds or creates the dedicated 'TDrive' storage group."""
    name = "TDrive"
    group_id = None

    try:
        info_path = './file/info.json'
        if os.path.exists(info_path):
            with open(info_path, 'r') as f:
                info = json.load(f)
            if info.get("api_id") == app_api_id:
                decrypted_data = cr.decrypt_secure_data(info.get("secure_data_blob"), str(app_api_id))
                if decrypted_data and decrypted_data.get('group_id'):
                    logger.info(f"Found Group ID in cache: {decrypted_data['group_id']}")
                    group_id = decrypted_data['group_id']
    except Exception as e:
        logger.warning(f"Failed to read cached group_id: {e}", exc_info=True)

    if group_id:
        await _ensure_no_ttl(client, group_id)
        return group_id

    logger.info("Searching for 'TDrive' group on the server...")
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.is_group and dialog.name == name:
            logger.info(f"Found Group ID on server: {dialog.id}. Caching it locally.")
            _save_group_id(app_api_id, dialog.id)
            await _ensure_no_ttl(client, dialog.id)
            return dialog.id

    logger.info("TDrive group not found, creating a new one...")
    try:
        result = await client(CreateChannelRequest(
            title=name,
            about="這是 TDrive 儲存群組。請勿刪除或退出。",
            megagroup=True
        ))
        channel = result.chats[0]
        # Convert the channel ID to a marked ID for use in Telethon's API
        group_id = int(f"-100{channel.id}")
        _save_group_id(app_api_id, group_id)
        logger.info(f"Successfully created new group with ID: {group_id}")
        await _ensure_no_ttl(client, group_id)
        return group_id
    except Exception as e:
        logger.error(f"Fatal error while creating group: {e}", exc_info=True)
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
            # Check cancellation
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
    Used for thumbnails DB and preview images.
    """
    loop = asyncio.get_running_loop()
    try:
        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        total_size = len(data_bytes)
        
        # Split bytes into chunks
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
    loop = asyncio.get_running_loop()
    try:
        messages = await client.get_messages(group_id, ids=msg_ids)
        messages = [m for m in messages if m]
        
        if not messages:
            return None
            
        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        final_buffer = io.BytesIO()
        
        # Sort messages by ID to ensure correct order if they are sequential chunks
        # In a robust system, we should use the 'part_num' from DB, but for simple data blobs 
        # uploaded via upload_data_as_file, the message IDs are usually sequential.
        # For strict correctness, the caller should provide sorted IDs or we trust Telethon's return order.
        # Here we assume msg_ids passed in are already sorted/ordered correctly by the caller.
        
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
    part_info_map = {part['message_id']: {"num": part['part_num'], "hash": part['part_hash']} for part in file_details['chunks']}
    
    message_ids = []
    for chunk in file_details['chunks']:
        if chunk['part_num'] not in completed_parts:
            message_ids.append(chunk['message_id'])
    
    final_path = os.path.join(download_dir, file_name)
    key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])

    loop = asyncio.get_running_loop()

    try:
        if not message_ids and len(completed_parts) == len(file_details['chunks']):
             logger.info("All parts marked as completed. Skipping download loop.")
        elif message_ids:
            messages_to_download = await client.get_messages(group_id, ids=message_ids)
            messages_to_download = [m for m in messages_to_download if m]
            
            if len(messages_to_download) != len(message_ids):
                 logger.warning(f"Requested {len(message_ids)} chunks but got {len(messages_to_download)}. Some cloud messages might be missing.")

        total_size = int(file_details['size'])
        
        await loop.run_in_executor(None, fp.prepare_download_file, final_path, total_size)
        
        downloaded_bytes_base = 0
        for chunk in file_details['chunks']:
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
                    encrypted_bytes = await message.download_media(file=bytes, progress_callback=callback)
                    
                    if not encrypted_bytes:
                        raise ValueError("Telegram 回傳空回應")

                    actual_part_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_bytes)
                    
                    if actual_part_hash != expected_part_hash:
                         raise ValueError(f"第 {part_num} 部分校驗和不符。")

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
            raise ValueError(f"'{file_name}' 的最終校驗和不符。檔案可能已損毀。")
        
        logger.info(f"'{file_name}' successfully downloaded and verified.")

    except asyncio.CancelledError:
        logger.info(f"Download task for '{file_name}' (ID: {task_id}) cancelled.")
        raise
    except Exception as e:
        logger.error(f"Download failed for '{file_name}' (task_id: {task_id}): {e}", exc_info=True)
        raise

async def _perform_db_upload(client, group_id: int, db_path: str):
    """Uploads local database and cleans up old backups."""
    logger.info("Uploading local database to the cloud...")
    if not os.path.exists(db_path):
        logger.error(f"Database file '{db_path}' not found. Cannot upload.")
        return

    temp_zip_path = db_path + ".zip"
    
    try:
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(db_path, os.path.basename(db_path))
        
        db = DatabaseHandler(db_path)
        version = db.get_db_version()
        caption = f"#tdrive_db_backup db_version:{version}"

        logger.info(f"Uploading new database backup (Version: {version})...")
        new_message = await client.send_file(group_id, file=temp_zip_path, caption=caption)
        logger.info(f"New database backup (Version: {version}) has been uploaded.")
        
        logger.info("Scanning for and removing old database backups...")
        try:
            old_messages = await client.get_messages(group_id, limit=50, search='#tdrive_db_backup')
            ids_to_delete = [msg.id for msg in old_messages if msg.id != new_message.id]
            
            if ids_to_delete:
                logger.info(f"Deleting {len(ids_to_delete)} old backup message(s)...")
                await client.delete_messages(group_id, ids_to_delete)
            else:
                logger.info("No old backups found to delete.")

        except Exception as e:
            logger.warning(f"Could not remove old database backups, proceeding anyway: {e}")

    except Exception as e:
        logger.error(f"An error occurred during database upload: {e}", exc_info=True)
    finally:
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)

async def sync_database_file(client, group_id: int, mode: str = 'sync', db_path: str = './file/tdrive.db'):
    """Syncs local database with remote backup (hashtag-based)."""
    async with update_lock:
        telethon_group_id = int(f"-100{group_id}") if group_id > 0 else group_id
        
        # Search for the latest message with the specific hashtag
        messages = await client.get_messages(telethon_group_id, limit=1, search='#tdrive_db_backup')
        remote_db_message = messages[0] if messages else None

        if mode == 'upload':
            await _perform_db_upload(client, group_id, db_path)
            return

        if mode == 'sync':
            logger.info("Starting database synchronization...")
            
            if not os.path.exists(db_path):
                if remote_db_message:
                    logger.warning(f"Local database '{db_path}' not found. Attempting to restore from cloud.")
                    await client.download_media(remote_db_message, file=db_path + ".zip")
                    with zipfile.ZipFile(db_path + ".zip", 'r') as z:
                        z.extractall(os.path.dirname(db_path))
                    os.remove(db_path + ".zip")
                    logger.info("Successfully restored database from cloud backup.")
                else:
                    logger.error(f"Local database '{db_path}' not found and no remote backup exists. Cannot sync.")
                return

            db = DatabaseHandler(db_path)
            local_version = db.get_db_version()
            remote_version = -1
            
            if remote_db_message and remote_db_message.text and "db_version:" in remote_db_message.text:
                try:
                    text_parts = remote_db_message.text.split("db_version:")
                    if len(text_parts) > 1:
                        remote_version = int(text_parts[1].split()[0].strip())
                except (ValueError, IndexError):
                    logger.warning("Could not parse version from remote database backup caption.")
            else:
                logger.info("No remote database backup found.")

            logger.info(f"Local DB version: {local_version}, Remote DB version: {remote_version}")
            
            if local_version > remote_version:
                logger.info("Local database is newer. Uploading to cloud...")
                await _perform_db_upload(client, group_id, db_path)
            
            elif remote_version > local_version:
                logger.info("Remote database is newer. Downloading from cloud...")
                temp_zip_path = db_path + ".zip"
                try:
                    await client.download_media(remote_db_message, file=temp_zip_path)
                    if os.path.exists(db_path): os.remove(db_path)
                    with zipfile.ZipFile(temp_zip_path, 'r') as z:
                        z.extractall(os.path.dirname(db_path))
                    logger.info("Local database has been updated from the cloud.")
                except Exception as e:
                    logger.error(f"Failed to download or extract remote database: {e}", exc_info=True)
                finally:
                    if os.path.exists(temp_zip_path):
                        os.remove(temp_zip_path)

            else:
                logger.info("Local and remote database versions are identical. No sync needed.")
                return