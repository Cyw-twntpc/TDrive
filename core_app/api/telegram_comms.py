"""
Handles all low-level communication with the Telegram API using Telethon.

This module encapsulates the logic for finding or creating the storage group,
streaming file uploads and downloads, and synchronizing the local database with
a remote backup stored in the group's pinned messages.
"""
from telethon.tl.types import InputMessagesFilterPinned
from telethon.tl.functions.channels import CreateChannelRequest
import os
import shutil
import zipfile
import time
import asyncio
import json
import logging
from typing import Callable

from . import crypto_handler as cr
from . import file_processor as fp
from ..data.db_handler import DatabaseHandler
from ..data.shared_state import TEMP_DIR

logger = logging.getLogger(__name__)

# An asyncio lock to prevent concurrent executions of `sync_database_file`,
# which could lead to race conditions when accessing the database file.
update_lock = asyncio.Lock()

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

async def get_group(client, app_api_id: int) -> int | None:
    """
    Finds or creates the dedicated 'TDrive' storage group.

    The logic follows a three-step process:
    1.  Check the local `info.json` cache for a saved group_id.
    2.  If not found, iterate through the user's dialogs to find the group by name.
    3.  If still not found, create a new private megagroup named 'TDrive'.
    """
    name = "TDrive"

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
                    return decrypted_data['group_id']
    except Exception as e:
        logger.warning(f"Failed to read cached group_id: {e}", exc_info=True)

    # 2. If not cached, search through dialogs on the server.
    logger.info("Searching for 'TDrive' group on the server...")
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.is_group and dialog.name == name:
            logger.info(f"Found Group ID on server: {dialog.id}. Caching it locally.")
            _save_group_id(app_api_id, dialog.id)
            return dialog.id

    # 3. If not found anywhere, create a new group.
    logger.info("TDrive group not found, creating a new one...")
    try:
        result = await client(CreateChannelRequest(
            title=name,
            about="This is the TDrive storage group. Do not delete or leave.",
            megagroup=True
        ))
        channel = result.chats[0]
        # Convert the channel ID to a marked ID for use in Telethon's API
        group_id = int(f"-100{channel.id}")
        _save_group_id(app_api_id, group_id)
        logger.info(f"Successfully created new group with ID: {group_id}")
        return group_id
    except Exception as e:
        logger.error(f"Fatal error while creating group: {e}", exc_info=True)
        return None

async def upload_file_with_info(client, group_id: int, file_path: str, task_id: str, progress_callback: Callable | None = None) -> list:
    """
    Streams, encrypts, and uploads a file, returning metadata for all its chunks.
    """
    temp_dir = os.path.join(TEMP_DIR, f"temp_upload_{os.path.basename(file_path)}_{task_id}")
    file_name = os.path.basename(file_path)
    split_files_info = []

    try:
        os.makedirs(temp_dir, exist_ok=True)
        
        original_file_hash = cr.hash_data(file_path)
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        
        total_size = os.path.getsize(file_path)
        uploaded_bytes = 0
        last_update_time = 0
        last_call_bytes = 0

        def callback(current, total):
            nonlocal uploaded_bytes, last_update_time, last_call_bytes
            now = time.time()
            # Throttle progress updates to about twice a second to avoid UI lag.
            if now - last_update_time > 0.5 or current == total:
                elapsed = now - last_update_time
                speed = (current - last_call_bytes) / elapsed if elapsed > 0 else 0
                last_call_bytes = current
                last_update_time = now
                if progress_callback:
                    progress_callback(task_id, file_name, uploaded_bytes + current, total_size, 'transferring', speed)

        # Stream the file, encrypting and uploading one chunk at a time.
        for part_num, part_path in fp.stream_split_and_encrypt(file_path, key, original_file_hash, temp_dir):
            try:
                part_hash = cr.hash_data(part_path)
                last_call_bytes = 0 # Reset for each new part's progress callback.
                
                message = await client.send_file(
                    group_id,
                    file=part_path,
                    progress_callback=callback
                )
                
                split_files_info.append([part_num, message.id, part_hash])
                uploaded_bytes += os.path.getsize(part_path)
            finally:
                # Ensure temporary encrypted chunks are deleted immediately after use.
                if os.path.exists(part_path):
                    os.remove(part_path)

        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)
        
        logger.info(f"File '{file_name}' (task_id: {task_id}) uploaded successfully.")
        return split_files_info

    except Exception as e:
        logger.error(f"Upload failed for file '{file_name}' (task_id: {task_id}): {e}", exc_info=True)
        if progress_callback:
            progress_callback(task_id, file_name, 0, 0, 'failed', 0)
        raise
    finally:
        # Final cleanup of the main temporary directory for the upload.
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.debug(f"Cleaned up main upload temp directory: {temp_dir}")

