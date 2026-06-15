import logging
import asyncio
import time
import os
from typing import TYPE_CHECKING, List, Dict, Any, Callable, Optional
from collections import defaultdict

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from ..media.gallery_manager import GalleryManager
    from core_app.infrastructure.telegram.metadata_manager import MetadataManager

from core_app.application.transfer.transfer_controller import TransferController
from core_app.application.sync.file_status_watcher import FileStatusWatcher
from core_app.core import utils
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
from core_app.infrastructure.database.main_db.repositories.map_repository import MapRepository
from core_app.infrastructure.local_fs import file_processor as fp

# Import Strategies
from core_app.application.transfer.strategies.upload_strategy import UploadStrategy
from core_app.application.transfer.strategies.download_strategy import DownloadStrategy

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3

class TransferService:
    def __init__(self, shared_state: 'SharedState', gallery_manager: 'GalleryManager', metadata_manager: 'MetadataManager'):
        self.shared_state = shared_state
        self.db = DatabaseConnection()
        self.file_repo = FileRepository(self.db)
        self.folder_repo = FolderRepository(self.db)
        self.trash_repo = TrashRepository(self.db)
        self.map_repo = MapRepository(self.db)
        self.metadata_manager = metadata_manager
        self.controller = TransferController()
        self.watcher = FileStatusWatcher(self.shared_state.loop, self.folder_repo, status_change_callback=lambda x: None)
        self.gallery_manager = gallery_manager
        
        # In the future, this can be loaded from user settings
        self.concurrency_limit = DEFAULT_CONCURRENCY
        self._semaphore = asyncio.Semaphore(self.concurrency_limit)
        # Tracking active sub-tasks for cancellation
        self._active_sub_tasks: Dict[str, set] = defaultdict(set)
        
        self._refresh_callback: Optional[Callable] = None
        self._last_refresh_time = 0
        self._pending_refresh_folders = set()
        self._refresh_timer_task: Optional[asyncio.TimerHandle] = None

        # Initialize Strategies
        self.uploader = UploadStrategy(self)
        self.downloader = DownloadStrategy(self)

        self.controller.reset_zombie_tasks()
        all_tasks = self.controller.get_incomplete_transfers()
        self.watcher.load_initial_watches(all_tasks['uploads'], all_tasks['downloads'])

    def shutdown(self):
        """Stops the watcher and cancels all active tasks during shutdown."""
        logger.info("Shutting down TransferService...")
        self.watcher.stop()
        
        # Cancel all active tasks tracked in SharedState
        active_ids = list(self._active_sub_tasks.keys())
        for task_id in active_ids:
            self.cancel_transfer(task_id)
            
        logger.info("TransferService shutdown complete.")

    # --- Common Helper Methods for Strategies ->
    
    def set_refresh_callback(self, callback: Callable):
        self._refresh_callback = callback

    def _trigger_folder_refresh(self, folder_ids: List[int]):
        """
        Throttled trigger for folder list refresh. 
        Ensures signal is sent at most once per second.
        """
        if not self._refresh_callback or not folder_ids:
            return

        valid_ids = [fid for fid in folder_ids if fid]
        if not valid_ids: return
        
        self._pending_refresh_folders.update(valid_ids)
        
        loop = self.shared_state.loop
        now = time.time()
        
        def _emit():
            if self._refresh_callback and self._pending_refresh_folders:
                ids = list(self._pending_refresh_folders)
                self._pending_refresh_folders.clear()
                self._refresh_callback(ids)
                self._last_refresh_time = time.time()
            self._refresh_timer_task = None

        if self._refresh_timer_task:
            return

        elapsed = now - self._last_refresh_time
        if elapsed >= 1.0:
            _emit()
        else:
            delay = 1.0 - elapsed
            self._refresh_timer_task = loop.call_later(delay, _emit)

    def set_file_status_callback(self, callback: Callable):
        self.watcher._callback = callback
        self.watcher.start()

    def _normalize_path(self, path: str) -> str:
        if not path:
            return ""
        p = path.replace(r'\\?\\', '').replace('\\\\?\\', '')
        return os.path.normpath(os.path.abspath(p))
    
    def _get_folder_db_info(self, folder_id):
        conn = self.db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id = ?", (folder_id,))
        return cur.fetchone()

    # --- Public API (Delegates to Strategies) ---

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], progress_callback: Callable):
        return await self.uploader.start('files', parent_id, upload_items, progress_callback)

    async def upload_folder_recursive(self, parent_id: int, local_folder_path: str, main_task_id: str, progress_callback: Callable):
        return await self.uploader.start('folder', parent_id, local_folder_path, main_task_id, progress_callback)

    async def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable):
        return await self.downloader.start('items', items, destination_dir, progress_callback)

    # --- Task Control & Cleanup ---

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        self.pause_transfer(task_id)
        self.watcher.remove_watch(task_id)
        
        if task_id in self._active_sub_tasks:
            del self._active_sub_tasks[task_id]
        
        task_info = self.controller.get_task(task_id)
        self.controller.remove_task(task_id)
        
        if task_info:
            asyncio.run_coroutine_threadsafe(self._cleanup_task_data(task_info), self.shared_state.loop)

        return {"success": True}

    async def _cleanup_task_data(self, task_info: Dict[str, Any]):
        task_type = task_info.get('type')
        try:
            if task_type == 'upload':
                await self.uploader.cleanup(task_info)
            elif task_type == 'download':
                await self.downloader.cleanup(task_info)
        except Exception as e:
            logger.error(f"Error during cleanup for task {task_info.get('task_id')}: {e}", exc_info=True)

    def pause_transfer(self, task_id: str):
        task = self.shared_state.active_tasks.get(task_id)
        if task and not task.done():
            self.shared_state.loop.call_soon_threadsafe(task.cancel)
        
        sub_tasks = self._active_sub_tasks.get(task_id, set()).copy()
        for sub_id in sub_tasks:
            sub_task = self.shared_state.active_tasks.get(sub_id)
            if sub_task and not sub_task.done():
                 self.shared_state.loop.call_soon_threadsafe(sub_task.cancel)
        
        self._active_sub_tasks[task_id].clear()
        self.controller.mark_paused(task_id)
        self.shared_state.loop.create_task(self.controller.pause_all_sub_tasks(task_id))
        
        logger.info(f"Task {task_id} marked as paused (Sub-tasks cancelled: {len(sub_tasks)}).")

    # --- Resume Logic (Complex, kept here for now as it orchestrates both) ---

    async def resume_transfer(self, task_id: str, progress_callback: Callable):
        self.shared_state.active_tasks[task_id] = asyncio.current_task()

        try:
            task_info = self.controller.get_task(task_id)
            if not task_info: return

            self.controller.mark_resumed(task_id)
            client = await utils.ensure_client_connected(self.shared_state)
            if not client: return
            
            # Calculate initial progress
            initial_progress = 0
            if not task_info.get("is_folder"):
                if task_info.get('status') == 'completed':
                    initial_progress = task_info.get('total_size', 0)
                else:
                    parts = task_info.get('transferred_parts', [])
                    initial_progress = len(parts) * fp.CHUNK_SIZE
            else:
                for sub_data in task_info.get("child_tasks", {}).values():
                    if sub_data.get('status') == 'completed':
                        initial_progress += sub_data.get('total_size', 0)
                    else:
                        parts = sub_data.get('transferred_parts', [])
                        initial_progress += len(parts) * fp.CHUNK_SIZE
            
            if initial_progress > task_info.get('total_size', 0):
                initial_progress = task_info.get('total_size', 0)

            progress_callback(task_id, task_info.get('name', ''), initial_progress, task_info.get('total_size', 0), 'transferring', 0) 

            tasks_to_run = []
            is_upload = task_info['type'] == 'upload'
            
            if not task_info.get("is_folder"):
                if is_upload:
                    tasks_to_run.append(
                        self.uploader._upload_single_file(
                            client, task_id, task_id, 
                            task_info['file_path'], task_info['parent_id'], 
                            progress_callback,
                            resume_context=task_info.get('split_files_info'),
                            pre_calculated_hash=task_info.get('file_hash')
                        )
                    )
                else:
                    tasks_to_run.append(
                        self.downloader._download_single_item(
                            client, task_id, task_id,
                            task_info['save_path'], task_info['file_details'],
                            progress_callback,
                            resume_parts=set(task_info.get('transferred_parts', []))
                        )
                    )
            else:
                child_tasks = task_info.get("child_tasks", {})
                for sub_id, sub_data in child_tasks.items():
                    if sub_data['status'] == 'completed':
                        continue
                    
                    sub_data['status'] = 'queued'
                    
                    if is_upload:
                        tasks_to_run.append(
                            self.uploader._upload_single_file(
                                client, task_id, sub_id,
                                sub_data['file_path'], sub_data['parent_id'],
                                progress_callback,
                                resume_context=sub_data.get('split_files_info'),
                                pre_calculated_hash=sub_data.get('file_hash')
                            )
                        )
                    else:
                        tasks_to_run.append(
                            self.downloader._download_single_item(
                                client, task_id, sub_id,
                                sub_data['save_path'], sub_data['file_details'],
                                progress_callback,
                                resume_parts=set(sub_data.get('transferred_parts', []))
                            )
                        )

            results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
            
            # Check for cancellation within results to avoid marking task as completed
            for res in results:
                if isinstance(res, asyncio.CancelledError):
                    logger.info(f"Resume task {task_id} caught cancellation signal in child tasks.")
                    raise res # Jump to outer CancelledError handler
            
            if is_upload:
                 await self.uploader._finalize_thumbnails(client, task_id)
                 await self.metadata_manager.sync_db_to_cloud(client, self.shared_state.group_id, self.shared_state.api_id)

            self.controller.mark_sub_task_completed(task_id, task_id)
            progress_callback(task_id, task_info.get('name', ''), task_info.get('total_size', 0), task_info.get('total_size', 0), 'completed', 0)

        except asyncio.CancelledError:
            logger.info(f"Resume task {task_id} cancelled/paused.")
            raise
        finally:
            if task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[task_id]
            self._active_sub_tasks.pop(task_id, None)

    # --- Other Methods ---

    def remove_transfer_history(self, task_id: str) -> Dict[str, Any]:
        self.controller.remove_task(task_id)
        self.watcher.remove_watch(task_id)
        return {"success": True}

    def get_transfer_config(self) -> Dict[str, Any]:
        return {
            "todayTraffic": self.controller.get_today_traffic(),
            "chunkSize": fp.CHUNK_SIZE,
            "concurrencyLimit": self.concurrency_limit
        }

    def set_concurrency_limit(self, limit: int):
        """Dynamically update concurrency limit (Note: Only applies to new tasks grabbed by semaphore)"""
        if limit < 1: limit = 1
        self.concurrency_limit = limit
        # Re-create semaphore (this will take effect for new transfers waiting for the semaphore)
        self._semaphore = asyncio.Semaphore(self.concurrency_limit)
        logger.info(f"Concurrency limit updated to {self.concurrency_limit}")