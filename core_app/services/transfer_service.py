import logging
import asyncio
import os
import uuid
import time
from typing import TYPE_CHECKING, List, Dict, Any, Callable, Optional

if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState

from . import utils
from .transfer_controller import TransferController
from core_app.api import telegram_comms, crypto_handler
from core_app.common import errors
from core_app.data.db_handler import DatabaseHandler
from core_app.services.monitor_service import TransferMonitorService
from core_app.api.file_processor import CHUNK_SIZE

logger = logging.getLogger(__name__)

class TransferService:
    """
    Manages all time-consuming file upload and download tasks.
    Supports Resume (斷點續傳), Pause, and Cancel operations via TransferController.
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state
        self.db = DatabaseHandler()
        self.monitor = TransferMonitorService()
        self.controller = TransferController()
        
        # Global semaphore for resumed tasks to prevent flooding
        self._resume_semaphore = asyncio.Semaphore(3)

        # Reset any tasks that were 'transferring' when the app crashed
        self.controller.reset_zombie_tasks()

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], concurrency_limit: int, progress_callback: Callable):
        """
        Entry point for new file uploads.
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Upload cannot start: client is not connected.")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗，無法開始上傳。")
            return
        
        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)
        
        tasks_to_run = []
        for item in upload_items:
            tasks_to_run.append(
                self._upload_single_file(client, group_id, parent_id, item['local_path'], item['task_id'], semaphore, progress_callback)
            )
            
        await asyncio.gather(*tasks_to_run, return_exceptions=True)
        self.monitor.close()

    async def _upload_single_file(self, client, group_id: int, parent_id: int, file_path: str, task_id: str, 
                                  semaphore: asyncio.Semaphore, progress_callback: Callable, 
                                  resume_context: List = None, pre_calculated_hash: str = None):
        """
        Worker for uploading. Supports both fresh uploads and resumes.
        """
        file_name = os.path.basename(file_path)
        
        # Define chunk callback for real-time state saving
        def chunk_cb(part_num, msg_id, part_hash):
            self.controller.update_progress(task_id, part_num, [part_num, msg_id, part_hash])

        async with semaphore:
            try:
                current_task = asyncio.current_task()
                self.shared_state.active_tasks[task_id] = current_task
            except RuntimeError:
                logger.warning(f"Could not get current task for task_id: {task_id}.")

            client = await utils.ensure_client_connected(self.shared_state)
            if not client or not os.path.exists(file_path):
                msg = "用戶端已斷線或本機檔案不存在。"
                self.controller.mark_failed(task_id, msg)
                progress_callback(task_id, file_name, 0, 0, 'failed', 0, message=msg)
                return

            total_size = os.path.getsize(file_path)
            loop = asyncio.get_running_loop()

            try:
                original_file_hash = pre_calculated_hash
                split_files_info = []

                if resume_context:
                    # --- RESUME PATH ---
                    logger.info(f"Resuming upload for {file_name}...")
                    
                    # [Removed Chart Logic] No longer need to pre-calculate for monitor baseline.
                    # The progress_callback will handle the initial progress.
                    if not original_file_hash:
                        logger.warning("Resume requested but hash missing. Re-calculating...")
                        original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    
                    # Skip DB checks for resume
                    split_files_info = await telegram_comms.upload_file_with_info(
                        client, group_id, file_path, original_file_hash, task_id, progress_callback,
                        resume_context=resume_context,
                        chunk_callback=chunk_cb, 
                        update_transferred_bytes=self.monitor.update_transferred_bytes
                    )
                
                else:
                    # --- FRESH UPLOAD PATH ---
                    # 1. DB Check (Name collision)
                    folder_contents = await loop.run_in_executor(None, self.db.get_folder_contents, parent_id)
                    if any(f['name'] == file_name for f in folder_contents['files']):
                        raise errors.ItemAlreadyExistsError(f"目標位置已存在名為 '{file_name}' 的項目。")

                    # 2. Hash Calculation
                    original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    
                    # Update Controller with Hash immediately
                    self.controller.add_upload_task(task_id, file_path, parent_id, total_size, original_file_hash, [])

                    # 3. Deduplication (Sec-Upload)
                    existing_file_obj = await loop.run_in_executor(None, self.db.find_file_by_hash, original_file_hash)
                    
                    if existing_file_obj:
                        logger.info(f"Identical content found for '{file_name}'. Creating metadata entry only.")
                        await loop.run_in_executor(
                            None, 
                            lambda: self.db.add_file(
                                parent_id, file_name, existing_file_obj["size"], 
                                existing_file_obj["hash"], time.time(), existing_file_obj["split_files"]
                            )
                        )
                        progress_callback(task_id, file_name, total_size, total_size, 'completed', 0, message="秒傳成功")
                        self.controller.remove_task(task_id) # Done
                        await utils.trigger_db_upload_in_background(self.shared_state)
                        return

                    progress_callback(task_id, file_name, 0, total_size, 'transferring', 0)
                    
                    # 4. Actual Upload
                    split_files_info = await telegram_comms.upload_file_with_info(
                        client, group_id, file_path, original_file_hash, task_id, progress_callback,
                        chunk_callback=chunk_cb, 
                        update_transferred_bytes=self.monitor.update_transferred_bytes
                    )

                # --- FINALIZE ---
                await loop.run_in_executor(
                    None,
                    lambda: self.db.add_file(
                        parent_id, file_name, total_size, original_file_hash, time.time(), split_files_info
                    )
                )
                
                self.controller.remove_task(task_id) # Removed from state on success
                await utils.trigger_db_upload_in_background(self.shared_state)

            except asyncio.CancelledError:
                # Check actual state in controller to distinguish pause vs cancel
                task_info = self.controller.get_task(task_id)
                if task_info and task_info.get('status') == 'paused':
                    logger.info(f"Upload task '{file_name}' paused.")
                    transferred = len(task_info.get('transferred_parts', [])) * CHUNK_SIZE
                    progress_callback(task_id, file_name, transferred, total_size, 'paused', 0)
                else:
                    logger.warning(f"Upload task '{file_name}' was cancelled.")
                    # Ensure controller state is updated if not already
                    self.controller.remove_task(task_id)
                    progress_callback(task_id, file_name, 0, total_size, 'cancelled', 0)
            
            except errors.ItemAlreadyExistsError as e:
                self.controller.mark_failed(task_id, str(e))
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message=str(e))
            
            except Exception as e:
                logger.error(f"Upload error '{file_name}': {e}", exc_info=True)
                self.controller.mark_failed(task_id, str(e))
                progress_callback(task_id, file_name, 0, total_size, 'failed', 0, message="發生未知的內部錯誤。")
            
            finally:
                if task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[task_id]

    async def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Callable):
        """Entry point for new downloads."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return

        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
        semaphore = asyncio.Semaphore(concurrency_limit)

        tasks = []
        for item in items:
            tasks.append(
                self._download_single_item(client, group_id, item['task_id'], item, destination_dir, semaphore, progress_callback)
            )
        
        await asyncio.gather(*tasks, return_exceptions=True)
        self.monitor.close()

    async def _download_single_item(self, client, group_id: int, main_task_id: str, item: Dict, dest_path: str, 
                                    semaphore: asyncio.Semaphore, progress_callback: Callable,
                                    resume: bool = False, completed_parts: set = None):
        """
        Worker for downloading.
        """
        item_db_id, item_type, item_name = item.get('db_id'), item.get('type'), item.get('name')
        
        try:
            self.shared_state.active_tasks[main_task_id] = asyncio.current_task()
        except RuntimeError: pass
        
        loop = asyncio.get_running_loop()

        try:
            async with semaphore:
                client = await utils.ensure_client_connected(self.shared_state)
                if not client: return

                # Register/Update Controller
                if not resume:
                    if item_type == 'file':
                         file_details = await loop.run_in_executor(None, self.db.get_file_details, item_db_id)
                         if file_details:
                             self.controller.add_download_task(
                                 main_task_id, item_db_id, dest_path, item.get('size', 0), file_details
                             )
                
                if item_type == 'file':
                    file_details = item.get('file_details') # From resume context
                    if not file_details:
                        file_details = await loop.run_in_executor(None, self.db.get_file_details, item_db_id)
                    
                    if not file_details: 
                        raise errors.PathNotFoundError("File not found.")
                    
                    await self._download_file_from_details(
                        client, group_id, main_task_id, file_details, dest_path, progress_callback,
                        completed_parts=completed_parts or set()
                    )
                    
                    # Success
                    self.controller.remove_task(main_task_id)

                elif item_type == 'folder':
                    await self._download_folder(client, group_id, main_task_id, item, dest_path, progress_callback)

        except asyncio.CancelledError:
            task_info = self.controller.get_task(main_task_id)
            if task_info and task_info.get('status') == 'paused':
                logger.info(f"Download task '{item_name}' paused.")
                transferred = len(task_info.get('transferred_parts', [])) * CHUNK_SIZE
                progress_callback(main_task_id, item_name, transferred, item.get('size', 0), 'paused', 0)
            else:
                logger.warning(f"Download task '{item_name}' was cancelled.")
                self.controller.remove_task(main_task_id)
                progress_callback(main_task_id, item_name, 0, 0, 'cancelled', 0)
        except Exception as e:
            logger.error(f"Download error {item_name}: {e}")
            self.controller.mark_failed(main_task_id, str(e))
            progress_callback(main_task_id, item_name, 0, 0, 'failed', 0, message=str(e))
        finally:
            if main_task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[main_task_id]

    async def _download_folder(self, client, group_id: int, main_task_id: str, folder_item: Dict, dest_path: str, progress_callback: Callable):
        """
        Downloads a folder. If files exist (from previous run), they will be skipped/resumed.
        """
        loop = asyncio.get_running_loop()
        folder_contents = await loop.run_in_executor(None, self.db.get_folder_contents_recursive, folder_item['db_id'])
        
        if not folder_contents: raise errors.PathNotFoundError("Folder empty or not found.")
        
        actual_folder_name = folder_contents.get('folder_name', folder_item['name'])
        local_root_path = os.path.join(dest_path, actual_folder_name)
        os.makedirs(local_root_path, exist_ok=True)
        
        files_in_folder = [f for f in folder_contents['items'] if f['type'] == 'file']
        
        child_info_for_frontend = []
        for f_or_d in folder_contents['items']:
            # [Fix] Use deterministic ID for files to support resume across restarts
            if f_or_d['type'] == 'file':
                item_task_id = f"dl_file_{f_or_d['db_id']}"
            else:
                item_task_id = f"dl_{uuid.uuid4()}" 
            
            child_info = {**f_or_d, 'id': item_task_id}
            child_info_for_frontend.append(child_info)
            if f_or_d['type'] == 'folder':
                os.makedirs(os.path.join(local_root_path, f_or_d['relative_path']), exist_ok=True)

        total_size = sum(f['size'] for f in files_in_folder)
        progress_callback(main_task_id, actual_folder_name, 0, total_size, 'starting_folder', 0, 
                          total_files=len(files_in_folder), children=child_info_for_frontend)

        download_tasks = []
        for child in child_info_for_frontend:
            if child['type'] == 'file':
                # Register sub-task in controller to ensure progress is saved
                file_details = await loop.run_in_executor(None, self.db.get_file_details, child['db_id'])
                if file_details:
                    dest_file_path = os.path.join(local_root_path, os.path.dirname(child['relative_path']), child['name'])
                    # Use the generated UUID (child['id']) as the task_id
                    self.controller.add_download_task(
                        child['id'], child['db_id'], dest_file_path, child['size'], file_details
                    )

                download_tasks.append(
                    self._download_file_from_details(
                        client, group_id, child['id'], 
                        child, 
                        os.path.join(local_root_path, os.path.dirname(child['relative_path'])), 
                        progress_callback, parent_task_id=main_task_id
                    )
                )
        
        await asyncio.gather(*download_tasks, return_exceptions=True)
        progress_callback(main_task_id, actual_folder_name, total_size, total_size, 'completed', 0)

    async def _download_file_from_details(self, client, group_id: int, task_id: str, file_details: Dict, destination: str, 
                                          progress_callback: Callable, parent_task_id: str = None, completed_parts: set = None):
        """Helper to download a file."""
        file_name = file_details['name']
        total_size = file_details.get("size", 0)
        
        # Calculate start offset for monitor based on parts
        start_offset = 0
        if completed_parts:
            start_offset = len(completed_parts) * (CHUNK_SIZE)
            if start_offset > total_size: start_offset = total_size

        progress_callback(task_id, file_name, start_offset, total_size, 'transferring', 0, parent_task_id=parent_task_id)

        # Define chunk callback for real-time state saving
        def chunk_cb(part_num):
            self.controller.update_progress(task_id, part_num)

        coro = telegram_comms.download_file(
            client, group_id, file_details, destination,
            task_id=task_id, progress_callback=progress_callback,
            completed_parts=completed_parts,
            chunk_callback=chunk_cb,
            update_transferred_bytes=self.monitor.update_transferred_bytes
        )
        
        task = asyncio.create_task(coro)
        self.shared_state.active_tasks[task_id] = task
        await task

    # --- CONTROL METHODS ---

    async def resume_transfer(self, task_id: str, progress_callback: Callable):
        """
        Resumes a paused or failed transfer task from the controller state.
        """
        task_info = self.controller.get_task(task_id)
        if not task_info:
            logger.warning(f"Resume failed: Task {task_id} not found in controller.")
            return

        self.controller.mark_resumed(task_id)
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return

        group_id = await telegram_comms.get_group(client, self.shared_state.api_id)

        if task_info['type'] == 'upload':
            await self._upload_single_file(
                client, group_id, task_info['parent_id'], task_info['file_path'], task_id,
                self._resume_semaphore, progress_callback,
                resume_context=task_info['split_files_info'],
                pre_calculated_hash=task_info['file_hash']
            )
        
        elif task_info['type'] == 'download':
            item_mock = {
                'db_id': task_info['db_id'],
                'type': 'file', 
                'name': task_info['file_details']['name'],
                'file_details': task_info['file_details'],
                'size': task_info['total_size']
            }
            await self._download_single_item(
                client, group_id, task_id, item_mock, task_info['save_path'],
                self._resume_semaphore, progress_callback,
                resume=True,
                completed_parts=set(task_info['transferred_parts'])
            )

    def pause_transfer(self, task_id: str):
        """
        Pauses an active transfer. The task state in Controller is preserved.
        """
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
        
        self.controller.mark_paused(task_id)
        logger.info(f"Task {task_id} paused.")

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        """
        Permanently cancels and removes a transfer.
        If it's a download task, the partial file on disk will be deleted.
        """
        # 1. 先獲取任務資訊 (因為一旦 remove_task 就拿不到了)
        task_info = self.controller.get_task(task_id)
        
        # 2. 停止正在運行的 asyncio 任務
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
        
        # 3. [新增] 如果是「下載」任務，清理本地殘留檔案
        if task_info and task_info.get('type') == 'download':
            file_path = task_info.get('save_path')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Deleted partial file for cancelled task: {file_path}")
                except OSError as e:
                    logger.error(f"Failed to delete cancelled file '{file_path}': {e}")

        # 4. 從控制器移除任務記錄
        self.controller.remove_task(task_id)
        
        logger.info(f"Task {task_id} cancelled and removed.")
        return {"success": True, "message": "任務已取消並移除。"}
    