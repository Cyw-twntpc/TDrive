from telethon.tl.types import InputMessagesFilterPinned
from telethon.tl.functions.channels import CreateChannelRequest
import os
import shutil
import zipfile
import time
import asyncio
import json

import logging
from . import crypto_handler as cr
from . import file_processor as fp
from .db_handler import DatabaseHandler
from .shared_state import TEMP_DIR

logger = logging.getLogger(__name__)

update_lock = asyncio.Lock()

def _save_group_id(api_id, group_id):
    """將 group_id 儲存到加密的 info.json 中。"""
    try:
        info_path = './file/info.json'
        current_info = {}
        if os.path.exists(info_path):
            with open(info_path, 'r') as f:
                current_info = json.load(f)
        
        api_hash = cr.decrypt_secure_data(current_info.get("secure_data_blob"), str(api_id)).get('api_hash')
        if not api_hash:
            logger.error("錯誤：無法解密現有資料以儲存 group_id。")
            return

        secure_data = {"api_hash": api_hash, "group_id": group_id}
        encrypted_blob = cr.encrypt_secure_data(secure_data, str(api_id))
        
        final_info = {
            "api_id": api_id,
            "secure_data_blob": encrypted_blob
        }
        with open(info_path, 'w') as f:
            json.dump(final_info, f)
        logger.info(f"Group ID {group_id} 已成功儲存到本地。")

    except Exception as e:
        logger.error(f"儲存 group_id 失敗: {e}", exc_info=True)

async def get_group(client, app_api_id):
    name = "TDrive"

    # 1. 嘗試從本地讀取 group_id
    try:
        info_path = './file/info.json'
        if os.path.exists(info_path):
            with open(info_path, 'r') as f:
                info = json.load(f)
            if info.get("api_id") == app_api_id:
                decrypted_data = cr.decrypt_secure_data(info.get("secure_data_blob"), str(app_api_id))
                if decrypted_data and decrypted_data.get('group_id'):
                    logger.info(f"從快取中找到 Group ID: {decrypted_data['group_id']}")
                    return decrypted_data['group_id']
    except Exception as e:
        logger.warning(f"讀取快取的 group_id 失敗: {e}", exc_info=True)

    # 2. 如果本地沒有，則遍歷對話
    logger.info("正在從伺服器搜尋 TDrive 群組...")
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.is_group and dialog.name == name:
            _save_group_id(app_api_id, dialog.entity.id)
            logger.info(f"從伺服器找到 Group ID: {dialog.entity.id}")
            return dialog.entity.id

    # 3. 如果都找不到，則建立新群組
    logger.info("找不到 TDrive 群組，正在建立新的...")
    try:
        result = await client(CreateChannelRequest(
            title=name,
            about="This is TDrive storage group, don't delete.",
            megagroup=True
        ))
        group_id = result.chats[0].id
    except Exception as e:
        logger.error(f"建立群組時發生嚴重錯誤: {e}", exc_info=True)
        return None

    _save_group_id(app_api_id, group_id)
    return group_id