async def download_file(client, group_id: int, file_details: dict, download_dir: str, task_id: str, progress_callback: Callable | None = None):
    """
    Downloads, decrypts, and reassembles a file from its chunks.
    
    This function pre-allocates the final file on disk and writes decrypted
    chunks directly to their correct positions, avoiding high memory usage.
    """
    file_name = file_details['name']
    part_info_map = {part['message_id']: {"num": part['part_num'], "hash": part['part_hash']} for part in file_details['chunks']}
    message_ids = list(part_info_map.keys())
    
    temp_dir = os.path.join(TEMP_DIR, f"temp_download_{task_id}")
    final_path = os.path.join(download_dir, file_name)
    key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])

    try:
        os.makedirs(temp_dir, exist_ok=True)
        logger.info(f"Starting stream download for '{file_name}' (task_id: {task_id}).")

        messages_to_download = await client.get_messages(group_id, ids=message_ids)
        messages_to_download = [m for m in messages_to_download if m]
        
        if len(messages_to_download) != len(file_details['chunks']):
            raise FileNotFoundError(f"Could not retrieve all parts for '{file_name}'. Some parts may be missing from the cloud.")

        total_size = int(file_details['size'])
        
        # 1. Check disk space and pre-allocate the file.
        free_space = shutil.disk_usage(download_dir).free
        if free_space < total_size:
            raise IOError(f"Not enough disk space. Required: {total_size / 1024**2:.2f} MB, Available: {free_space / 1024**2:.2f} MB.")
        
        with open(final_path, 'wb') as f:
            f.seek(total_size - 1)
            f.write(b'\0')
        logger.debug(f"Pre-allocated file of size {total_size} at '{final_path}'.")

        # 2. Stream download, decrypt, and write chunks.
        downloaded_bytes = 0
        last_update_time = 0
        last_call_bytes = 0

        def callback(current, total):
            nonlocal downloaded_bytes, last_update_time, last_call_bytes
            now = time.time()
            if now - last_update_time > 0.5 or current == total:
                elapsed = now - last_update_time
                speed = (current - last_call_bytes) / elapsed if elapsed > 0 else 0
                last_call_bytes = current
                last_update_time = now
                if progress_callback:
                    progress_callback(task_id, file_name, downloaded_bytes + current, total_size, 'transferring', speed)

        MAX_DOWNLOAD_PART_RETRIES = 3

        for message in messages_to_download:
            part_num = part_info_map[message.id]["num"]
            expected_part_hash = part_info_map[message.id]["hash"]
            last_call_bytes = 0
            
            for retry_count in range(MAX_DOWNLOAD_PART_RETRIES):
                downloaded_part_path = await message.download_media(file=temp_dir, progress_callback=callback)
                
                if not downloaded_part_path or not os.path.exists(downloaded_part_path):
                    logger.warning(f"Download of part {part_num} failed or file not found. Retrying... ({retry_count + 1}/{MAX_DOWNLOAD_PART_RETRIES})")
                    await asyncio.sleep(1)
                    continue

                actual_part_hash = cr.hash_data(downloaded_part_path)
                if actual_part_hash == expected_part_hash:
                    logger.debug(f"Part {part_num} checksum verified.")
                    try:
                        offset = (part_num - 1) * fp.CHUNK_SIZE
                        fp.decrypt_and_write_chunk(downloaded_part_path, final_path, key, offset)
                        logger.debug(f"Part {part_num} decrypted and written to offset {offset}.")
                        os.remove(downloaded_part_path)
                        break # Success, break retry loop
                    except Exception as e:
                        logger.error(f"Error processing part {part_num}: {e}", exc_info=True)
                        raise # Re-raise critical internal errors
                else:
                    logger.warning(f"Part {part_num} checksum mismatch. Retrying... ({retry_count + 1}/{MAX_DOWNLOAD_PART_RETRIES})")
                    os.remove(downloaded_part_path)
                    await asyncio.sleep(1)
            else: # This 'else' belongs to the 'for' loop, executed if the loop completes without a 'break'.
                raise ValueError(f"Part {part_num} failed checksum verification after {MAX_DOWNLOAD_PART_RETRIES} retries.")
            
            downloaded_bytes += message.document.size

        # 3. Final integrity check on the reassembled file.
        logger.info(f"All parts of '{file_name}' processed. Performing final integrity check.")
        final_hash = cr.hash_data(final_path)
        if final_hash != file_details['hash']:
            raise ValueError(f"Final checksum for '{file_name}' does not match. The file may be corrupt.")
        
        logger.info(f"'{file_name}' successfully downloaded and verified.")
        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)

    except asyncio.CancelledError:
        logger.warning(f"Download task for '{file_name}' (ID: {task_id}) was cancelled.")
        if progress_callback:
            progress_callback(task_id, file_name, 0, 0, 'cancelled', 0)
        if 'final_path' in locals() and os.path.exists(final_path):
            os.remove(final_path)
        raise

    except Exception as e:
        logger.error(f"Download failed for '{file_name}' (task_id: {task_id}): {e}", exc_info=True)
        if progress_callback:
            msg = str(e) if isinstance(e, (IOError, ValueError, FileNotFoundError)) else "An unexpected error occurred during download."
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
        if 'final_path' in locals() and os.path.exists(final_path):
            os.remove(final_path)
        raise
    
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.debug(f"Cleaned up download temp directory: {temp_dir}")

