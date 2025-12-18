"""
Handles all low-level communication with the Telegram API using Telethon.

This module encapsulates the logic for finding or creating the storage group,
streaming file uploads and downloads, and synchronizing the local database with
a remote backup stored in the group's pinned messages.
"""
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SetHistoryTTLRequest
import os
import zipfile
import time
import asyncio
import json
import logging
from typing import Callable, List, Optional, Set

from . import crypto_handler as cr
from . import file_processor as fp
from ..data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

# An asyncio lock to prevent concurrent executions of `sync_database_file`,
# which could lead to race conditions when accessing the database file.
update_lock = asyncio.Lock()

CALLBACK_ELAPSED = 0.5 #seconds

def _save_group_id(api_id: int, group_id: int):
    """
    Saves the TDrive storage group ID into the encrypted info.json file.
    This caches the group_id to avoid searching for it on every startup.
    """
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
    """
    Checks if auto-delete (TTL) is enabled on the group and disables it if so.
    This prevents accidental data loss.
    """
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
    """
    Finds or creates the dedicated 'TDrive' storage group.

    The logic follows a three-step process:
    1.  Check the local `info.json` cache for a saved group_id.
    2.  If not found, iterate through the user's dialogs to find the group by name.
    3.  If still not found, create a new private megagroup named 'TDrive'.
    """
    name = "TDrive"
    group_id = None

    # 1. Try to read the group_id from the local cache first.
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

    # 2. If not cached, search through dialogs on the server.
    logger.info("Searching for 'TDrive' group on the server...")
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.is_group and dialog.name == name:
            logger.info(f"Found Group ID on server: {dialog.id}. Caching it locally.")
            _save_group_id(app_api_id, dialog.id)
            await _ensure_no_ttl(client, dialog.id)
            return dialog.id

    # 3. If not found anywhere, create a new group.
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

async def upload_file_with_info(client, group_id: int, file_path: str, original_file_hash: str, task_id: str, 
                                progress_callback: Callable | None = None, resume_context: List = None,
                                chunk_callback: Callable[[int, int, str], None] = None,
                                update_transferred_bytes: Callable[[int], None] = None,
                                parent_id: str | None = None) -> list:
    """
    Streams, encrypts, and uploads a file with fully async I/O.
    
    Args:
        resume_context: A list of already uploaded part info.
        chunk_callback: A function called on every successful chunk upload: (part_num, msg_id, hash).
                        This ensures the Controller is updated in real-time.
    """
    file_name = os.path.basename(file_path)
    
    # Initialize split_files_info. If resuming, we start with what we have.
    split_files_info = list(resume_context) if resume_context else []
    
    # Identify completed parts to skip
    completed_parts = {item[0] for item in split_files_info}
    
    loop = asyncio.get_running_loop()

    try:
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        
        total_size = os.path.getsize(file_path)
        
        # Calculate initial uploaded bytes based on completed parts for correct progress bar
        uploaded_bytes = 0
        for part_num in completed_parts:
            # Estimate size
            uploaded_bytes += fp.CHUNK_SIZE 
        
        if uploaded_bytes > total_size:
            uploaded_bytes = total_size

        last_update_time = 0
        last_call_bytes = 0

        # 定義進度回調
        def callback(current, total):
            nonlocal uploaded_bytes, last_update_time, last_call_bytes
            now = time.time()
            elapsed = now - last_update_time
            if elapsed > CALLBACK_ELAPSED:
                delta = current - last_call_bytes
                if update_transferred_bytes:
                    asyncio.create_task(update_transferred_bytes(delta))
                speed = delta / elapsed
                last_call_bytes = current
                last_update_time = now
                if progress_callback:
                    progress_callback(task_id, file_name, uploaded_bytes + current, total_size, 'transferring', speed, parent_id=parent_id)

        # 建立生成器，傳入 completed_parts 以便跳過已完成部分
        generator = fp.stream_split_and_encrypt(file_path, key, completed_parts)

        while True:
            # 將 "讀取原始檔 + 加密 + 寫入暫存檔" 移至背景執行緒
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

            try:
                # 雙重檢查
                if part_num in completed_parts:
                    continue

                # 分塊雜湊計算移至背景
                part_hash = await loop.run_in_executor(None, cr.hash_bytes, part_bytes)
                last_call_bytes = 0
                
                # 直接發送記憶體中的 bytes
                message = await client.send_file(
                    group_id,
                    file=part_bytes, 
                    progress_callback=callback
                )
                
                # Update local list
                split_files_info.append([part_num, message.id, part_hash])
                
                # Update base progress
                uploaded_bytes += len(part_bytes)

                # [REAL-TIME UPDATE] Notify Controller
                if chunk_callback:
                    try:
                        chunk_callback(part_num, message.id, part_hash)
                    except Exception as e:
                        logger.warning(f"Chunk callback failed: {e}")
                
            except Exception as e:
                logger.error(f"Error uploading part {part_num}: {e}")
                raise

        # Sort results by part number before returning
        split_files_info.sort(key=lambda x: x[0])

        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0, parent_id=parent_id)
        
        logger.info(f"File '{file_name}' uploaded successfully.")
        return split_files_info

    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        if progress_callback:
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, parent_id=parent_id)
        raise

