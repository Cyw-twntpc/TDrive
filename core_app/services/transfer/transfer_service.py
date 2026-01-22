import logging
import asyncio
import os
import uuid
import time
from typing import TYPE_CHECKING, List, Dict, Any, Callable, Optional
from collections import defaultdict

if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState
    from ..media.gallery_manager import GalleryManager
    from core_app.data.metadata_manager import MetadataManager

from ..common import utils
from .transfer_controller import TransferController
from ..file_system.file_status_watcher import FileStatusWatcher
from ..media.image_processor import ImageProcessor
from ..media.gallery_manager import GalleryManager
from core_app.api import telegram_comms, crypto_handler
from core_app.common import errors
from core_app.data.db_handler import DatabaseHandler
from core_app.api import file_processor as fp

logger = logging.getLogger(__name__)

CONCURRENCY_LIMIT = 3

class TransferService:
    def __init__(self, shared_state: 'SharedState', gallery_manager: 'GalleryManager', metadata_manager: 'MetadataManager'):
        self.shared_state = shared_state
        self.db = DatabaseHandler()
        self.metadata_manager = metadata_manager # Injected
        self.controller = TransferController()
        self.watcher = FileStatusWatcher(self.shared_state.loop, self.db, status_change_callback=lambda x: None)
        self.gallery_manager = gallery_manager
        self._semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        self._active_sub_tasks: Dict[str, set] = defaultdict(set)
        
        self._refresh_callback: Optional[Callable] = None
        self._last_refresh_time = 0
        self._pending_refresh_folders = set()
        self._refresh_timer_task: Optional[asyncio.TimerHandle] = None

        self.controller.reset_zombie_tasks()
        all_tasks = self.controller.get_incomplete_transfers()
        self.watcher.load_initial_watches(all_tasks['uploads'], all_tasks['downloads'])

    def set_refresh_callback(self, callback: Callable):
        self._refresh_callback = callback

    def _trigger_folder_refresh(self, folder_ids: List[int]):
        """
        Throttled trigger for folder list refresh. 
        Ensures signal is sent at most once per second.
        """
        if not self._refresh_callback or not folder_ids:
            return

        # Filter out None/0 if any
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
        """Helper to normalize paths for dictionary lookups, removing Windows long path prefix."""
        if not path:
            return ""
        # Remove any variation of the Windows long path prefix
        p = path.replace(r'\\?\\', '').replace('\\\\?\\', '')
        # Ensure it's an absolute path and normalized for the OS
        return os.path.normpath(os.path.abspath(p))

    async def _finalize_thumbnails(self, client, main_task_id: str):
        """
        Aggregates thumbnails from transfer DB, updates thumbs.db (downloading old if needed),
        and uploads the new version.
        """
        try:
            loop = asyncio.get_running_loop()
            logger.info(f"Processing thumbnails for task {main_task_id}...")
            thumbs = await loop.run_in_executor(None, self.controller.db.get_task_thumbnails, main_task_id)
            
            if thumbs:
                thumbs_by_folder = defaultdict(dict)
                for item in thumbs:
                    thumbs_by_folder[item['target_folder_id']][item['file_id']] = item['thumbnail_blob']
                
                for f_id, new_thumbs_map in thumbs_by_folder.items():
                    if not self.gallery_manager.has_db(f_id):
                        db_info = await loop.run_in_executor(None, self._get_folder_db_info, f_id)
                        if db_info and db_info['thumbs_db_msg_id']:
                            logger.info(f"Downloading existing thumbs.db for folder {f_id} before update...")
                            old_db_bytes = await telegram_comms.download_data_as_bytes(
                                client, self.shared_state.group_id, [db_info['thumbs_db_msg_id']], db_info['thumbs_db_hash']
                            )
                            if old_db_bytes:
                                self.gallery_manager.load_thumbs_db_from_bytes(f_id, old_db_bytes)

                    logger.info(f"Updating thumbs.db for folder {f_id} with {len(new_thumbs_map)} new items.")
                    
                    db_bytes = self.gallery_manager.update_thumbs_db(f_id, new_thumbs_map)
                    
                    if db_bytes:
                        db_hash = await loop.run_in_executor(None, crypto_handler.hash_bytes, db_bytes)
                        
                        def hidden_progress(current, total):
                            asyncio.create_task(self.controller.update_transferred_bytes(current))

                        upload_info = await telegram_comms.upload_data_as_file(
                            client, self.shared_state.group_id, db_bytes, db_hash,
                            progress_callback=hidden_progress
                        )
                        
                        if upload_info:
                            msg_id = upload_info[0][1]
                            await loop.run_in_executor(None, self.db.update_folder_thumbs_info, f_id, msg_id, db_hash)
                
                await loop.run_in_executor(None, self.controller.db.delete_task_thumbnails, main_task_id)
                
        except Exception as e:
            logger.error(f"Error during thumbnail DB sync for task {main_task_id}: {e}", exc_info=True)

    def _get_folder_db_info(self, folder_id):
        conn = self.db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id = ?", (folder_id,))
        return cur.fetchone()

    # --- UPLOAD OPERATIONS ---

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], progress_callback: Callable):
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Upload failed: Client not connected.")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗")
            return

        # Suppress sync during batch upload
        self.db.sync_manager.set_busy(True)
        
        try:
            tasks_to_run = []
            for item in upload_items:
                task_id = item['task_id']
                file_path = item['local_path']
                file_name = os.path.basename(file_path)
                
                try:
                    total_size = os.path.getsize(file_path)
                except OSError:
                    total_size = 0

                self.controller.add_upload_task(
                    task_id, file_path, parent_id, total_size, is_folder=False, file_hash=None
                )

                progress_callback(task_id, file_name, 0, total_size, 'queued', 0, is_folder=False)

                tasks_to_run.append(
                    self._upload_single_file(
                        client, task_id, task_id, 
                        file_path, parent_id,
                        progress_callback
                    )
                )
                
            await asyncio.gather(*tasks_to_run, return_exceptions=True)

            # Sync DB Snapshot after batch upload (Explicit sync handled by set_busy(False) logic if needed, 
            # or we can keep explicit sync here to be sure, but set_busy(False) triggers it if score > 0)
            # Keeping explicit sync call here is redundant if set_busy(False) works, but harmless.
            # Actually, `sync_db_to_cloud` inside MetadataManager handles the actual upload.
            # set_busy(False) calls _trigger_sync_now which calls the callback.
            # So we can remove explicit sync call if we trust the manager.
            # But wait, `upload_folder` logic below had an explicit sync.
            # Let's rely on set_busy(False) to trigger the catch-up sync.
            
            for item in upload_items:
                await self._finalize_thumbnails(client, item['task_id'])
        
        finally:
            self.db.sync_manager.set_busy(False)
            # Trigger final refresh after batch upload and sync
            self._trigger_folder_refresh([parent_id])

    async def upload_folder_recursive(self, parent_id: int, local_folder_path: str, main_task_id: str, progress_callback: Callable):
        base_folder_name = os.path.basename(local_folder_path)
        logger.info(f"Starting folder upload: '{local_folder_path}' (Task: {main_task_id})")
        self.shared_state.active_tasks[main_task_id] = asyncio.current_task()
        
        # Suppress sync during folder upload
        self.db.sync_manager.set_busy(True)

        try:
            total_size = 0
            file_list = []
            
            # Handle long paths on Windows
            scan_path = local_folder_path
            if os.name == 'nt' and not scan_path.startswith(r'\\?\\'):
                scan_path = r'\\?\\' + os.path.abspath(scan_path)

            for root, dirs, files in os.walk(scan_path):
                for f in files:
                    full_path = os.path.join(root, f)
                    try:
                        f_size = os.path.getsize(full_path)
                        total_size += f_size
                        # Store normalized path for consistency
                        stored_path = self._normalize_path(full_path)
                        file_list.append((stored_path, f_size))
                    except OSError as e:
                        logger.warning(f"[SizeCalc] Skipping file due to error: {full_path} - {e}")
            
            self.controller.add_upload_task(
                main_task_id, local_folder_path, parent_id, total_size, is_folder=True
            )

            progress_callback(main_task_id, base_folder_name, 0, total_size, 'queued', 0, is_folder=True)

            loop = asyncio.get_running_loop()
            client = await utils.ensure_client_connected(self.shared_state)
            if not client: 
                self.controller.mark_failed(main_task_id, "Client disconnected")
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message="連線失敗")
                return

            try:
                try:
                    root_remote_id = await loop.run_in_executor(None, self.db.add_folder, parent_id, base_folder_name)
                    self.controller.record_created_artifact(main_task_id, 'folder', root_remote_id)
                except errors.ItemAlreadyExistsError:
                    existing = await loop.run_in_executor(None, self.db.get_folder_contents, parent_id)
                    found = next((f for f in existing['folders'] if f['name'] == base_folder_name), None)
                    if found:
                        root_remote_id = found['id']
                    else:
                        raise Exception("資料夾已存在但無法找到。")

                # Normalize local_folder_path for consistent mapping
                local_folder_normalized = self._normalize_path(local_folder_path).lower()
                path_to_remote_id = {local_folder_normalized: root_remote_id}
                
                # Use standard abspath for os.walk, but ensure prefix is clean if needed
                scan_path_structure = os.path.abspath(local_folder_path)
                if os.name == 'nt' and not scan_path_structure.startswith(r'\\?\\'):
                    scan_path_structure = r'\\?\\' + scan_path_structure
                
                for root, dirs, _ in os.walk(scan_path_structure):
                    current_root_key = self._normalize_path(root).lower()

                    current_remote_id = path_to_remote_id.get(current_root_key)
                    if current_remote_id is None:
                        continue

                    for d in dirs:
                        # Construct full local path for the subdirectory
                        real_dir_path = os.path.join(root, d)
                        local_dir_key = self._normalize_path(real_dir_path).lower()
                        
                        try:
                            new_folder_id = await loop.run_in_executor(None, self.db.add_folder, current_remote_id, d)
                            self.controller.record_created_artifact(main_task_id, 'folder', new_folder_id)
                            path_to_remote_id[local_dir_key] = new_folder_id
                        except errors.ItemAlreadyExistsError:
                            contents = await loop.run_in_executor(None, self.db.get_folder_contents, current_remote_id)
                            found = next((f for f in contents['folders'] if f['name'] == d), None)
                            if found:
                                path_to_remote_id[local_dir_key] = found['id']

                # Trigger refresh so the newly created folders appear immediately
                # Include -1 to update the folder tree in the sidebar
                self._trigger_folder_refresh([parent_id, -1])

                child_tasks_map = {}
                tasks_to_run = []
                real_total_size = 0
                
                for file_path, f_size in file_list:
                    sub_task_id = str(uuid.uuid4())
                    # Ensure lookup key matches the creation key
                    file_dir = self._normalize_path(os.path.dirname(file_path)).lower() 
                    
                    target_parent_id = path_to_remote_id.get(file_dir)
                    
                    if target_parent_id:
                        real_total_size += f_size
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
                                progress_callback,
                                defer_map_processing=True
                            )
                        )
                    else:
                        logger.warning(f"[TaskGen] Skipping file {file_path} because parent folder ID not found.")

                if real_total_size != total_size:
                    self.controller.update_task_total_size(main_task_id, real_total_size)
                    total_size = real_total_size
                    progress_callback(main_task_id, base_folder_name, 0, total_size, 'transferring', 0)

                self.controller.add_child_tasks_bulk(main_task_id, child_tasks_map)

                progress_callback(main_task_id, base_folder_name, -1, total_size, 'transferring', 0)
                
                # Execute uploads in parallel with deferred map processing
                results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
                
                # Batch Process Map Files
                logger.info(f"Processing batch map updates for task {main_task_id}...")
                
                transfers_by_folder = defaultdict(dict) # { folder_id: { fid: chunks } }
                
                # ...
                
                # Execute Batch Updates
                for folder_id, file_map in transfers_by_folder.items():
                    if file_map:
                        await self.metadata_manager.batch_process_file_transfers(
                            client, self.shared_state.group_id, self.shared_state.api_id,
                            folder_id, file_map
                        )
                
                # Trigger refresh for all folders involved in batch update
                self._trigger_folder_refresh(list(transfers_by_folder.keys()) + [parent_id])

                # Sync handled by finally block
                
                await self._finalize_thumbnails(client, main_task_id)
                
                task_info = self.controller.get_task(main_task_id)
                if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
                    self.controller.mark_sub_task_completed(main_task_id, main_task_id)
                    progress_callback(main_task_id, base_folder_name, 0, total_size, 'completed', 0)
                    self.watcher.add_watch(main_task_id, parent_id, 'remote')

            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    logger.info(f"Folder upload cancelled/paused: {main_task_id}")
                    raise
                logger.error(f"Folder upload failed: {e}", exc_info=True)
                self.controller.mark_failed(main_task_id, str(e))
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message=str(e))
        finally:
            if main_task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[main_task_id]
            
            # Clean up active sub-tasks tracking container
            self._active_sub_tasks.pop(main_task_id, None)
            
            # Ensure busy mode is disabled
            self.db.sync_manager.set_busy(False)

    async def _upload_single_file(self, client, main_task_id: str, sub_task_id: str,
                                  file_path: str, parent_id: int, 
                                  progress_callback: Callable,
                                  resume_context: List = None, pre_calculated_hash: str = None,
                                  defer_map_processing: bool = False):
        file_name = os.path.basename(file_path)
        
        def chunk_cb(part_num, msg_id, part_hash):
            self.controller.update_progress(main_task_id, sub_task_id, part_num, [msg_id, part_hash])

        last_uploaded = 0
        try:
            task_info = self.controller.get_task(main_task_id)
            sub_status = None
            if task_info:
                if task_info.get('is_folder'):
                    child = task_info.get('child_tasks', {}).get(sub_task_id)
                    if child: sub_status = child.get('status')
                elif main_task_id == sub_task_id:
                    sub_status = task_info.get('status')

            if sub_status == 'completed':
                last_uploaded = total_size
            elif resume_context:
                last_uploaded = len(resume_context) * fp.CHUNK_SIZE
                if last_uploaded > total_size:
                    last_uploaded = total_size
        except Exception as e:
            logger.warning(f"Error initializing upload progress for {sub_task_id}: {e}")

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
                asyncio.create_task(self.controller.update_transferred_bytes(delta))
                progress_callback(main_task_id, delta, speed)

        async with self._semaphore:
            main_status = self.controller.db.get_main_task_status(main_task_id)
            if main_status in ['paused', 'cancelled', 'failed']:
                return

            try:
                current_task = asyncio.current_task()
                self.shared_state.active_tasks[sub_task_id] = current_task
                self._active_sub_tasks[main_task_id].add(sub_task_id)

                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"找不到檔案：{file_path}")

                # Manually set status to transferring so DB query can find it
                self.controller.db.update_sub_task_status(sub_task_id, "transferring")

                # Trigger refresh to show this file in the list as "Uploading"
                # Since it has acquired the semaphore, it's about to start.
                self._trigger_folder_refresh([parent_id])

                loop = asyncio.get_running_loop()
                total_size = os.path.getsize(file_path)

                original_file_hash = pre_calculated_hash
                split_files_info = resume_context or []

                if not original_file_hash:
                    original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    self.controller.set_file_hash(sub_task_id, original_file_hash)
                    
                existing_file_id = await loop.run_in_executor(None, self.db.find_file_by_hash, original_file_hash)
                
                # Deduplication Check
                if existing_file_id:
                    logger.info(f"Sec-upload (Deduplication) triggered for {file_name}")
                    try:
                        def _dedup_add():
                            fid = self.db.add_file(parent_id, file_name, time.time(), file_id=existing_file_id)
                            self.controller.record_created_artifact(main_task_id, 'file', fid)
                            return fid

                        await loop.run_in_executor(None, _dedup_add)
                        self.controller.mark_sub_task_completed(main_task_id, sub_task_id)
                        
                        remaining = total_size - last_uploaded
                        if remaining > 0: progress_callback(main_task_id, remaining, 0)
                        
                        if main_task_id == sub_task_id:
                            progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                            self.watcher.add_watch(main_task_id, parent_id, 'remote')
                            
                        # If deferred, return None as we handled it (no chunks needed for map update since file reused)
                        return None
                    except errors.ItemAlreadyExistsError:
                        self.controller.mark_sub_task_failed(main_task_id, sub_task_id, "檔案已存在")
                        return

                progress_callback(main_task_id, file_name, -1, -1, 'transferring', 0, is_folder=False)


                # --- Image Preview ---
                thumb_bytes, preview_bytes = None, None
                ext = os.path.splitext(file_path)[1].lower()
                if ext in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.ico', '.tiff'}:
                    thumb_bytes, preview_bytes = await loop.run_in_executor(None, ImageProcessor.process_image, file_path)
                
                preview_msg_id = None
                preview_hash = None

                if preview_bytes:
                    preview_hash = await loop.run_in_executor(None, crypto_handler.hash_bytes, preview_bytes)
                    def hidden_progress(current, total):
                        asyncio.create_task(self.controller.update_transferred_bytes(current))

                    preview_upload_info = await telegram_comms.upload_data_as_file(
                        client, self.shared_state.group_id, preview_bytes, preview_hash,
                        progress_callback=hidden_progress
                    )
                    if preview_upload_info:
                        preview_msg_id = preview_upload_info[0][1]
                        # Track for cleanup in case of cancellation
                        self.controller.set_preview_msg_id(sub_task_id, preview_msg_id)

                # --- Phase 1: Upload Chunks ---
                split_files_info = await telegram_comms.upload_file_to_cloud(
                    client, self.shared_state.group_id, file_path, original_file_hash, 
                    main_task_id,
                    progress_callback=ui_cb, 
                    resume_context=split_files_info,
                    chunk_callback=chunk_cb
                )

                # --- Phase 2: Process Map (Metadata Manager) ---
                def _pre_insert_file():
                    # Insert with NULL map_id first
                    return self.db.add_file(
                        parent_id, file_name, time.time(), 
                        file_hash=original_file_hash, size=total_size, 
                        preview_msg_id=preview_msg_id, preview_hash=preview_hash,
                        map_id=None # Allowed temporarily
                    )
                
                fid = await loop.run_in_executor(None, _pre_insert_file)
                self.controller.record_created_artifact(main_task_id, 'file', fid)
                
                if thumb_bytes:
                    self.controller.db.add_task_thumbnail(main_task_id, parent_id, fid, thumb_bytes)
                
                self.controller.mark_sub_task_completed(main_task_id, sub_task_id)

                if defer_map_processing:
                    return (fid, split_files_info)

                # If not deferred, process immediately
                await self.metadata_manager.process_file_transfer(
                    client, self.shared_state.group_id, self.shared_state.api_id,
                    parent_id, fid, split_files_info
                )
                
                if main_task_id == sub_task_id:
                    # Single file upload -> Sync DB now
                    await self.metadata_manager.sync_db_to_cloud(client, self.shared_state.group_id, self.shared_state.api_id)
                    progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                    self.watcher.add_watch(main_task_id, parent_id, 'remote')

            except asyncio.CancelledError:
                logger.info(f"Upload task cancelled: {file_name}")
                raise
            except Exception as e:
                logger.error(f"File upload error '{file_name}': {e}", exc_info=True)
                self.controller.mark_sub_task_failed(main_task_id, sub_task_id, str(e))
            finally:
                if sub_task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[sub_task_id]
                self._active_sub_tasks[main_task_id].discard(sub_task_id)
                # If standalone task, remove the empty set to prevent leak
                if main_task_id == sub_task_id:
                    self._active_sub_tasks.pop(main_task_id, None)

    # --- DOWNLOAD OPERATIONS ---

    async def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable):
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return

        tasks_to_run = []
        for item in items:
            if item['type'] == 'folder':
                tasks_to_run.append(
                    self._download_folder(client, item['task_id'], item, destination_dir, progress_callback)
                )
            else:
                task_id = item['task_id']
                db_id = item['db_id']
                
                loop = asyncio.get_running_loop()
                
                # Fetch basic details from DB
                file_details_basic = await loop.run_in_executor(None, self.db.get_file_details, db_id)
                if not file_details_basic: 
                    progress_callback(task_id, item['name'], 0, 0, 'failed', 0, message="找不到檔案資訊")
                    continue

                # Fetch Chunks from Metadata Manager (Cloud Map)
                file_id = file_details_basic['file_id']
                chunks = await self.metadata_manager.get_file_chunks(client, self.shared_state.group_id, self.shared_state.api_id, file_id)
                
                # Construct full file details
                file_details = {
                    "name": file_details_basic['name'],
                    "size": file_details_basic['size'],
                    "hash": file_details_basic['hash'],
                    "chunks": [{"part_num": c[0], "message_id": c[1], "part_hash": c[2]} for c in chunks]
                }

                save_path = await loop.run_in_executor(None, fp.get_unique_filepath, destination_dir, file_details['name'])

                self.controller.add_download_task(
                    task_id, db_id, save_path, file_details['size'], is_folder=False, file_details=file_details
                )
                
                progress_callback(task_id, file_details['name'], 0, file_details['size'], 'queued', 0, is_folder=False)

                tasks_to_run.append(
                    self._download_single_item(
                        client, task_id, task_id, 
                        save_path, file_details, 
                        progress_callback
                    )
                )
        
        await asyncio.gather(*tasks_to_run, return_exceptions=True)

    async def _download_folder(self, client, main_task_id: str, folder_item: Dict, dest_path: str, progress_callback: Callable):
        self.shared_state.active_tasks[main_task_id] = asyncio.current_task()

        try:
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
                        "name": item['name']
                    })

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
                    "chunks": [], 
                    "file_id": f['file_id'] 
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

            progress_callback(main_task_id, root_folder_name, -1, total_size, 'transferring', 0)
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            
            self.controller.mark_sub_task_completed(main_task_id, main_task_id)
            progress_callback(main_task_id, root_folder_name, 0, total_size, 'completed', 0)
            self.watcher.add_watch(main_task_id, local_root_path, 'local')

        except Exception as e:
            logger.error(f"Download folder failed: {e}", exc_info=True)
            self.controller.mark_failed(main_task_id, str(e))
        finally:
            if main_task_id in self.shared_state.active_tasks:
                del self.shared_state.active_tasks[main_task_id]
            self._active_sub_tasks.pop(main_task_id, None)

    async def _download_single_item(self, client, main_task_id: str, sub_task_id: str, 
                                    save_path: str, file_details: Dict,
                                    progress_callback: Callable,
                                    resume_parts: set = None):
        def chunk_cb(part_num):
            self.controller.update_progress(main_task_id, sub_task_id, part_num)
        
        last_downloaded = 0
        try:
            task_info = self.controller.get_task(main_task_id)
            sub_status = None
            if task_info:
                if task_info.get('is_folder'):
                    child = task_info.get('child_tasks', {}).get(sub_task_id)
                    if child: sub_status = child.get('status')
                elif main_task_id == sub_task_id:
                    sub_status = task_info.get('status')

            if sub_status == 'completed':
                last_downloaded = file_details['size']
            elif resume_parts:
                last_downloaded = len(resume_parts) * fp.CHUNK_SIZE
                if last_downloaded > file_details['size']:
                    last_downloaded = file_details['size']
        except Exception as e:
            logger.warning(f"Error initializing download progress for {sub_task_id}: {e}")

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
                asyncio.create_task(self.controller.update_transferred_bytes(delta))
                progress_callback(main_task_id, delta, speed)

        async with self._semaphore:
            main_status = self.controller.db.get_main_task_status(main_task_id)
            if main_status in ['paused', 'cancelled', 'failed']:
                return

            try:
                self.shared_state.active_tasks[sub_task_id] = asyncio.current_task()
                self._active_sub_tasks[main_task_id].add(sub_task_id)

                # JIT Chunk Fetching
                if not file_details.get('chunks') and file_details.get('file_id'):
                    chunks = await self.metadata_manager.get_file_chunks(
                        client, self.shared_state.group_id, self.shared_state.api_id, file_details['file_id']
                    )
                    file_details['chunks'] = [{"part_num": c[0], "message_id": c[1], "part_hash": c[2]} for c in chunks]

                if not file_details.get('chunks'):
                    raise Exception("Unable to retrieve file chunks from cloud.")

                progress_callback(main_task_id, file_details['name'], -1, -1, 'transferring', 0)

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
                    self.watcher.add_watch(main_task_id, save_path, 'local')

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Download failed {save_path}: {e}")
                self.controller.mark_sub_task_failed(main_task_id, sub_task_id, str(e))
            finally:
                if sub_task_id in self.shared_state.active_tasks:
                    del self.shared_state.active_tasks[sub_task_id]
                self._active_sub_tasks[main_task_id].discard(sub_task_id)
                # Clean up tracking set for standalone tasks
                if main_task_id == sub_task_id:
                    self._active_sub_tasks.pop(main_task_id, None)

    # --- CONTROL METHODS ---

    def get_transfer_config(self) -> Dict[str, Any]:
        return {
            "todayTraffic": self.controller.get_today_traffic(),
            "chunkSize": fp.CHUNK_SIZE
        }

    async def resume_transfer(self, task_id: str, progress_callback: Callable):
        self.shared_state.active_tasks[task_id] = asyncio.current_task()

        try:
            task_info = self.controller.get_task(task_id)
            if not task_info: return

            self.controller.mark_resumed(task_id)
            client = await utils.ensure_client_connected(self.shared_state)
            if not client: return
            
            # Calculate initial progress for accurate resume UI
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

            # Send one-time global correction packet
            progress_callback(task_id, task_info.get('name', ''), initial_progress, task_info.get('total_size', 0), 'transferring', 0) 

            tasks_to_run = []
            
            if not task_info.get("is_folder"):
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
                                pre_calculated_hash=sub_data.get('file_hash')
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

            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            
            if task_info['type'] == 'upload':
                 await self._finalize_thumbnails(client, task_id)
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

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        self.pause_transfer(task_id)
        self.watcher.remove_watch(task_id)
        
        if task_id in self._active_sub_tasks:
            del self._active_sub_tasks[task_id]
        
        task_info = self.controller.get_task(task_id)
        self.controller.remove_task(task_id)
        
        if task_info:
            # Trigger refresh for the parent folder to remove the uploading entry
            if task_info.get('remote_id'):
                 self._trigger_folder_refresh([task_info['remote_id']])
            
            asyncio.run_coroutine_threadsafe(self._cleanup_task_data(task_info), self.shared_state.loop)

        return {"success": True, "message": "任務已取消並開始背景清理。"}

    def remove_history_item(self, task_id: str) -> Dict[str, Any]:
        self.controller.remove_task(task_id)
        self.watcher.remove_watch(task_id)
        return {"success": True, "message": "歷史項目已移除。"}

    def shutdown(self):
        if self.watcher:
            self.watcher.stop()

    async def _cleanup_task_data(self, task_info: Dict[str, Any]):
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

                # Suppress DB sync during batch cleanup - Start early
                self.db.sync_manager.set_busy(True)
                
                message_ids_to_delete = []
                affected_parents = set()

                try:
                    # Clean up created DB artifacts via Metadata Manager (handles map cleanup)
                    task_id = task_info.get('task_id')
                    logger.info(f"Cleanup: Starting artifact cleanup for task {task_id}")
                    if task_id:
                        artifacts = await self.shared_state.loop.run_in_executor(None, self.controller.get_created_artifacts, task_id)
                        
                        def _cleanup_db_items():
                            results = []
                            for artifact in artifacts:
                                try:
                                    if artifact['artifact_type'] == 'file':
                                        res = self.db.remove_file(artifact['db_id'])
                                        if res: results.append(res)
                                    elif artifact['artifact_type'] == 'folder':
                                        res_list = self.db.remove_folder(artifact['db_id'])
                                        results.extend(res_list)
                                except Exception: pass
                            return results

                        deletion_results = await self.shared_state.loop.run_in_executor(None, _cleanup_db_items)
                        
                        # Collect affected parents for list refresh
                        for res in deletion_results:
                            if res.get('parent_id'):
                                affected_parents.add(res['parent_id'])
                        
                        # Process cloud map cleanup
                        await self.metadata_manager.handle_deletion(client, self.shared_state.group_id, self.shared_state.api_id, deletion_results)
                        
                        # Collect extra msg ids (thumbs, previews etc)
                        for res in deletion_results:
                            if res.get('msg_ids_to_delete'):
                                message_ids_to_delete.extend(res['msg_ids_to_delete'])

                    def collect_cleanup_info(info):
                        # Collect Chunk Message IDs
                        if info.get('split_files_info'):
                            for item in info['split_files_info']:
                                if len(item) >= 2:
                                    message_ids_to_delete.append(item[1])
                        # Collect Preview Message ID (Recently added tracking)
                        if info.get('preview_msg_id'):
                            message_ids_to_delete.append(info['preview_msg_id'])

                    if is_folder:
                        child_tasks = task_info.get('child_tasks', {})
                        for child in child_tasks.values():
                            collect_cleanup_info(child)
                    else:
                        collect_cleanup_info(task_info)

                    if message_ids_to_delete:
                        unique_ids = list(set(message_ids_to_delete))
                        batch_size = 100
                        for i in range(0, len(unique_ids), batch_size):
                            batch = unique_ids[i:i + batch_size]
                            try:
                                await client.delete_messages(self.shared_state.group_id, batch)
                            except Exception as del_err:
                                logger.error(f"Failed to delete messages in cleanup: {del_err}")
                finally:
                    self.db.sync_manager.set_busy(False) # Release and trigger ONE sync
                            
            logger.info("Cleanup completed successfully.")
            # Trigger refresh for all affected folders + dummy ID for tree update
            refresh_ids = list(affected_parents) + [-1]
            
            # Fix: Check parent_id (upload) or db_id (download) as remote_id key doesn't exist in get_task result
            main_remote_id = task_info.get('parent_id') or task_info.get('db_id')
            if main_remote_id:
                refresh_ids.append(main_remote_id)
            
            self._trigger_folder_refresh(refresh_ids)

        except Exception as e:
            logger.error(f"Error during task cleanup: {e}", exc_info=True)