async def upload_file_with_info(client, group_id, file_path, task_id, progress_callback=None):
    """
    【串流式】上傳檔案，並返回所有分塊的資訊（ID, hash 等）。
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
            if now - last_update_time > 0.5 or current == total:
                elapsed = now - last_update_time
                speed = (current - last_call_bytes) / elapsed if elapsed > 0 else 0
                last_call_bytes = current
                last_update_time = now
                if progress_callback:
                    progress_callback(task_id, file_name, uploaded_bytes + current, total_size, 'transferring', speed)

        # 新的串流模式迴圈
        for part_num, part_path in fp.stream_split_and_encrypt(file_path, key, original_file_hash, temp_dir):
            try:
                part_hash = cr.hash_data(part_path)
                last_call_bytes = 0
                
                message = await client.send_file(
                    group_id,
                    file=part_path,
                    progress_callback=callback
                )
                
                split_files_info.append([part_num, message.id, part_hash])
                uploaded_bytes += os.path.getsize(part_path)
            
            finally:
                # 確保無論上傳成功或失敗，暫存分塊都會被刪除
                if os.path.exists(part_path):
                    os.remove(part_path)

        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)
        
        logger.info(f"檔案 '{file_name}' (task_id: {task_id}) 所有分塊上傳完成。")
        return split_files_info

    except Exception as e:
        logger.error(f"上傳檔案 '{file_name}' (task_id: {task_id}) 失敗: {e}", exc_info=True)
        if progress_callback:
            progress_callback(task_id, file_name, 0, 0, 'failed', 0)
        raise e
    finally:
        # 在流程結束時，清理最外層的暫存目錄
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理上傳主暫存目錄: {temp_dir}")

async def download_file(client, group_id, file_details, download_dir, task_id, progress_callback=None):
    """
    【串流式】根據分塊資訊列表，下載所有分割檔，校驗並寫入最終檔案。
    """
    file_name = file_details['name']
    part_info_map = {part['message_id']: {"num": part['part_num'], "hash": part['part_hash']} for part in file_details['chunks']}
    message_ids = list(part_info_map.keys())
    
    temp_dir = os.path.join(TEMP_DIR, f"temp_download_{task_id}")
    final_path = os.path.join(download_dir, file_name)
    key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])

    try:
        os.makedirs(temp_dir, exist_ok=True)
        logger.info(f"開始串流式下載檔案 '{file_name}' (task_id: {task_id})。")

        messages_to_download = await client.get_messages(group_id, ids=message_ids)
        messages_to_download = [m for m in messages_to_download if m]
        
        if len(messages_to_download) != len(file_details['chunks']):
            raise FileNotFoundError(f"檔案 '{file_name}' 的部分分塊在雲端遺失。")

        total_size = int(file_details['size'])
        
        # --- 1. 空間檢查與檔案佔位 ---
        free_space = shutil.disk_usage(download_dir).free
        temp_free_space = shutil.disk_usage(temp_dir).free
        if free_space < total_size:
            raise IOError(f"磁碟空間不足。需要 {total_size / 1024**2:.2f} MB，可用 {free_space / 1024**2:.2f} MB。")
        
        if temp_free_space < fp.CHUNK_SIZE:
            raise IOError(f"暫存空間不足。需要 {fp.CHUNK_SIZE / 1024**2:.2f} MB，可用 {free_space / 1024**2:.2f} MB。")

        with open(final_path, 'wb') as f:
            f.seek(total_size - 1)
            f.write(b'\0')
        logger.info(f"已在 '{final_path}' 建立大小為 {total_size} 的佔位檔案。")

        # --- 2. 串流式下載與寫入 ---
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
            retry_count = 0
            
            while retry_count < MAX_DOWNLOAD_PART_RETRIES:
                downloaded_part_path = await message.download_media(file=temp_dir, progress_callback=callback)
                
                if not downloaded_part_path or not os.path.exists(downloaded_part_path):
                    retry_count += 1
                    logger.warning(f"分塊 {part_num} 下載失敗或檔案不存在，正在重試... ({retry_count}/{MAX_DOWNLOAD_PART_RETRIES})")
                    await asyncio.sleep(1)
                    continue

                actual_part_hash = cr.hash_data(downloaded_part_path)
                if actual_part_hash == expected_part_hash:
                    logger.debug(f"分塊 {part_num} 校驗成功。")
                    try:
                        offset = (part_num - 1) * fp.CHUNK_SIZE
                        fp.decrypt_and_write_chunk(downloaded_part_path, final_path, key, offset)
                        logger.debug(f"分塊 {part_num} 已成功解密並寫入至偏移量 {offset}。")
                        os.remove(downloaded_part_path)
                        break
                    except Exception as e:
                        logger.error(f"處理分塊 {part_num} 時發生錯誤: {e}", exc_info=True)
                        raise e # 將內部錯誤向上拋出
                else:
                    retry_count += 1
                    logger.warning(f"分塊 {part_num} 校驗失敗，正在重試... ({retry_count}/{MAX_DOWNLOAD_PART_RETRIES})")
                    os.remove(downloaded_part_path) # 刪除錯誤的檔案
                    await asyncio.sleep(1)
            
            if retry_count == MAX_DOWNLOAD_PART_RETRIES:
                raise ValueError(f"分塊 {part_num} 在 {MAX_DOWNLOAD_PART_RETRIES} 次重試後仍校驗失敗。")
            
            downloaded_bytes += message.document.size

        # --- 3. 最終校驗 ---
        logger.info(f"檔案 '{file_name}' 所有分塊處理完成，開始最終校驗。")
        final_hash = cr.hash_data(final_path)
        if final_hash != file_details['hash']:
            raise ValueError(f"檔案 '{file_name}' 合併後最終校驗失敗！")
        
        logger.info(f"檔案 '{file_name}' 最終校驗成功。")
        if progress_callback:
            progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)

    except asyncio.CancelledError:
        logger.warning(f"下載任務 '{file_name}' (ID: {task_id}) 已被使用者取消。")
        if progress_callback:
            progress_callback(task_id, file_name, 0, 0, 'cancelled', 0)
        # 如果最終檔案已建立，刪除不完整的檔案
        if 'final_path' in locals() and os.path.exists(final_path):
            os.remove(final_path)
            logger.info(f"已刪除因取消而未完成的檔案: {final_path}")
        raise

    except Exception as e:
        logger.error(f"下載檔案 '{file_name}' (task_id: {task_id}) 失敗: {e}", exc_info=True)
        if progress_callback:
            # 根據錯誤類型傳遞更具體的訊息
            msg = str(e) if isinstance(e, (IOError, ValueError, FileNotFoundError)) else "下載時發生未預期錯誤。"
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
        # 如果最終檔案已建立，刪除可能已損壞的檔案
        if 'final_path' in locals() and os.path.exists(final_path):
            os.remove(final_path)
            logger.info(f"已刪除因錯誤而可能損壞的檔案: {final_path}")
        raise
    
    finally:
        # 清理暫存目錄
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理下載後暫存目錄: {temp_dir}")

async def _perform_db_upload(client, group_id, db_path, remote_db_message):
    """執行資料庫的上傳、置頂和清理操作。"""
    logger.info("正在執行強制上傳資料庫模式...")
    if not os.path.exists(db_path):
        logger.error(f"資料庫檔案 '{db_path}' 不存在，無法上傳。")
        return

    temp_zip_path = db_path + ".zip"
    
    try:
        # 壓縮資料庫
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(db_path, os.path.basename(db_path))
        logger.info(f"資料庫已成功壓縮到 '{temp_zip_path}'。")

        # 獲取版本號
        db = DatabaseHandler(db_path)
        version = db.get_db_version()
        caption = f"db_version:{version}"

        # 刪除舊的備份
        if remote_db_message:
            logger.info("正在刪除舊的置頂資料庫備份...")
            try:
                await client.unpin_message(group_id, remote_db_message.id)
            except Exception as e:
                logger.warning(f"取消置頂舊訊息失敗，但不影響流程: {e}")
            try:
                await client.delete_messages(group_id, [remote_db_message.id])
            except Exception as e:
                logger.warning(f"刪除舊訊息失敗: {e}")

        # 上傳新備份並置頂
        new_message = await client.send_file(group_id, file=temp_zip_path, caption=caption)
        await client.pin_message(group_id, new_message.id, notify=False)
        logger.info(f"已上傳並置頂新的資料庫備份 (版本: {version})。")

    except Exception as e:
        logger.error(f"上傳資料庫時發生錯誤: {e}", exc_info=True)
    finally:
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)

async def sync_database_file(client, group_id, mode='sync', db_path='./file/tdrive.db'):
    """
    同步本地與雲端的 tdrive.db 資料庫檔案。
    """
    async with update_lock:
        # 尋找雲端置頂的資料庫備份訊息
        messages = await client.get_messages(group_id, limit=1, filter=InputMessagesFilterPinned)
        remote_db_message = messages[0] if messages else None

        if mode == 'upload':
            await _perform_db_upload(client, group_id, db_path, remote_db_message)
            return

        if mode == 'sync':
            logger.info("正在執行同步資料庫模式...")
            
            # 確保本地資料庫存在
            if not os.path.exists(db_path):
                # 如果本地沒有資料庫，但雲端有，則下載
                if remote_db_message:
                    logger.warning(f"本地資料庫 '{db_path}' 不存在，將從雲端下載。")
                    await client.download_media(remote_db_message, file=db_path + ".zip")
                    with zipfile.ZipFile(db_path + ".zip", 'r') as z:
                        z.extractall(os.path.dirname(db_path))
                    os.remove(db_path + ".zip")
                    logger.info("已成功從雲端恢復資料庫。")
                else:
                    logger.error(f"本地資料庫 '{db_path}' 不存在，且雲端沒有備份，無法同步。")
                return

            db = DatabaseHandler(db_path)
            local_version = db.get_db_version()
            
            remote_version = -1 # 預設為一個無效的舊版本
            
            # 檢查雲端是否有資料庫備份
            if remote_db_message and remote_db_message.text and "db_version:" in remote_db_message.text:
                try:
                    # 從 caption 中解析版本號
                    remote_version = int(remote_db_message.text.split("db_version:", 1)[1].split(',')[0].strip())
                except (ValueError, IndexError):
                    logger.warning("無法從雲端資料庫備份的 caption 中解析版本號。")
            else:
                logger.warning("雲端找不到資料庫備份或備份版本資訊不完整。")

            # 比較版本號
            logger.info(f"本地資料庫版本: {local_version}，雲端資料庫版本: {remote_version}")
            if local_version > remote_version:
                logger.info("本地版本較新，將上傳至雲端。")
                await _perform_db_upload(client, group_id, db_path, remote_db_message)
            
            elif remote_version > local_version:
                logger.info("雲端版本較新，將從雲端下載並覆蓋本地版本。")
                temp_zip_path = db_path + ".zip"
                try:
                    await client.download_media(remote_db_message, file=temp_zip_path)
                    # 先刪除舊的 db，避免解壓縮失敗時殘留
                    if os.path.exists(db_path): os.remove(db_path)
                    with zipfile.ZipFile(temp_zip_path, 'r') as z:
                        z.extractall(os.path.dirname(db_path))
                    logger.info("本地資料庫已成功更新為雲端版本。")
                except Exception as e:
                    logger.error(f"從雲端下載或解壓縮資料庫失敗: {e}", exc_info=True)
                finally:
                    if os.path.exists(temp_zip_path):
                        os.remove(temp_zip_path)

            else: # local_version == remote_version
                logger.info("本地與雲端資料庫版本一致，無需同步。")
                return