async def download_file(client, group_id: int, file_details: dict, download_dir: str, task_id: str, 
                        progress_callback: Callable | None = None, completed_parts: Set[int] = None,
                        chunk_callback: Callable[[int], None] = None, 
                        update_transferred_bytes: Callable[[int], None] = None):
    """
    Downloads, decrypts, and reassembles a file.
    
    Args:
        completed_parts: Set of part numbers to skip.
        chunk_callback: A function called on every successful chunk download: (part_num).
    """
    if completed_parts is None:
        completed_parts = set()

    file_name = file_details['name']
    part_info_map = {part['message_id']: {"num": part['part_num'], "hash": part['part_hash']} for part in file_details['chunks']}
    
    # Filter message IDs: Only download what we don't have
    message_ids = []
    for chunk in file_details['chunks']:
        if chunk['part_num'] not in completed_parts:
            message_ids.append(chunk['message_id'])
    
    final_path = os.path.join(download_dir, file_name)
    key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])

    loop = asyncio.get_running_loop()

    try:
        logger.info(f"Starting stream download for '{file_name}' (task_id: {task_id}). Resuming: {len(completed_parts) > 0}")

        if not message_ids and len(completed_parts) == len(file_details['chunks']):
             logger.info("All parts marked as completed. Skipping download loop.")
             # Proceed to integrity check directly
        elif message_ids:
            # 取得訊息物件 (Metadata)
            messages_to_download = await client.get_messages(group_id, ids=message_ids)
            messages_to_download = [m for m in messages_to_download if m]
            
            if len(messages_to_download) != len(message_ids):
                 logger.warning(f"Requested {len(message_ids)} chunks but got {len(messages_to_download)}. Some cloud messages might be missing.")

        total_size = int(file_details['size'])
        
        # Prepare file
        await loop.run_in_executor(None, fp.prepare_download_file, final_path, total_size)
        
        # Initialize downloaded bytes based on completed parts
        downloaded_bytes = 0
        for chunk in file_details['chunks']:
            if chunk['part_num'] in completed_parts:
                downloaded_bytes += fp.CHUNK_SIZE
        if downloaded_bytes > total_size:
            downloaded_bytes = total_size

        last_update_time = 0
        last_call_bytes = 0

        def callback(current, total):
            nonlocal downloaded_bytes, last_update_time, last_call_bytes
            now = time.time()
            elapsed = now - last_update_time
            if elapsed > CALLBACK_ELAPSED:
                delta = current - last_call_bytes
                if update_transferred_bytes:
                    asyncio.create_task(update_transferred_bytes(delta))
                speed = delta / elapsed
                last_call_bytes = current
                last_update_time = now
                if progress_callback:
                    progress_callback(task_id, file_name, downloaded_bytes + current, total_size, 'transferring', speed)

        MAX_DOWNLOAD_PART_RETRIES = 3

        if message_ids:
            for message in messages_to_download:
                part_num = part_info_map[message.id]["num"]
                expected_part_hash = part_info_map[message.id]["hash"]
                last_call_bytes = 0
                
                for retry_count in range(MAX_DOWNLOAD_PART_RETRIES):
                    try:
                        # 下載到記憶體 (Bytes)
                        encrypted_bytes = await message.download_media(file=bytes, progress_callback=callback)
                        
                        if not encrypted_bytes:
                            raise ValueError("Empty response from Telegram")

                        # 在背景計算雜湊
                        actual_part_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_bytes)
                        
                        if actual_part_hash == expected_part_hash:
                            # 在背景解密並寫入硬碟
                            offset = (part_num - 1) * fp.CHUNK_SIZE
                            await loop.run_in_executor(
                                None, 
                                fp.decrypt_bytes_and_write, 
                                encrypted_bytes, final_path, key, offset
                            )
                            
                            # [REAL-TIME UPDATE] Notify Controller
                            if chunk_callback:
                                try:
                                    chunk_callback(part_num)
                                except Exception as e:
                                    logger.warning(f"Chunk callback failed: {e}")
                                    
                            break # 成功
                        else:
                            logger.warning(f"Part {part_num} checksum mismatch. Retrying... ({retry_count + 1}/{MAX_DOWNLOAD_PART_RETRIES})")
                            await asyncio.sleep(1)

                    except Exception as e:
                        logger.warning(f"Download error for part {part_num}: {e}. Retrying...")
                        await asyncio.sleep(1)
                else:
                    raise ValueError(f"Part {part_num} failed checksum verification after {MAX_DOWNLOAD_PART_RETRIES} retries.")
                
                downloaded_bytes += message.document.size

        # 最終完整性檢查
        logger.info(f"All parts of '{file_name}' processed. Performing final integrity check.")
        final_hash = await loop.run_in_executor(None, cr.hash_data, final_path)
        
        if final_hash != file_details['hash']:
            raise ValueError(f"'{file_name}' 的最終校驗和不符。檔案可能已損毀。")
        
        logger.info(f"'{file_name}' successfully downloaded and verified.")
        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)

    except asyncio.CancelledError:
        logger.warning(f"Download task for '{file_name}' (ID: {task_id}) was cancelled.")
        raise

    except Exception as e:
        logger.error(f"Download failed for '{file_name}' (task_id: {task_id}): {e}", exc_info=True)
        if progress_callback:
            msg = str(e) if isinstance(e, (IOError, ValueError, FileNotFoundError)) else "下載過程中發生未預期的錯誤。"
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
        raise

