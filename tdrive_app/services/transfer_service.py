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

class TransferService:
    """
    Manages all time-consuming file upload and download tasks.

    This service handles queuing, concurrent execution using a semaphore,
    progress reporting, and cancellation of transfer tasks.
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state
        self.db = DatabaseHandler()

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], concurrency_limit: int, progress_callback: Callable):
        """
        Entry point for file uploads. Creates and manages a pool of upload workers.
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Upload cannot start: client is not connected.")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗，無法開始上傳。")
            return
        
        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)
        
        tasks_to_run = [
            self._upload_single_file(client, group_id, parent_id, item['local_path'], item['task_id'], semaphore, progress_callback)
            for item in upload_items
        ]
        await asyncio.gather(*tasks_to_run, return_exceptions=True)

    async def _upload_single_file(self, client, group_id: int, parent_id: int, file_path: str, task_id: str, semaphore: asyncio.Semaphore, progress_callback: Callable):
        """
        The core worker coroutine for uploading a single file.
        """
        file_name = os.path.basename(file_path)
        
        async with semaphore:
            # Register the task for cancellation handling.
            try:
                current_task = asyncio.current_task()
                self.shared_state.active_tasks[task_id] = current_task
            except RuntimeError:
                logger.warning(f"Could not get current task for task_id: {task_id}. Cancellation may not be possible.")

            client = await utils.ensure_client_connected(self.shared_state)
            if not client or not os.path.exists(file_path):
                msg = "用戶端已斷線或本機檔案不存在。"
                logger.warning(f"Upload task '{file_name}' failed: {msg}")
                progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
                return

            total_size = os.path.getsize(file_path)
            
            try:
                # Check for existing item with the same name.
                folder_contents = self.db.get_folder_contents(parent_id)
                if any(f['name'] == file_name for f in folder_contents['files']) or \
                   any(f['name'] == file_name for f in folder_contents['folders']):
                    raise errors.ItemAlreadyExistsError(f"目標位置已存在名為 '{file_name}' 的項目。")

                # Check for content hash collision (for "instant" upload).
                original_file_hash = crypto_handler.hash_data(file_path)
                if existing_file_obj := self.db.find_file_by_hash(original_file_hash):
                    logger.info(f"Identical content found for '{file_name}'. Creating metadata entry only.")
                    self.db.add_file(parent_id, file_name, existing_file_obj["size"], existing_file_obj["hash"], time.time(), existing_file_obj["split_files"])
                    progress_callback(task_id, file_name, total_size, total_size, 'completed', 0, message="秒傳成功")
                    await utils.trigger_db_upload_in_background(self.shared_state)
                    return

                progress_callback(task_id, file_name, 0, total_size, 'transferring', 0)
                split_files_info = await telegram_comms.upload_file_with_info(client, group_id, file_path, task_id, progress_callback)
                
                self.db.add_file(parent_id, file_name, total_size, original_file_hash, time.time(), split_files_info)
                await utils.trigger_db_upload_in_background(self.shared_state)

            except asyncio.CancelledError:
                logger.warning(f"Upload task '{file_name}' (ID: {task_id}) was cancelled by the user.")
                progress_callback(task_id, file_name, 0, total_size, 'cancelled', 0)
            except errors.ItemAlreadyExistsError as e:
                logger.warning(f"Upload for '{file_name}' failed: {e}")
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message=str(e))
            except Exception as e:
                logger.error(f"An unexpected error occurred while uploading '{file_name}'.", exc_info=True)
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message="發生未知的內部錯誤。")
            finally:
                if task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[task_id]

    async def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Callable):
        """
        Entry point for downloads. Creates and manages a pool of download workers.
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Download cannot start: client is not connected.")
            for item in items:
                temp_task_id = f"dl_{uuid.uuid4()}"
                progress_callback(temp_task_id, item.get('name', "Unknown"), 0, 0, 'failed', 0, message="連線失敗。")
            return

        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)

        tasks = [
            self._download_single_item(client, group_id, item['task_id'], item, destination_dir, semaphore, progress_callback)
            for item in items
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _download_single_item(self, client, group_id: int, main_task_id: str, item: Dict, dest_path: str, semaphore: asyncio.Semaphore, progress_callback: Callable):
        """
        The core worker coroutine for downloading a single item, which can be
        either a file or a folder.
        """
        item_db_id, item_type, item_name = item['db_id'], item['type'], item['name']
        
        # Register the main task for cancellation.
        try:
            self.shared_state.active_tasks[main_task_id] = asyncio.current_task()
        except RuntimeError:
             logger.warning(f"Could not get current task for folder download (ID: {main_task_id}). Cancellation may not work.")

        try:
            async with semaphore:
                client = await utils.ensure_client_connected(self.shared_state)
                if not client:
                    progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message="連線失敗。")
                    return
                
                progress_callback(main_task_id, item_name, 0, item.get('size',0), 'queued', 0)

                if item_type == 'file':
                    file_details = self.db.get_file_details(item_db_id)
                    if not file_details: raise errors.PathNotFoundError(f"資料庫中找不到 ID 為 {item_db_id} 的檔案。")
                    await self._download_file_from_details(client, group_id, main_task_id, file_details, dest_path, progress_callback)
                
                elif item_type == 'folder':
                    await self._download_folder(client, group_id, main_task_id, item, dest_path, progress_callback)

        except asyncio.CancelledError:
            logger.warning(f"Download task for '{item_name}' (ID: {main_task_id}) was cancelled.")
            progress_callback(main_task_id, item_name, 0, 0, 'cancelled', 0)
        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to download '{item_name}': {e}")
            progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message=str(e))
        except Exception as e:
            logger.error(f"Unexpected error while processing download for '{item_name}'.", exc_info=True)
            progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message="發生未知的內部錯誤。")
        finally:
            if main_task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[main_task_id]

    async def _download_folder(self, client, group_id: int, main_task_id: str, folder_item: Dict, dest_path: str, progress_callback: Callable):
        """Helper method to handle the logic for downloading a folder."""
        folder_contents = self.db.get_folder_contents_recursive(folder_item['db_id'])
        if not folder_contents:
            raise errors.PathNotFoundError(f"資料庫中找不到 ID 為 {folder_item['db_id']} 的資料夾。")
        
        actual_folder_name = folder_contents.get('folder_name', folder_item['name'])
        local_root_path = os.path.join(dest_path, actual_folder_name)
        os.makedirs(local_root_path, exist_ok=True)
        
        files_in_folder = [f for f in folder_contents['items'] if f['type'] == 'file']
        
        # Create subdirectories and prepare child item info for the UI.
        child_info_for_frontend = []
        for f_or_d in folder_contents['items']:
            item_task_id = f"dl_{uuid.uuid4()}"
            child_info = {**f_or_d, 'id': item_task_id}
            child_info_for_frontend.append(child_info)
            if f_or_d['type'] == 'folder':
                os.makedirs(os.path.join(local_root_path, f_or_d['relative_path']), exist_ok=True)

        total_size = sum(f['size'] for f in files_in_folder)
        progress_callback(main_task_id, actual_folder_name, 0, total_size, 'starting_folder', 0, 
                          total_files=len(files_in_folder), children=child_info_for_frontend)

        # Create download tasks for all files within the folder.
        download_tasks = [
            self._download_file_from_details(
                client, group_id, child['id'], self.db.get_file_details(child['db_id']), 
                os.path.join(local_root_path, os.path.dirname(child['relative_path'])), 
                progress_callback, parent_task_id=main_task_id
            ) for child in child_info_for_frontend if child['type'] == 'file'
        ]
        
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        
        # Determine the final status of the folder download.
        has_failures = any(isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError) for res in results)
        was_cancelled = any(isinstance(res, asyncio.CancelledError) for res in results)

        if was_cancelled:
            logger.warning(f"Download of folder '{folder_item['name']}' was partially or fully cancelled.")
            progress_callback(main_task_id, actual_folder_name, 0, total_size, 'cancelled', 0)
        elif has_failures:
            logger.error(f"Some files failed to download for folder '{folder_item['name']}'.")
            progress_callback(main_task_id, actual_folder_name, 0, total_size, 'failed', 0, message="部分檔案下載失敗。")
        else:
            progress_callback(main_task_id, actual_folder_name, total_size, total_size, 'completed', 0)

    async def _download_file_from_details(self, client, group_id: int, task_id: str, file_details: Dict, destination: str, progress_callback: Callable, parent_task_id: str = None):
        """Helper coroutine to download a single file given its full details."""
        file_name = file_details['name']
        try:
            progress_callback(task_id, file_name, 0, file_details.get("size", 0), 'transferring', 0, parent_task_id=parent_task_id)
            
            # Create and register the cancellable download task.
            coro = telegram_comms.download_file(
                client, group_id, file_details, destination,
                task_id=task_id, progress_callback=progress_callback
            )
            task = asyncio.create_task(coro)
            self.shared_state.active_tasks[task_id] = task
            await task
        except asyncio.CancelledError:
            logger.warning(f"Download task '{file_name}' (ID: {task_id}) was cancelled.")
            progress_callback(task_id, file_name, 0, 0, 'cancelled', 0, parent_task_id=parent_task_id)
            # Re-raise to notify the parent gather().
            raise
        except Exception as e:
            logger.error(f"Unexpected error while downloading '{file_name}' (ID: {task_id}).", exc_info=True)
            progress_callback(task_id, file_name, 0, 0, 'failed', 0, message="發生未知的錯誤。", parent_task_id=parent_task_id)
            # Re-raise to notify the parent gather().
            raise
        finally:
            if task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[task_id]

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        """
        Requests the cancellation of an active transfer task.
        """
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            # Schedule the cancellation on the main event loop's thread.
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
            logger.info(f"Cancellation requested for task {task_id}.")
            return {"success": True, "message": f"Cancellation requested for task {task_id}."}
        
        logger.warning(f"Could not cancel task {task_id}: task not found or already completed.")
        return {"success": False, "message": "任務找不到或已完成。"}