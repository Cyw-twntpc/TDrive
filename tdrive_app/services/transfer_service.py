import logging
import asyncio
import os
import uuid
import time
from typing import TYPE_CHECKING, List, Dict, Any, Callable

if TYPE_CHECKING:
    from ..shared_state import SharedState

from . import utils
from .. import telegram_comms, crypto_handler, errors
from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)
db = DatabaseHandler()

class TransferService:
    """
    處理所有耗時的檔案上傳與下載任務。
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    # --- 上傳邏輯 ---
    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], concurrency_limit: int, progress_callback: Callable):
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("上傳無法啟動：連線無效。")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗，無法啟動上傳。")
            return
        
        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)
        
        tasks_to_run = [
            self._upload_single_file(client, group_id, parent_id, item['local_path'], item['task_id'], semaphore, progress_callback)
            for item in upload_items
        ]
        await asyncio.gather(*tasks_to_run, return_exceptions=True)

    async def _upload_single_file(self, client, group_id, parent_id, file_path, task_id: str, semaphore, progress_callback):
        file_name = os.path.basename(file_path)
        
        async with semaphore:
            try:
                # 取得由 asyncio.gather() 建立的當前任務，並註冊以便取消
                current_task = asyncio.current_task()
                self.shared_state.active_tasks[task_id] = current_task
            except RuntimeError:
                logger.warning(f"無法取得當前任務 (Task ID: {task_id})。此任務可能無法被取消。")

            client = await utils.ensure_client_connected(self.shared_state)
            if not client or not os.path.exists(file_path):
                msg = "連線無效或本地檔案不存在。"
                logger.warning(f"上傳任務 '{file_name}' 失敗：{msg}")
                progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
                return

            total_size = os.path.getsize(file_path)
            
            try:
                if any(f['name'] == file_name for f in db.get_folder_contents(parent_id)['files']) or \
                   any(f['name'] == file_name for f in db.get_folder_contents(parent_id)['folders']):
                    raise errors.ItemAlreadyExistsError(f"目標資料夾中已存在同名項目。")

                original_file_hash = crypto_handler.hash_data(file_path)
                if existing_file_obj := db.find_file_by_hash(original_file_hash):
                    logger.info(f"上傳檔案 '{file_name}' 時發現重複內容，將複製元資料。")
                    db.add_file(parent_id, file_name, existing_file_obj["size"], existing_file_obj["hash"], time.time(), existing_file_obj["split_files"])
                    progress_callback(task_id, file_name, total_size, total_size, 'completed', 0)
                    await utils.trigger_db_upload_in_background(self.shared_state)
                    return

                progress_callback(task_id, file_name, 0, total_size, 'transferring', 0)
                split_files_info = await telegram_comms.upload_file_with_info(client, group_id, file_path, task_id, progress_callback)
                
                db.add_file(parent_id, file_name, total_size, original_file_hash, time.time(), split_files_info)
                await utils.trigger_db_upload_in_background(self.shared_state)

            except asyncio.CancelledError:
                logger.warning(f"上傳任務 '{file_name}' (ID: {task_id}) 已被使用者取消。")
                progress_callback(task_id, file_name, 0, total_size, 'cancelled', 0)
            except errors.ItemAlreadyExistsError as e:
                logger.warning(f"上傳檔案 '{file_name}' 失敗: {e}")
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message=str(e))
            except Exception as e:
                logger.error(f"上傳檔案 '{file_name}' 時發生未預期錯誤。", exc_info=True)
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message="發生未預期的內部錯誤。")
            finally:
                if task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[task_id]

    # --- 下載邏輯 (重構後) ---
    async def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Callable):
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("下載無法啟動：連線無效。")
            for item in items:
                # 即使啟動失敗，也為每個項目發送失敗狀態
                temp_task_id = f"dl_{uuid.uuid4()}"
                progress_callback(temp_task_id, item.get('name', "未知名稱"), 0, 0, 'failed', 0, message="連線失敗，無法啟動下載。")
            return

        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)

        tasks = []
        for item in items:
            coro = self._download_single_item(client, group_id, item['task_id'], item, destination_dir, semaphore, progress_callback)
            task = asyncio.create_task(coro)
            # Register the main task (for both files and folders) to make it cancellable
            self.shared_state.active_tasks[item['task_id']] = task
            tasks.append(task)
            
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _download_single_item(self, client, group_id, main_task_id: str, item: Dict, dest_path: str, semaphore, progress_callback):
        # 使用 db_id 查詢資料庫，而不是舊的 id
        item_db_id, item_type, item_name = item['db_id'], item['type'], item['name']
        
        try:
            async with semaphore:
                client = await utils.ensure_client_connected(self.shared_state)
                if not client:
                    progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message="連線無效。")
                    return
                
                # 後端主動發送初始的 'queued' 狀態
                progress_callback(main_task_id, item_name, 0, item.get('size',0), 'queued', 0)

                if item_type == 'file':
                    file_details = db.get_file_details(item_db_id)
                    if not file_details: raise errors.PathNotFoundError(f"在資料庫中找不到檔案 ID {item_db_id}")
                    await self._download_file_from_details(client, group_id, main_task_id, file_details, dest_path, progress_callback)
                
                elif item_type == 'folder':
                    folder_contents = db.get_folder_contents_recursive(item_db_id)
                    if not folder_contents: raise errors.PathNotFoundError(f"在資料庫中找不到資料夾 ID {item_db_id}")
                    
                    actual_folder_name = folder_contents.get('folder_name', item_name)
                    local_root_path = os.path.join(dest_path, actual_folder_name)
                    os.makedirs(local_root_path, exist_ok=True)
                    
                    files_in_folder = []
                    child_info_for_frontend = []
                    for f in folder_contents['items']:
                        # Assign a unique ID for each item in the transfer UI
                        item_task_id = f"dl_{uuid.uuid4()}"
                        
                        child_info = {
                            'id': item_task_id,
                            'db_id': f['id'],
                            'name': f['name'],
                            'size': f.get('size', 0),
                            'relative_path': f['relative_path'],
                            'type': f['type']
                        }
                        child_info_for_frontend.append(child_info)

                        if f['type'] == 'file':
                            files_in_folder.append(child_info) # Use the enriched dict
                        elif f['type'] == 'folder':
                            # Create local directory for the subfolder
                            os.makedirs(os.path.join(local_root_path, f['relative_path']), exist_ok=True)
                    
                    total_size = sum(f['size'] for f in files_in_folder)
                    total_files = len(files_in_folder)

                    progress_callback(main_task_id, actual_folder_name, 0, total_size, 'starting_folder', 0, 
                                      total_files=total_files, children=child_info_for_frontend)

                    download_tasks = []
                    for f_details in files_in_folder:
                        file_dest_path = os.path.join(local_root_path, os.path.dirname(f_details['relative_path']))
                        download_tasks.append(
                            self._download_file_from_details(client, group_id, f_details['id'], db.get_file_details(f_details['db_id']), file_dest_path, progress_callback, parent_task_id=main_task_id)
                        )
                    
                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                    
                    # Check for failures and cancellations
                    has_failures = any(isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError) for res in results)
                    was_cancelled = any(isinstance(res, asyncio.CancelledError) for res in results)

                    if was_cancelled:
                        logger.warning(f"資料夾 '{item_name}' (ID: {main_task_id}) 的部分或全部下載已被取消。")
                        progress_callback(main_task_id, actual_folder_name, 0, total_size, 'cancelled', 0, message="下載已取消。")
                    elif has_failures:
                        logger.error(f"資料夾 '{item_name}' (ID: {main_task_id}) 的部分檔案下載失敗。")
                        progress_callback(main_task_id, actual_folder_name, 0, total_size, 'failed', 0, message="部分檔案下載失敗。")
                    else:
                        progress_callback(main_task_id, actual_folder_name, total_size, total_size, 'completed', 0)

        except asyncio.CancelledError:
            logger.warning(f"資料夾下載任務 '{item_name}' (ID: {main_task_id}) 已被取消。")
            progress_callback(main_task_id, item_name, 0, 0, 'cancelled', 0, message="下載已取消。")
        except errors.PathNotFoundError as e:
            logger.warning(f"下載項目 '{item_name}' 失敗: {e}")
            progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message=str(e))
        except Exception as e:
            logger.error(f"處理下載項目 '{item_name}' 時發生未預期錯誤。", exc_info=True)
            progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message="處理下載時發生未預期錯誤。")
        finally:
            # Always ensure the main task is deregistered when it's finished
            if main_task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[main_task_id]

    async def _download_file_from_details(self, client, group_id, task_id: str, file_details: Dict, destination: str, progress_callback: Callable, parent_task_id: str = None):
        file_name = file_details['name']
        try:
            progress_callback(task_id, file_name, 0, file_details.get("size", 0), 'transferring', 0, parent_task_id=parent_task_id)
            
            coro = telegram_comms.download_file(
                client, group_id, file_details, destination,
                task_id=task_id, progress_callback=progress_callback
            )
            task = asyncio.create_task(coro)
            self.shared_state.active_tasks[task_id] = task
            await task
        except asyncio.CancelledError:
            logger.warning(f"下載任務 '{file_name}' (ID: {task_id}) 已被使用者取消。")
            progress_callback(task_id, file_name, 0, 0, 'cancelled', 0, parent_task_id=parent_task_id)
        except Exception as e:
            logger.error(f"下載檔案 '{file_name}' (ID: {task_id}) 時發生未預期錯誤。", exc_info=True)
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, message="下載時發生未預期錯誤。", parent_task_id=parent_task_id)
        finally:
            if task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[task_id]

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
            return {"success": True, "message": f"任務 {task_id} 已請求取消。"}
        return {"success": False, "message": "任務不存在或已完成。"}