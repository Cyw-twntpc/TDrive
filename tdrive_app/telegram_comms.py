from telethon.tl.types import DocumentAttributeFilename, InputMessagesFilterPinned
from telethon.tl.functions.messages import CreateChatRequest, EditChatAboutRequest
from telethon.tl.functions.channels import CreateChannelRequest
import os
import shutil
import zipfile
import time
import asyncio
import json

import logging # 匯入 logging 模組
from . import crypto_handler as cr
from . import file_processor as fp
from .db_handler import DatabaseHandler
from .shared_state import TEMP_DIR

logger = logging.getLogger(__name__) # 取得 logger 實例

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
    上傳檔案，並返回所有分塊的資訊（ID, hash 等）。
    不再檢查遠端檔案，不再寫入 caption。
    """
    temp_dir = os.path.join(TEMP_DIR, f"temp_upload_{os.path.basename(file_path)}_{task_id}")
    file_name = os.path.basename(file_path)
    split_files_info = [] # 用於儲存返回的分塊資訊
    
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理舊的暫存上傳目錄: {temp_dir}")
        os.makedirs(temp_dir, exist_ok=True)
        
        original_file_hash = cr.hash_data(file_path)
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        
        # 1. 產生所有本地分塊
        temp_parts = fp.split_file(file_path, key, original_file_hash, temp_dir)
        
        total_size = os.path.getsize(file_path)
        uploaded_bytes = 0

        # 定義進度回呼函式
        last_update_time = 0
        last_call_bytes = 0
        def callback(current, total):
            nonlocal uploaded_bytes, last_update_time, last_call_bytes
            now = time.time()
            if now - last_update_time > 0.5 or current == total:
                elapsed = now - last_update_time # 修正錯誤的公式
                speed = (current - last_call_bytes) / elapsed if elapsed > 0 else 0
                last_call_bytes = current
                last_update_time = now
                
                current_total_progress = uploaded_bytes + current
                
                if progress_callback:
                    progress_callback(task_id, file_name, current_total_progress, total_size, 'transferring', speed)

        # 2. 迭代上傳所有分塊
        for i, part_path in enumerate(temp_parts):
            part_hash = cr.hash_data(part_path)
            part_num = i + 1
            last_call_bytes = 0
            
            # 上傳檔案，不帶 caption
            message = await client.send_file(
                group_id,
                file=part_path,
                progress_callback=callback
            )
            
            # 收集分塊資訊
            split_files_info.append([part_num, message.id, part_hash])
            uploaded_bytes += os.path.getsize(part_path)
            
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
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理上傳後暫存目錄: {temp_dir}")


async def search_messages_by_filename_prefixes(client, group_id, prefixes):
    """根據檔名前綴搜尋訊息ID。"""
    message_ids = []
    prefixes_set = set(prefixes)
    async for message in client.iter_messages(entity=group_id, limit=None):
        if not (message.document and hasattr(message.document, 'attributes')):
            continue
        
        for attribute in message.document.attributes:
            if isinstance(attribute, DocumentAttributeFilename):
                for prefix in prefixes_set:
                    if attribute.file_name.startswith(prefix):
                        message_ids.append(message.id)
                        break 
    return message_ids


async def download_file(client, group_id, split_files_info, original_file_name, original_file_hash, download_dir, task_id, progress_callback=None):
    """
    根據分塊資訊列表（包含 message_id），下載所有分割檔，校驗並合併。
    """
    part_info_map = {part[1]: {"num": part[0], "hash": part[2]} for part in split_files_info}
    message_ids = list(part_info_map.keys())
    
    temp_dir = os.path.join(TEMP_DIR, f"temp_download_{task_id}")
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
        logger.info(f"開始下載檔案 '{original_file_name}' (task_id: {task_id})。")

        messages_to_download = await client.get_messages(group_id, ids=message_ids)
        messages_to_download = [m for m in messages_to_download if m]
        
        if len(messages_to_download) != len(split_files_info):
            logger.error(f"檔案 '{original_file_name}' 的部分分塊在雲端遺失。")
            raise FileNotFoundError(f"檔案 '{original_file_name}' 的部分分塊在雲端遺失。")

        total_size = sum(m.document.size for m in messages_to_download if m.document)
        
        downloaded_bytes = 0
        local_parts_by_name = {os.path.basename(p): p for p in fp.find_part_files(temp_dir)}

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
                    progress_callback(task_id, original_file_name, downloaded_bytes + current, total_size, 'transferring', speed)

        for message in messages_to_download:
            part_num = part_info_map[message.id]["num"]
            expected_part_hash = part_info_map[message.id]["hash"]
            part_filename = f"{original_file_hash[:20]}.part_{part_num}"

            if part_filename in local_parts_by_name:
                part_path = local_parts_by_name[part_filename]
                if os.path.exists(part_path) and cr.hash_data(part_path) == expected_part_hash:
                    downloaded_bytes += os.path.getsize(part_path)
                    part_info_map[message.id]['downloaded'] = True
                    logger.debug(f"檔案 '{original_file_name}' 的分塊 {part_num} 已存在並通過校驗。")

        MAX_DOWNLOAD_PART_RETRIES = 3 # 設定最大重試次數

        for message in messages_to_download:
            if part_info_map[message.id].get('downloaded'):
                continue

            last_call_bytes = 0
            expected_part_hash = part_info_map[message.id]["hash"]
            retry_count = 0 # 初始化重試計數器
            
            while retry_count < MAX_DOWNLOAD_PART_RETRIES: # 限制重試次數
                downloaded_part_path = await message.download_media(file=temp_dir, progress_callback=callback)
                if not downloaded_part_path or not os.path.exists(downloaded_part_path):
                    retry_count += 1
                    logger.warning(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 下載失敗或檔案不存在，正在重試... ({retry_count}/{MAX_DOWNLOAD_PART_RETRIES})")
                    await asyncio.sleep(1) # 等待一秒再重試
                    continue

                actual_part_hash = cr.hash_data(downloaded_part_path)
                if actual_part_hash == expected_part_hash:
                    logger.debug(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 下載完成並通過校驗。")
                    break # 校驗成功，跳出重試迴圈
                else:
                    retry_count += 1
                    logger.warning(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 校驗失敗，正在重試下載... ({retry_count}/{MAX_DOWNLOAD_PART_RETRIES})")
                    await asyncio.sleep(1) # 等待一秒再重試
            
            if retry_count == MAX_DOWNLOAD_PART_RETRIES:
                logger.error(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 在 {MAX_DOWNLOAD_PART_RETRIES} 次重試後仍校驗失敗，視為損壞。")
                raise ValueError(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 損壞，無法下載。")
            downloaded_bytes += message.document.size

        logger.info(f"檔案 '{original_file_name}' (task_id: {task_id}) 所有分塊下載完成，開始合併。")
        sorted_part_files = fp.find_part_files(temp_dir)
        if not sorted_part_files:
            logger.error(f"找不到任何有效的檔案部分來合併檔案 '{original_file_name}'。")
            raise FileNotFoundError(f"找不到任何有效的檔案部分來合併檔案 '{original_file_name}'。")

        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        final_path = os.path.join(download_dir, original_file_name)
        fp.merge_files(sorted_part_files, final_path, key)
        logger.info(f"檔案 '{original_file_name}' 合併完成。")

        final_hash = cr.hash_data(final_path)
        if final_hash != original_file_hash:
            logger.error(f"檔案 '{original_file_name}' 合併後最終校驗失敗！")
            raise ValueError(f"檔案 '{original_file_name}' 合併後最終校驗失敗！")
        logger.info(f"檔案 '{original_file_name}' 最終校驗成功。")

        if progress_callback:
            progress_callback(task_id, original_file_name, total_size, total_size, 'completed', 0)
        
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理下載後暫存目錄: {temp_dir}")

    except asyncio.CancelledError:
        logger.warning(f"下載任務 '{original_file_name}' (ID: {task_id}) 已被使用者取消。")
        if progress_callback:
            progress_callback(task_id, original_file_name, 0, 0, 'cancelled', 0)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"清理下載後暫存目錄: {temp_dir}")
        raise

    except Exception as e:
        logger.error(f"下載檔案 '{original_file_name}' (task_id: {task_id}) 失敗: {e}", exc_info=True)
        if progress_callback:
            progress_callback(task_id, original_file_name, 0, 0, 'failed', 0)
        raise e


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
