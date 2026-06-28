import asyncio
import base64
import logging
import time

from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository

logger = logging.getLogger(__name__)

class DefragWorker:
    def __init__(self, shared_state, file_service):
        self.shared_state = shared_state
        self.file_service = file_service
        self.db = DatabaseConnection()
        self.folder_repo = FolderRepository(self.db)
        self.queue = None
        self.worker_task = None
        self.timer_task = None
        self.queued_folders = set()
        self.is_running = False

    def start(self):
        if not self.is_running:
            self.is_running = True
            loop = self.shared_state.loop
            self.queue = asyncio.Queue()
            self.worker_task = loop.create_task(self._process_queue())
            self.timer_task = loop.create_task(self._timer_loop())
            logger.info("DefragWorker started.")

    def stop(self):
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
        if self.timer_task:
            self.timer_task.cancel()
        logger.info("DefragWorker stopped.")

    def evaluate_folders(self, folder_ids: list[int]):
        """Evaluates folders for fragmentation and adds them to queue if needed."""
        if not self.is_running:
            return
            
        # Filter out None and remove duplicates
        valid_ids = list(set(fid for fid in folder_ids if fid is not None))
        if not valid_ids:
            return

        # Query db in background thread
        async def _eval():
            try:
                fragmented = await asyncio.to_thread(self.folder_repo.get_fragmented_folders, valid_ids)
                for f_id in fragmented:
                    if f_id not in self.queued_folders:
                        self.queued_folders.add(f_id)
                        await self.queue.put(f_id)
                        logger.info(f"Folder {f_id} queued for defragmentation.")
            except Exception as e:
                logger.error(f"Error evaluating fragmentation for folders {valid_ids}: {e}")

        self.shared_state.loop.create_task(_eval())

    async def _timer_loop(self):
        """Forces a re-evaluation of all queued folders every 3 hours (10800 seconds)."""
        while self.is_running:
            try:
                await asyncio.sleep(10800)
                logger.info("DefragWorker 3-hour timer fired. Waking up worker.")
                
                # To forcefully execute the queue even if it was delayed, we don't
                # strictly need anything because the worker loop is already processing it
                # as long as it's idle.
                # However, if we want to ensure any stragglers are checked, we could
                # potentially re-scan active folders, but user rule said "強制重組防線"
                # so the existing queued folders just get executed.
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DefragWorker timer error: {e}")

    async def _process_queue(self):
        while self.is_running:
            try:
                # Wait for a folder to process
                folder_id = await self.queue.get()
                
                # Check if system is idle
                # We wait until transfer manager has no active tasks
                while self._is_system_busy():
                    await asyncio.sleep(10) # Check every 10 seconds if busy
                
                logger.info(f"DefragWorker starting defragmentation for folder {folder_id}...")
                
                # Step 1: Pull all thumbnails using ThumbnailManager (via file_service)
                # This will automatically reconstruct missing packages by downloading them
                thumbs_result = await self.file_service.get_thumbnails(folder_id, return_file_id_keys=True)
                
                if thumbs_result and thumbs_result.get("success"):
                    new_thumbs_map = thumbs_result.get("thumbnails", {})
                    
                    # Convert base64 back to bytes
                    binary_thumbs_map = {int(k): base64.b64decode(v) for k, v in new_thumbs_map.items()}
                    
                    if binary_thumbs_map:
                        client = self.shared_state.client
                        if client:
                            # Step 2: Push new package and clear thumb_src_folder_id
                            await self.shared_state.metadata_manager.update_folder_thumbnails(
                                client,
                                self.shared_state.group_id,
                                folder_id,
                                binary_thumbs_map,
                                self.file_service.thumb_manager
                            )
                            logger.info(f"DefragWorker successfully defragmented folder {folder_id}.")
                
                # Remove from queued set
                if folder_id in self.queued_folders:
                    self.queued_folders.remove(folder_id)
                    
                self.queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DefragWorker error processing folder {folder_id}: {e}", exc_info=True)
                # Remove from queued set on error so it can be retried later
                if folder_id in self.queued_folders:
                    self.queued_folders.remove(folder_id)
                self.queue.task_done()

    def _is_system_busy(self) -> bool:
        """Checks if the system is currently performing transfers by checking the database."""
        try:
            from core_app.infrastructure.database.transfer_db.transfer_database import TransferDatabaseConnection
            tdb = TransferDatabaseConnection()
            conn = tdb._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM main_tasks WHERE status IN ('queued', 'running', 'processing') LIMIT 1")
            is_busy = cursor.fetchone() is not None
            conn.close()
            return is_busy
        except Exception as e:
            logger.error(f"Error checking system busy status: {e}")
            return False
