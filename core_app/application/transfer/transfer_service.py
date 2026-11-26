import collections
import logging
import asyncio
import sqlite3
from core_app.core.settings_manager import SettingsManager
import time
import os
from typing import TYPE_CHECKING, List, Dict, Any, Callable, Optional
from collections import defaultdict

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from core_app.application.file_system.thumbnail.manager import ThumbnailManager
    from core_app.infrastructure.telegram.metadata_manager import MetadataManager

from core_app.application.transfer.transfer_controller import TransferController
from core_app.application.sync.file_status_watcher import FileStatusWatcher
from core_app.core import utils
from core_app.core.errors import ErrorCode
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository

from core_app.infrastructure.local_fs import file_processor as fp

# Import Strategies
from core_app.application.transfer.strategies.upload_strategy import UploadStrategy
from core_app.application.transfer.strategies.download_strategy import DownloadStrategy

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 10


class PrioritySemaphore(asyncio.Semaphore):
    """Asyncio semaphore allowing waiting TDrive tasks to be reordered dynamically."""

    async def acquire(self):
        if not self.locked():
            self._value -= 1
            return True

        if self._waiters is None:
            self._waiters = collections.deque()
        fut = self._get_loop().create_future()
        current_task = asyncio.current_task()
        fut._tdrive_main_task_id = getattr(current_task, "_tdrive_main_task_id", None) if current_task else None
        self._waiters.append(fut)

        try:
            try:
                await fut
            finally:
                if self._waiters and fut in self._waiters:
                    self._waiters.remove(fut)
        except asyncio.CancelledError:
            if not fut.cancelled():
                self._value += 1
                self._wake_up_next()
            raise

        if self._value > 0:
            self._wake_up_next()
        return True

    def reorder_waiters(self, ordered_task_ids: list[str]) -> int:
        """Reorder pending waiter futures to prioritize the supplied task IDs."""
        if not self._waiters or not ordered_task_ids:
            return 0
        rank = {tid: idx for idx, tid in enumerate(ordered_task_ids)}
        fallback_rank = len(rank)
        waiters_list = list(self._waiters)
        # Stable sort preserves relative FIFO order for untagged waiters or sibling subfiles.
        waiters_list.sort(key=lambda fut: rank.get(getattr(fut, "_tdrive_main_task_id", None), fallback_rank))
        self._waiters = collections.deque(waiters_list)
        return len(waiters_list)