async def _perform_db_upload(client, group_id: int, db_path: str):
    """
    Handles the actual process of uploading and cleaning up old database backups.
    Uses a hashtag search mechanism instead of pinned messages to avoid flood wait limits.
    """
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
        # Include the unique hashtag for easy retrieval
        caption = f"#tdrive_db_backup db_version:{version}"

        logger.info(f"Uploading new database backup (Version: {version})...")
        new_message = await client.send_file(group_id, file=temp_zip_path, caption=caption)
        logger.info(f"New database backup (Version: {version}) has been uploaded.")
        
        # Clean up ALL old backup messages to ensure only the latest exists
        logger.info("Scanning for and removing old database backups...")
        try:
            # Search for all backup messages
            # Note: Telethon's search might not immediately index the new message, 
            # but we filter by ID just in case.
            old_messages = await client.get_messages(group_id, limit=50, search='#tdrive_db_backup')
            
            ids_to_delete = [msg.id for msg in old_messages if msg.id != new_message.id]
            
            if ids_to_delete:
                logger.info(f"Deleting {len(ids_to_delete)} old backup message(s)...")
                await client.delete_messages(group_id, ids_to_delete)
                logger.info("Old backups removed.")
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
    """
    Synchronizes the local database with the remote backup using hashtag search.

    Modes:
    - 'sync': Compares local and remote versions and syncs the newer one.
    - 'upload': Forces an upload of the local database.
    """
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
                    # Robust parsing for " ... db_version:123 ..."
                    text_parts = remote_db_message.text.split("db_version:")
                    if len(text_parts) > 1:
                        remote_version = int(text_parts[1].split()[0].strip()) # Take the first token after 'db_version:'
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