async def _perform_db_upload(client, group_id: int, db_path: str, remote_db_message):
    """
    Handles the actual process of uploading, pinning, and cleaning up old database backups.
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
        caption = f"db_version:{version}"

        if remote_db_message:
            logger.info("Removing old pinned database backup...")
            try:
                await client.unpin_message(group_id, remote_db_message.id)
                await client.delete_messages(group_id, [remote_db_message.id])
            except Exception as e:
                logger.warning(f"Could not remove old database backup, proceeding anyway: {e}")

        logger.info(f"Uploading new database backup (Version: {version})...")
        new_message = await client.send_file(group_id, file=temp_zip_path, caption=caption)
        await client.pin_message(group_id, new_message.id, notify=False)
        logger.info(f"New database backup (Version: {version}) has been uploaded and pinned.")

    except Exception as e:
        logger.error(f"An error occurred during database upload: {e}", exc_info=True)
    finally:
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)

async def sync_database_file(client, group_id: int, mode: str = 'sync', db_path: str = './file/tdrive.db'):
    """
    Synchronizes the local database with the remote backup in the pinned messages.

    Modes:
    - 'sync': Compares local and remote versions and syncs the newer one.
    - 'upload': Forces an upload of the local database.
    """
    async with update_lock:
        # Telethon 要求頻道 ID 在 get_messages 等方法中需要是負數 (-100xxxxxxxxx)
        telethon_group_id = int(f"-100{group_id}") if group_id > 0 else group_id
        # Find the pinned database message in the group
        messages = await client.get_messages(telethon_group_id, limit=1, filter=InputMessagesFilterPinned)
        remote_db_message = messages[0] if messages else None

        if mode == 'upload':
            await _perform_db_upload(client, group_id, db_path, remote_db_message)
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
                    remote_version = int(remote_db_message.text.split("db_version:", 1)[1].split(',')[0].strip())
                except (ValueError, IndexError):
                    logger.warning("Could not parse version from remote database backup caption.")
            else:
                logger.info("No remote database backup found.")

            logger.info(f"Local DB version: {local_version}, Remote DB version: {remote_version}")
            
            if local_version > remote_version:
                logger.info("Local database is newer. Uploading to cloud...")
                await _perform_db_upload(client, group_id, db_path, remote_db_message)
            
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
