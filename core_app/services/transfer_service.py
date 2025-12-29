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
from core_app.api import file_processor as fp

logger = logging.getLogger(__name__)

CONCURRENCY_LIMIT = 3

class TransferService:
    """
    Manages all time-consuming file upload and download tasks.
    Refactored to separate structure creation from file transfer execution,
    and includes robust cancellation cleanup logic.
    """
    def __init__(self, shared_state: 'SharedState', monitor_service: Optional[TransferMonitorService] = None):
        self.shared_state = shared_state
        self.db = DatabaseHandler()
        self.monitor = monitor_service if monitor_service else TransferMonitorService()
        self.controller = TransferController()
        
        # Global semaphore for tasks to prevent flooding
        self._semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        # Reset any tasks that were 'transferring' when the app crashed
        self.controller.reset_zombie_tasks()

    # --- UPLOAD OPERATIONS ---

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], progress_callback: Callable):
        """
        Entry point for selecting and uploading multiple separate files.
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Upload failed: Client not connected.")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗")
            return

        tasks_to_run = []
        for item in upload_items:
            task_id = item['task_id']
            file_path = item['local_path']
            file_name = os.path.basename(file_path)
            
            try:
                total_size = os.path.getsize(file_path)
            except OSError:
                total_size = 0

            # Register Single File Task
            self.controller.add_upload_task(
                task_id, file_path, parent_id, total_size, is_folder=False, file_hash=None
            )

            # [FIXED] Send initial update to frontend to sync Total Size immediately
            progress_callback(task_id, file_name, 0, total_size, 'queued', 0, is_folder=False)

            tasks_to_run.append(
                self._upload_single_file(
                    client, task_id, task_id, # main_id == sub_id for single files
                    file_path, parent_id,
                    progress_callback
                )
            )
            
        await asyncio.gather(*tasks_to_run, return_exceptions=True)

    async def upload_folder_recursive(self, parent_id: int, local_folder_path: str, main_task_id: str, progress_callback: Callable):
        """
        Recursive folder upload:
        1. Pre-scan structure and create remote folders.
        2. Bulk register all tasks.
        3. Execute file uploads.
        """
        base_folder_name = os.path.basename(local_folder_path)
        
        logger.info(f"Starting folder upload: '{local_folder_path}' (Task: {main_task_id})")

        # --- Phase 1: Pre-scan & Structure Creation ---
        total_size = 0
        file_list = [] # List of (local_path, file_size)
        
        # 1. Calculate Total Size & Gather File List
        for root, dirs, files in os.walk(local_folder_path):
            for f in files:
                full_path = os.path.join(root, f)
                try:
                    f_size = os.path.getsize(full_path)
                    total_size += f_size
                    file_list.append((full_path, f_size))
                except OSError:
                    pass

        # 2. Register Main Folder Task
        self.controller.add_upload_task(
            main_task_id, local_folder_path, parent_id, total_size, is_folder=True
        )

        # Notify Frontend: Task Created
        progress_callback(main_task_id, base_folder_name, 0, total_size, 'queued', 0, is_folder=True)

        loop = asyncio.get_running_loop()
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: 
            self.controller.mark_failed(main_task_id, "Client disconnected")
            progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message="連線失敗")
            return

        try:
            # 3. Create DB Structure (BFS/DFS) and Map Paths to Remote IDs
            # Create Root Folder
            try:
                root_remote_id = await loop.run_in_executor(None, self.db.add_folder, parent_id, base_folder_name)
            except errors.ItemAlreadyExistsError:
                existing = await loop.run_in_executor(None, self.db.get_folder_contents, parent_id)
                found = next((f for f in existing['folders'] if f['name'] == base_folder_name), None)
                if found:
                    root_remote_id = found['id']
                else:
                    raise Exception("Folder exists but cannot be found.")

            path_to_remote_id = {local_folder_path: root_remote_id}
            
            for root, dirs, _ in os.walk(local_folder_path):
                current_remote_id = path_to_remote_id.get(root)
                if current_remote_id is None: continue

                for d in dirs:
                    local_dir_path = os.path.join(root, d)
                    try:
                        new_folder_id = await loop.run_in_executor(None, self.db.add_folder, current_remote_id, d)
                        path_to_remote_id[local_dir_path] = new_folder_id
                    except errors.ItemAlreadyExistsError:
                        contents = await loop.run_in_executor(None, self.db.get_folder_contents, current_remote_id)
                        found = next((f for f in contents['folders'] if f['name'] == d), None)
                        if found:
                            path_to_remote_id[local_dir_path] = found['id']

            # 4. Create Child Tasks in Memory
            child_tasks_map = {}
            tasks_to_run = []
            
            for file_path, f_size in file_list:
                sub_task_id = str(uuid.uuid4())
                file_dir = os.path.dirname(file_path)
                target_parent_id = path_to_remote_id.get(file_dir)
                
                if target_parent_id:
                    child_tasks_map[sub_task_id] = {
                        "file_path": file_path,
                        "parent_id": target_parent_id,
                        "total_size": f_size,
                        "status": "queued"
                    }
                    
                    tasks_to_run.append(
                        self._upload_single_file(
                            client, main_task_id, sub_task_id,
                            file_path, target_parent_id,
                            progress_callback
                        )
                    )

            # 5. Bulk Register to Controller
            self.controller.add_child_tasks_bulk(main_task_id, child_tasks_map)

            # --- Phase 2: Execution ---
            progress_callback(main_task_id, base_folder_name, 0, total_size, 'transferring', 0)
            
            await asyncio.gather(*tasks_to_run, return_exceptions=True)

            # Finalize
            await utils.trigger_db_upload_in_background(self.shared_state)
            
            task_info = self.controller.get_task(main_task_id)
            if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
                self.controller.mark_sub_task_completed(main_task_id, main_task_id)
                progress_callback(main_task_id, base_folder_name, 0, total_size, 'completed', 0)

        except Exception as e:
            logger.error(f"Folder upload failed: {e}", exc_info=True)
            self.controller.mark_failed(main_task_id, str(e))
            progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message=str(e))

    async def _upload_single_file(self, client, main_task_id: str, sub_task_id: str,
                                  file_path: str, parent_id: int, 
                                  progress_callback: Callable,
                                  resume_context: List = None, pre_calculated_hash: str = None):
        """
        Worker for uploading a single file. 
        """
        file_name = os.path.basename(file_path)
        
        def chunk_cb(part_num, msg_id, part_hash):
            self.controller.update_progress(main_task_id, sub_task_id, part_num, [msg_id, part_hash])

        last_uploaded = 0
        last_update_time = time.time()

        def ui_cb(current, total):
            nonlocal last_uploaded, last_update_time
            delta = current - last_uploaded
            now = time.time()
            time_diff = now - last_update_time

            if delta > 0:
                last_uploaded = current
                last_update_time = now
                
                speed = delta / time_diff if time_diff > 0 else 0
                
                # 1. Update Traffic Monitor (Real-time)
                asyncio.create_task(self.monitor.update_transferred_bytes(delta))
                # 2. Notify UI
                progress_callback(main_task_id, delta, speed)

        async with self._semaphore:
            try:
                current_task = asyncio.current_task()
                self.shared_state.active_tasks[sub_task_id] = current_task

                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"File not found: {file_path}")

                loop = asyncio.get_running_loop()
                total_size = os.path.getsize(file_path)

                original_file_hash = pre_calculated_hash
                split_files_info = resume_context or []

                if not original_file_hash:
                    original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    # Persist hash immediately to avoid re-calculation on resume
                    self.controller.set_file_hash(sub_task_id, original_file_hash)
                    
                existing_file_id = await loop.run_in_executor(None, self.db.find_file_by_hash, original_file_hash)
                
                if existing_file_id:
                    logger.info(f"Sec-upload (Deduplication) triggered for {file_name}")
                    try:
                        await loop.run_in_executor(
                            None, 
                            lambda: self.db.add_file(parent_id, file_name, time.time(), file_id=existing_file_id)
                        )
                        self.controller.mark_sub_task_completed(main_task_id, sub_task_id)
                        # Notify UI of full completion for this file
                        remaining = total_size - last_uploaded
                        if remaining > 0:
                            progress_callback(main_task_id, remaining, 0)
                        return
                    except errors.ItemAlreadyExistsError:
                        self.controller.mark_sub_task_failed(main_task_id, sub_task_id, "File already exists")
                        return

                split_files_info = await telegram_comms.upload_file_with_info(
                    client, self.shared_state.group_id, file_path, original_file_hash, 
                    main_task_id,
                    progress_callback=ui_cb, 
                    resume_context=split_files_info,
                    chunk_callback=chunk_cb, 
                    parent_id=main_task_id if main_task_id != sub_task_id else None
                )

                await loop.run_in_executor(
                    None,
                    lambda: self.db.add_file(
                        parent_id, file_name, time.time(), 
                        file_hash=original_file_hash, size=total_size, chunks_data=split_files_info
                    )
                )
                
                self.controller.mark_sub_task_completed(main_task_id, sub_task_id)

                if main_task_id == sub_task_id:
                    progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)

            except asyncio.CancelledError:
                logger.info(f"Upload task cancelled: {file_name}")
                raise
            except Exception as e:
                logger.error(f"File upload error '{file_name}': {e}", exc_info=True)
                self.controller.mark_sub_task_failed(main_task_id, sub_task_id, str(e))
            finally:
                if sub_task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[sub_task_id]

    # --- DOWNLOAD OPERATIONS ---

    async def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable):
        """
        Entry point for downloading items.
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return

        tasks_to_run = []
        for item in items:
            if item['type'] == 'folder':
                tasks_to_run.append(
                    self._download_folder(client, item['task_id'], item, destination_dir, progress_callback)
                )
            else:
                # Single File
                task_id = item['task_id']
                db_id = item['db_id']
                
                loop = asyncio.get_running_loop()
                file_details = await loop.run_in_executor(None, self.db.get_file_details, db_id)
                if not file_details: 
                    progress_callback(task_id, item['name'], 0, 0, 'failed', 0, message="找不到檔案資訊")
                    continue

                save_path = await loop.run_in_executor(None, fp.get_unique_filepath, destination_dir, file_details['name'])

                # Register
                self.controller.add_download_task(
                    task_id, db_id, save_path, file_details['size'], is_folder=False, file_details=file_details
                )
                
                # [FIXED] Send initial update to frontend to sync Total Size & queued status
                progress_callback(task_id, file_details['name'], 0, file_details['size'], 'queued', 0, is_folder=False)

                tasks_to_run.append(
                    self._download_single_item(
                        client, task_id, task_id, 
                        save_path, file_details, 
                        progress_callback
                    )
                )
        
        await asyncio.gather(*tasks_to_run, return_exceptions=True)
        self.monitor.close()

    async def _download_folder(self, client, main_task_id: str, folder_item: Dict, dest_path: str, progress_callback: Callable):
        """
        Recursive folder download.
        """
        loop = asyncio.get_running_loop()
        folder_db_id = folder_item['db_id']
        
        contents = await loop.run_in_executor(None, self.db.get_folder_contents_recursive, folder_db_id)
        if not contents: 
            progress_callback(main_task_id, folder_item['name'], 0, 0, 'failed', 0, message="資料夾為空或讀取失敗")
            return
        
        root_folder_name = contents['folder_name']
        local_root_path = os.path.join(dest_path, root_folder_name)
        
        os.makedirs(local_root_path, exist_ok=True)
        total_size = 0
        file_items = []
        
        for item in contents['items']:
            relative_path = item['relative_path']
            full_local_path = os.path.join(local_root_path, relative_path)
            
            if item['type'] == 'folder':
                os.makedirs(full_local_path, exist_ok=True)
            elif item['type'] == 'file':
                total_size += item['size']
                file_items.append({
                    "db_id": item['id'],
                    "file_id": item['file_id'],
                    "local_path": full_local_path,
                    "size": item['size'],
                    "hash": item['hash'],
                    "chunks": item['chunks'],
                    "name": item['name']
                })

        # Register Main Task
        self.controller.add_download_task(
            main_task_id, folder_db_id, local_root_path, total_size, is_folder=True
        )
        progress_callback(main_task_id, root_folder_name, 0, total_size, 'queued', 0, is_folder=True)

        child_tasks_map = {}
        tasks_to_run = []

        for f in file_items:
            sub_task_id = str(uuid.uuid4())
            
            file_details = {
                "name": f['name'],
                "size": f['size'],
                "hash": f['hash'],
                "chunks": f['chunks']
            }

            child_tasks_map[sub_task_id] = {
                "db_id": f['db_id'],
                "save_path": f['local_path'],
                "total_size": f['size'],
                "status": "queued",
                "file_details": file_details
            }

            tasks_to_run.append(
                self._download_single_item(
                    client, main_task_id, sub_task_id,
                    f['local_path'], file_details,
                    progress_callback
                )
            )

        self.controller.add_child_tasks_bulk(main_task_id, child_tasks_map)

        progress_callback(main_task_id, root_folder_name, 0, total_size, 'transferring', 0)
        await asyncio.gather(*tasks_to_run, return_exceptions=True)
        
        task_info = self.controller.get_task(main_task_id)
        if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
            self.controller.mark_sub_task_completed(main_task_id, main_task_id)
            progress_callback(main_task_id, root_folder_name, 0, total_size, 'completed', 0)

    async def _download_single_item(self, client, main_task_id: str, sub_task_id: str, 
                                    save_path: str, file_details: Dict,
                                    progress_callback: Callable,
                                    resume_parts: set = None):
        """
        Worker for downloading a single file.
        """
        def chunk_cb(part_num):
            self.controller.update_progress(main_task_id, sub_task_id, part_num)
        
        last_downloaded = 0
        last_update_time = time.time()

        def ui_cb(current, total):
            nonlocal last_downloaded, last_update_time
            delta = current - last_downloaded
            now = time.time()
            time_diff = now - last_update_time

            if delta > 0:
                last_downloaded = current
                last_update_time = now
                
                speed = delta / time_diff if time_diff > 0 else 0
                
                # 1. Update Traffic Monitor (Real-time)
                asyncio.create_task(self.monitor.update_transferred_bytes(delta))
                # 2. Notify UI
                progress_callback(main_task_id, delta, speed)

        async with self._semaphore:
            try:
                self.shared_state.active_tasks[sub_task_id] = asyncio.current_task()
                
                await telegram_comms.download_file(
                    client, self.shared_state.group_id, file_details, os.path.dirname(save_path),
                    task_id=sub_task_id,
                    progress_callback=ui_cb,
                    completed_parts=resume_parts,
                    chunk_callback=chunk_cb
                )
                
                self.controller.mark_sub_task_completed(main_task_id, sub_task_id)

                if main_task_id == sub_task_id:
                    progress_callback(main_task_id, file_details['name'], file_details['size'], file_details['size'], 'completed', 0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Download failed {save_path}: {e}")
                self.controller.mark_sub_task_failed(main_task_id, sub_task_id, str(e))
            finally:
                if sub_task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[sub_task_id]

    # --- CONTROL METHODS ---

    def get_transfer_config(self) -> Dict[str, Any]:
        """Returns initialization configuration and stats."""
        return {
            "todayTraffic": self.monitor.get_today_traffic(),
            "chunkSize": fp.CHUNK_SIZE
        }

    async def resume_transfer(self, task_id: str, progress_callback: Callable):
        """
        Resumes a paused or failed transfer task.
        """
        task_info = self.controller.get_task(task_id)
        if not task_info: return

        self.controller.mark_resumed(task_id)
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return
        
        progress_callback(task_id, 0, 0, status='transferring') 

        tasks_to_run = []
        
        if not task_info.get("is_folder"):
            # Single File Resume
            if task_info['type'] == 'upload':
                tasks_to_run.append(
                    self._upload_single_file(
                        client, task_id, task_id, 
                        task_info['file_path'], task_info['parent_id'], 
                        progress_callback,
                        resume_context=task_info.get('split_files_info'),
                        pre_calculated_hash=task_info.get('file_hash')
                    )
                )
            else:
                tasks_to_run.append(
                    self._download_single_item(
                        client, task_id, task_id,
                        task_info['save_path'], task_info['file_details'],
                        progress_callback,
                        resume_parts=set(task_info.get('transferred_parts', []))
                    )
                )
        else:
            # Folder Resume
            child_tasks = task_info.get("child_tasks", {})
            for sub_id, sub_data in child_tasks.items():
                if sub_data['status'] == 'completed':
                    continue
                
                sub_data['status'] = 'queued'
                
                if task_info['type'] == 'upload':
                    tasks_to_run.append(
                        self._upload_single_file(
                            client, task_id, sub_id,
                            sub_data['file_path'], sub_data['parent_id'],
                            progress_callback,
                            resume_context=sub_data.get('split_files_info'),
                            pre_calculated_hash=sub_data.get('file_hash') # Pass the stored hash
                        )
                    )
                else:
                    tasks_to_run.append(
                        self._download_single_item(
                            client, task_id, sub_id,
                            sub_data['save_path'], sub_data['file_details'],
                            progress_callback,
                            resume_parts=set(sub_data.get('transferred_parts', []))
                        )
                    )
            
            self.controller._save_state()

        await asyncio.gather(*tasks_to_run, return_exceptions=True)
        
        self.controller.mark_sub_task_completed(task_id, task_id)
        progress_callback(task_id, 0, 0, status='completed')

    def pause_transfer(self, task_id: str):
        """
        Pauses an active transfer.
        """
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
        self.controller.mark_paused(task_id)
        logger.info(f"Task {task_id} marked as paused.")

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        """
        Permanently cancels and removes a transfer, triggering background cleanup.
        """
        self.pause_transfer(task_id)
        
        task_info = self.controller.get_task(task_id)
        self.controller.remove_task(task_id)
        
        if task_info:
            asyncio.run_coroutine_threadsafe(self._cleanup_task_data(task_info), self.shared_state.loop)

        return {"success": True, "message": "任務已取消並開始背景清理。"}

    def remove_history_item(self, task_id: str) -> Dict[str, Any]:
        """
        Removes a task from the history (state file) without deleting the physical file.
        """
        self.controller.remove_task(task_id)
        return {"success": True, "message": "History item removed."}

    async def _cleanup_task_data(self, task_info: Dict[str, Any]):
        """
        Performs slow cleanup operations (Network calls / Disk I/O) after cancellation.
        """
        try:
            task_type = task_info.get('type')
            is_folder = task_info.get('is_folder')
            logger.info(f"Starting cleanup for cancelled task: {task_type}")

            if task_type == 'download':
                paths_to_delete = []
                if is_folder:
                    child_tasks = task_info.get('child_tasks', {})
                    for child in child_tasks.values():
                        if child.get('save_path'):
                            paths_to_delete.append(child['save_path'])
                else:
                    if task_info.get('save_path'):
                        paths_to_delete.append(task_info['save_path'])

                for path in paths_to_delete:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError: pass

            elif task_type == 'upload':
                client = await utils.ensure_client_connected(self.shared_state)
                if not client: return

                message_ids_to_delete = []

                def collect_msg_ids(info_list):
                    if info_list:
                        for item in info_list:
                            if len(item) >= 2:
                                message_ids_to_delete.append(item[1])

                if is_folder:
                    child_tasks = task_info.get('child_tasks', {})
                    for child in child_tasks.values():
                        collect_msg_ids(child.get('split_files_info'))
                else:
                    collect_msg_ids(task_info.get('split_files_info'))

                if message_ids_to_delete:
                    batch_size = 100
                    for i in range(0, len(message_ids_to_delete), batch_size):
                        batch = message_ids_to_delete[i:i + batch_size]
                        try:
                            await client.delete_messages(self.shared_state.group_id, batch)
                        except Exception: pass
                            
            logger.info("Cleanup completed successfully.")

        except Exception as e:
            logger.error(f"Error during task cleanup: {e}", exc_info=True)