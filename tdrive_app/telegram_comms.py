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
    temp_dir = f".\\temp_upload_{os.path.basename(file_path)}_{task_id}"
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
    
    temp_dir = f".\\temp_download_{task_id}"
    
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

        for message in messages_to_download:
            if part_info_map[message.id].get('downloaded'):
                continue

            last_call_bytes = 0
            expected_part_hash = part_info_map[message.id]["hash"]
            
            while True:
                downloaded_part_path = await message.download_media(file=temp_dir, progress_callback=callback)
                if not downloaded_part_path or not os.path.exists(downloaded_part_path): continue

                actual_part_hash = cr.hash_data(downloaded_part_path)
                if actual_part_hash == expected_part_hash:
                    logger.debug(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 下載完成並通過校驗。")
                    break
                else:
                    logger.warning(f"檔案 '{original_file_name}' 的分塊 {part_info_map[message.id]['num']} 校驗失敗，正在重試下載...")
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

        # 計算 Hash
        local_db_hash = cr.hash_data(db_path)
        caption = f"db_hash:{local_db_hash}"

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
        logger.info("已上傳並置頂新的資料庫備份。")

    except Exception as e:
        logger.error(f"上傳資料庫時發生錯誤: {e}", exc_info=True)
    finally:
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)


async def sync_database_file(client, group_id, mode='sync', db_path='./file/tdrive.db'):
    """
    同步本地與雲端的 tdrive.db 資料庫檔案。

    :param client: Telegram 客戶端實例。
    :param group_id: TDrive 的儲存群組 ID。
    :param mode: 操作模式，可以是 'sync' (同步) 或 'upload' (強制上傳)。
    :param db_path: 本地資料庫檔案的路徑。
    """
    
    async with update_lock:
        # 尋找雲端置頂的資料庫備份訊息
        messages = await client.get_messages(group_id, limit=1, filter=InputMessagesFilterPinned)
        remote_db_message = None
        if messages:
            remote_db_message = messages[0]

        if mode == 'upload':
            await _perform_db_upload(client, group_id, db_path, remote_db_message)

        elif mode == 'sync':
            logger.info("正在執行同步資料庫模式...")
            
            # 確保本地資料庫存在
            if not os.path.exists(db_path):
                logger.error(f"本地資料庫檔案 '{db_path}' 不存在，無法進行同步。")
                return

            db = DatabaseHandler(db_path)
            local_db_hash = cr.hash_data(db_path)
            local_mod_time = db.get_modification_time()
            
            remote_db_hash = None
            
            # 檢查雲端是否有資料庫備份
            if remote_db_message and remote_db_message.media:
                # 從 caption 中解析 Hash
                if remote_db_message.text and remote_db_message.text.startswith("db_hash:"):
                    remote_db_hash = remote_db_message.text.split("db_hash:", 1)[1].strip()
                else:
                    logger.warning("雲端資料庫備份訊息的 caption 中未找到有效的 Hash。")
            
            # 比較 Hash
            if remote_db_hash and local_db_hash == remote_db_hash:
                logger.info("本地與雲端資料庫 Hash 一致，無需同步。")
                return

            logger.warning("本地與雲端資料庫 Hash 不一致，進行比對及同步...")
            
            temp_cloud_zip_path = os.path.join(os.path.dirname(db_path), f"temp_tdrive_cloud_{int(time.time())}.zip")
            temp_cloud_db_path = os.path.join(os.path.dirname(db_path), f"temp_tdrive_cloud_{int(time.time())}.db")

            try:
                # 如果雲端有備份但 Hash 不一致，則下載雲端版本
                if remote_db_message and remote_db_hash:
                    logger.info("正在下載雲端資料庫備份進行比對...")
                    await client.download_media(remote_db_message, file=temp_cloud_zip_path)
                    
                    if os.path.exists(temp_cloud_zip_path):
                        with zipfile.ZipFile(temp_cloud_zip_path, 'r') as z:
                            # 確保解壓縮到指定臨時路徑
                            z.extract(os.path.basename(db_path), os.path.dirname(temp_cloud_db_path))
                        # 將解壓縮後的檔案重新命名為我們預期的臨時名稱
                        extracted_db_name = os.path.join(os.path.dirname(temp_cloud_db_path), os.path.basename(db_path))
                        os.rename(extracted_db_name, temp_cloud_db_path)

                        logger.info(f"雲端資料庫備份已下載並解壓縮到 '{temp_cloud_db_path}'。")
                        
                        temp_db_instance = DatabaseHandler(db_path=temp_cloud_db_path)
                        cloud_db_mod_time = temp_db_instance.get_modification_time()
                        
                        if cloud_db_mod_time is None:
                            logger.warning(f"無法獲取雲端副本 '{temp_cloud_db_path}' 的修改時間，將以本地為準進行上傳。")
                            await _perform_db_upload(client, group_id, db_path, remote_db_message)
                            return
                        
                        # 比較時間戳
                        if local_mod_time is None or cloud_db_mod_time > local_mod_time:
                            logger.info(f"雲端資料庫 ({cloud_db_mod_time}) 較新，將替換本地資料庫 ({local_mod_time})。")
                            shutil.copy(temp_cloud_db_path, db_path)
                            logger.info("本地資料庫已更新為雲端最新版本。")
                        else:
                            logger.info(f"本地資料庫 ({local_mod_time}) 較新或相同，將上傳本地版本。")
                            await _perform_db_upload(client, group_id, db_path, remote_db_message)
                    else:
                        logger.error("從 Telegram 下載雲端資料庫備份失敗。將上傳本地資料庫。")
                        await _perform_db_upload(client, group_id, db_path, remote_db_message)
                else:
                    logger.warning("雲端沒有資料庫備份或無法解析，將上傳本地資料庫。")
                    await _perform_db_upload(client, group_id, db_path, remote_db_message)

            except Exception as e:
                logger.error(f"同步資料庫時發生錯誤: {e}", exc_info=True)
            finally:
                if os.path.exists(temp_cloud_zip_path):
                    os.remove(temp_cloud_zip_path)
                if os.path.exists(temp_cloud_db_path):
                    os.remove(temp_cloud_db_path)