def _clamp_concurrency_limit(value: Any, default: int = DEFAULT_CONCURRENCY) -> int:
    """Normalize concurrency setting to a safe integer range [1, 10]."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, parsed))


class TransferService:
    def __init__(self, shared_state: 'SharedState', thumb_manager: 'ThumbnailManager', metadata_manager: 'MetadataManager'):
        self.shared_state = shared_state
        self.db = DatabaseConnection()
        self.file_repo = FileRepository(self.db)
        self.folder_repo = FolderRepository(self.db)
        self.trash_repo = TrashRepository(self.db)

        self.metadata_manager = metadata_manager
        self.controller = TransferController()
        self.watcher = FileStatusWatcher(self.shared_state.loop, self.folder_repo, status_change_callback=lambda x: None)
        self.thumb_manager = thumb_manager
        
        self.concurrency_limit = _clamp_concurrency_limit(
            SettingsManager().get("maxConcurrentTransfers", DEFAULT_CONCURRENCY)
        )
        self._semaphore = PrioritySemaphore(self.concurrency_limit)
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
        
        if self._refresh_timer_task:
            try:
                self._refresh_timer_task.cancel()
            except Exception as e:
                logger.debug(f"Failed to cancel refresh timer task: {e}")
            self._refresh_timer_task = None

        # Cancel all active tasks tracked in SharedState
        active_ids = list(self._active_sub_tasks.keys())
        for task_id in active_ids:
            self.cancel_transfer(task_id)
            
        if self.shared_state:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            tasks = list(getattr(self.shared_state, "background_tasks", []))
            for t in tasks:
                if t is not current_task and not t.done():
                    t.cancel()

        logger.info("TransferService shutdown complete.")

    def has_active_transfers(self) -> bool:
        """Returns True if any upload or download transfer task is currently active."""
        if any(bool(sub_ids) for sub_ids in self._active_sub_tasks.values()):
            return True
        if self.shared_state and getattr(self.shared_state, "active_tasks", None):
            for task_id, task in self.shared_state.active_tasks.items():
                if not str(task_id).startswith("bg_task_") and task and not task.done():
                    return True
        return False

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
        if task_info:
            task_info['created_artifacts'] = self.controller.get_created_artifacts(task_id)
            
        self.controller.remove_task(task_id)
        
        if task_info:
            coro = self._cleanup_task_data(task_info)
            if self.shared_state and getattr(self.shared_state, "loop", None) and self.shared_state.loop.is_running():
                self.shared_state.create_background_task(coro)
            else:
                try:
                    coro.close()
                except Exception as e:
                    logger.debug(f"Failed to close unscheduled cleanup coroutine: {e}")

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
        
        if task_id in self._active_sub_tasks:
            self._active_sub_tasks[task_id].clear()
        # Background task for pausing sub-tasks, protected against GC
        self.shared_state.create_background_task(self.controller.pause_all_sub_tasks(task_id))
        
        logger.debug(f"Task {task_id} marked as paused (Sub-tasks cancelled: {len(sub_tasks)}).")

    # --- Resume Logic (Complex, kept here for now as it orchestrates both) ---

    async def resume_transfer(self, task_id: str, progress_callback: Callable):
        self.shared_state.active_tasks[task_id] = asyncio.current_task()

        try:
            task_info = await self.controller.get_task_async(task_id)
            if not task_info: return

            await self.controller.mark_resumed_async(task_id)
            client = await utils.ensure_client_connected(self.shared_state)
            if not client:
                await self.controller.mark_failed_async(task_id, ErrorCode.CONNECTION_FAILED)
                progress_callback(task_id, task_info.get('name', ''), 0, task_info.get('total_size', 0), 'failed', 0, error_code=ErrorCode.CONNECTION_FAILED)
                return
            
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
                            pre_calculated_hash=task_info.get('file_hash'),
                            known_total_size=task_info.get('total_size'),
                            known_file_details=task_info.get('file_details')
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
                                pre_calculated_hash=sub_data.get('file_hash'),
                                known_total_size=sub_data.get('total_size'),
                                known_file_details=sub_data.get('file_details')
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
                    logger.debug(f"Resume task {task_id} caught cancellation signal in child tasks.")
                    raise res # Jump to outer CancelledError handler

            failed_exceptions = [res for res in results if isinstance(res, Exception)]
            if failed_exceptions:
                logger.error(f"Resume task {task_id} encountered sub-task exceptions: {failed_exceptions}")
                await self.controller.mark_failed_async(task_id, ErrorCode.TASK_FAILED)
                progress_callback(task_id, task_info.get('name', ''), 0, task_info.get('total_size', 0), 'failed', 0, error_code=ErrorCode.TASK_FAILED)
                return

            # Check persisted status in case child tasks caught errors and marked the task failed
            latest_info = await self.controller.get_task_async(task_id)
            if latest_info and latest_info.get('status') in ('cancelled', 'failed', 'paused'):
                logger.debug(f"Resume task {task_id} ended with status '{latest_info.get('status')}', skipping completion.")
                return
            
            if is_upload:
                 await self.uploader._finalize_thumbnails(client, task_id)
                 await self.metadata_manager.sync_db_to_cloud(client, self.shared_state.group_id, self.shared_state.api_id)

            await self.controller.mark_sub_task_completed_async(task_id, task_id)
            progress_callback(task_id, task_info.get('name', ''), task_info.get('total_size', 0), task_info.get('total_size', 0), 'completed', 0)

        except asyncio.CancelledError:
            logger.debug(f"Resume task {task_id} cancelled/paused.")
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

    @staticmethod
    def _expected_part_count(total_size: int) -> int:
        if total_size and total_size > 0:
            return max(1, (total_size + fp.CHUNK_SIZE - 1) // fp.CHUNK_SIZE)
        return 0

    def _is_upload_persisted_in_main_db(
        self,
        main_task_id: str,
        file_path: Optional[str],
        parent_id: Optional[int],
        total_size: int,
        file_hash: Optional[str],
        transferred_parts: List[Any],
        has_file_artifact: bool,
    ) -> bool:
        """Return True when upload DB evidence and main_db confirm the file is already stored."""
        if parent_id is None or not file_path or not file_hash:
            return False
        expected_parts = self._expected_part_count(total_size)
        has_all_parts = (expected_parts == 0 and bool(file_hash)) or (
            expected_parts > 0 and len(transferred_parts or []) >= expected_parts
        )
        if not (has_all_parts or has_file_artifact):
            return False
        file_name = os.path.basename(file_path)
        try:
            return bool(self.file_repo.has_file_in_folder(parent_id, file_name, file_hash=file_hash, require_map=True))
        except sqlite3.OperationalError as e:
            logger.debug(f"Skipping upload reconciliation for task {main_task_id}; main_db not ready: {e}")
            return False

    def reconcile_completed_uploads(self) -> int:
        """Promote stale incomplete upload tasks whose files are already fully persisted in main_db."""
        all_tasks = self.controller.get_incomplete_transfers()
        uploads = all_tasks.get("uploads", {}) if isinstance(all_tasks, dict) else {}
        reconciled = 0

        for task_id, info in uploads.items():
            status = info.get("status")
            if status in ("completed", "failed", "cancelled"):
                continue
            if self._active_sub_tasks.get(task_id):
                continue
            active_tasks = getattr(self.shared_state, "active_tasks", None) or {}
            active_task = active_tasks.get(task_id)
            if active_task and not active_task.done():
                continue

            artifacts = self.controller.get_created_artifacts(task_id)
            has_file_artifact = isinstance(artifacts, list) and any(
                isinstance(a, dict) and a.get("artifact_type") == "file" for a in artifacts
            )

            if not info.get("is_folder"):
                if self._is_upload_persisted_in_main_db(
                    task_id,
                    info.get("file_path"),
                    info.get("parent_id"),
                    info.get("total_size") or 0,
                    info.get("file_hash"),
                    info.get("transferred_parts") or [],
                    has_file_artifact,
                ):
                    self.controller.mark_sub_task_completed(task_id, task_id)
                    self.watcher.add_watch(task_id, info.get("parent_id"), "remote")
                    reconciled += 1
            else:
                child_tasks = info.get("child_tasks") or {}
                if not child_tasks:
                    continue
                all_children_completed = True
                for sub_id, sub_data in child_tasks.items():
                    if sub_data.get("status") == "completed":
                        continue
                    if self._is_upload_persisted_in_main_db(
                        task_id,
                        sub_data.get("file_path"),
                        sub_data.get("parent_id"),
                        sub_data.get("total_size") or 0,
                        sub_data.get("file_hash"),
                        sub_data.get("transferred_parts") or [],
                        has_file_artifact,
                    ):
                        self.controller.mark_sub_task_completed(task_id, sub_id)
                        reconciled += 1
                    else:
                        all_children_completed = False
                if all_children_completed:
                    self.controller.mark_sub_task_completed(task_id, task_id)
                    if info.get("parent_id") is not None:
                        self.watcher.add_watch(task_id, info.get("parent_id"), "remote")

        if reconciled > 0:
            logger.info(f"Reconciled {reconciled} stale already-uploaded task(s) to completed.")
        return reconciled

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        """Reconcile any already-persisted stale uploads before returning transfer DB state."""
        self.reconcile_completed_uploads()
        return self.controller.get_incomplete_transfers()

    def set_concurrency_limit(self, limit: int):
        """Dynamically update concurrency limit without dropping waiting tasks."""
        limit = _clamp_concurrency_limit(limit, default=MIN_CONCURRENCY)
        diff = limit - self.concurrency_limit
        self.concurrency_limit = limit
        
        if diff > 0:
            for _ in range(diff):
                self._semaphore.release()
        elif diff < 0:
            # Drain excess quota asynchronously so we don't block
            async def drain():
                for _ in range(abs(diff)):
                    await self._semaphore.acquire()
            # Background task to drain the buffer without blocking the caller
            self.shared_state.create_background_task(drain())
            
        logger.debug(f"Concurrency limit updated to {self.concurrency_limit}")

    def reorder_queued_tasks(self, ordered_task_ids: list[str]) -> Dict[str, Any]:
        """Reorder queued transfer waiters according to requested task ID order."""
        if not isinstance(ordered_task_ids, list):
            return {"success": False, "reordered": 0}
        reordered = 0
        if hasattr(self._semaphore, "reorder_waiters"):
            reordered = self._semaphore.reorder_waiters(ordered_task_ids)
        return {"success": True, "reordered": reordered}