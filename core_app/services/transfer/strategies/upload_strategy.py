import os
import time
import uuid
import asyncio
import logging
import sqlite3
from collections import defaultdict
from typing import List, Dict, Any, Callable

from .base_strategy import TransferStrategy
from ...common import utils
from core_app.api import telegram_comms, crypto_handler
from core_app.common import errors
from core_app.api import file_processor as fp
from ...media.image_processor import ImageProcessor
from ..metadata_extractor import extract_metadata

logger = logging.getLogger(__name__)

class UploadStrategy(TransferStrategy):
    
    async def start(self, task_type: str, *args, **kwargs):
        if task_type == 'files':
            return await self.upload_files(*args, **kwargs)
        elif task_type == 'folder':
            return await self.upload_folder_recursive(*args, **kwargs)

    async def upload_files(self, parent_id: int, upload_items: List[Dict[str, Any]], progress_callback: Callable):
        client = await utils.ensure_client_connected(self.context.shared_state)
        if not client:
            logger.error("Upload failed: Client not connected.")
            for item in upload_items:
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, message="連線失敗")
            return

        self.context.db.sync_manager.set_busy(True)
        
        try:
            tasks_to_run = []
            for item in upload_items:
                task_id = item['task_id']
                file_path = item['local_path']
                file_name = os.path.basename(file_path)
                
                try:
                    total_size = os.path.getsize(file_path)
                    meta = extract_metadata(file_path)
                except OSError:
                    total_size = 0
                    meta = None

                self.context.controller.add_upload_task(
                    task_id, file_path, parent_id, total_size, is_folder=False, file_hash=None, file_details=meta
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
            
            for item in upload_items:
                await self._finalize_thumbnails(client, item['task_id'])
        
        finally:
            self.context.db.sync_manager.set_busy(False)
            self.context._trigger_folder_refresh([parent_id])

    async def upload_folder_recursive(self, parent_id: int, local_folder_path: str, main_task_id: str, progress_callback: Callable):
        base_folder_name = os.path.basename(local_folder_path)
        logger.info(f"Starting folder upload: '{local_folder_path}' (Task: {main_task_id})")
        self.context.shared_state.active_tasks[main_task_id] = asyncio.current_task()
        
        self.context.db.sync_manager.set_busy(True)

        try:
            total_size = 0
            file_list = []
            
            scan_path = local_folder_path
            if os.name == 'nt' and not scan_path.startswith(r'\\?\\'):
                scan_path = r'\\?\\' + os.path.abspath(scan_path)

            for root, dirs, files in os.walk(scan_path):
                for f in files:
                    full_path = os.path.join(root, f)
                    try:
                        f_size = os.path.getsize(full_path)
                        total_size += f_size
                        stored_path = self.context._normalize_path(full_path)
                        file_list.append((stored_path, f_size))
                    except OSError as e:
                        logger.warning(f"[SizeCalc] Skipping file due to error: {full_path} - {e}")
            
            self.context.controller.add_upload_task(
                main_task_id, local_folder_path, parent_id, total_size, is_folder=True
            )

            progress_callback(main_task_id, base_folder_name, 0, total_size, 'queued', 0, is_folder=True)

            loop = asyncio.get_running_loop()
            client = await utils.ensure_client_connected(self.context.shared_state)
            if not client: 
                self.context.controller.mark_failed(main_task_id, "Client disconnected")
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message="連線失敗")
                return

            try:
                try:
                    root_remote_id = await loop.run_in_executor(None, self.context.db.add_folder, parent_id, base_folder_name)
                    self.context.controller.record_created_artifact(main_task_id, 'folder', root_remote_id)
                except errors.ItemAlreadyExistsError:
                    existing = await loop.run_in_executor(None, self.context.db.get_folder_contents, parent_id)
                    found = next((f for f in existing['folders'] if f['name'] == base_folder_name), None)
                    if found:
                        root_remote_id = found['id']
                    else:
                        raise Exception("資料夾已存在但無法找到。")

                local_folder_normalized = self.context._normalize_path(local_folder_path).lower()
                path_to_remote_id = {local_folder_normalized: root_remote_id}
                
                scan_path_structure = os.path.abspath(local_folder_path)
                if os.name == 'nt' and not scan_path_structure.startswith(r'\\?\\'):
                    scan_path_structure = r'\\?\\' + scan_path_structure
                
                for root, dirs, _ in os.walk(scan_path_structure):
                    current_root_key = self.context._normalize_path(root).lower()

                    current_remote_id = path_to_remote_id.get(current_root_key)
                    if current_remote_id is None:
                        continue

                    for d in dirs:
                        real_dir_path = os.path.join(root, d)
                        local_dir_key = self.context._normalize_path(real_dir_path).lower()
                        
                        try:
                            new_folder_id = await loop.run_in_executor(None, self.context.db.add_folder, current_remote_id, d)
                            self.context.controller.record_created_artifact(main_task_id, 'folder', new_folder_id)
                            path_to_remote_id[local_dir_key] = new_folder_id
                        except errors.ItemAlreadyExistsError:
                            contents = await loop.run_in_executor(None, self.context.db.get_folder_contents, current_remote_id)
                            found = next((f for f in contents['folders'] if f['name'] == d), None)
                            if found:
                                path_to_remote_id[local_dir_key] = found['id']

                self.context._trigger_folder_refresh([parent_id, -1])

                child_tasks_map = {}
                tasks_to_run = []
                real_total_size = 0
                
                for file_path, f_size in file_list:
                    sub_task_id = str(uuid.uuid4())
                    file_dir = self.context._normalize_path(os.path.dirname(file_path)).lower() 
                    target_parent_id = path_to_remote_id.get(file_dir)
                    
                    if target_parent_id:
                        real_total_size += f_size
                        meta = extract_metadata(file_path)
                        child_tasks_map[sub_task_id] = {
                            "file_path": file_path,
                            "parent_id": target_parent_id,
                            "total_size": f_size,
                            "file_details": meta,
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
                    self.context.controller.update_task_total_size(main_task_id, real_total_size)
                    total_size = real_total_size
                    progress_callback(main_task_id, base_folder_name, 0, total_size, 'transferring', 0)

                self.context.controller.add_child_tasks_bulk(main_task_id, child_tasks_map)

                progress_callback(main_task_id, base_folder_name, -1, total_size, 'transferring', 0)
                
                results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
                
                logger.info(f"Processing batch map updates for task {main_task_id}...")
                
                transfers_by_folder = defaultdict(dict)
                
                for res in results:
                    if isinstance(res, Exception):
                        logger.error(f"Sub-task failed: {res}")
                        continue
                    if res is None:
                        continue 
                    
                    fid, chunks, sub_id = res
                    if not chunks: continue
                    
                    file_details = None
                    task_data = self.context.controller.get_task(main_task_id)
                    if task_data and task_data.get('is_folder'):
                        child = task_data.get('child_tasks', {}).get(sub_id)
                        if child:
                            file_details = child.get('file_details')
                    
                    def _get_fid_parent():
                        conn = self.context.db._get_conn()
                        cur = conn.cursor()
                        cur.execute("SELECT folder_id FROM file_folder_map WHERE file_id = ?", (fid,))
                        row = cur.fetchone()
                        return row['folder_id'] if row else None
                    
                    folder_id = await loop.run_in_executor(None, _get_fid_parent)
                    if folder_id:
                        transfers_by_folder[folder_id][fid] = {'c': chunks, 'm': file_details or {}}

                for folder_id, file_map in transfers_by_folder.items():
                    if file_map:
                        await self.context.metadata_manager.batch_process_file_transfers(
                            client, self.context.shared_state.group_id, self.context.shared_state.api_id,
                            folder_id, file_map
                        )
                
                self.context._trigger_folder_refresh(list(transfers_by_folder.keys()) + [parent_id])
                
                await self._finalize_thumbnails(client, main_task_id)
                
                task_info = self.context.controller.get_task(main_task_id)
                if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
                    self.context.controller.mark_sub_task_completed(main_task_id, main_task_id)
                    progress_callback(main_task_id, base_folder_name, 0, total_size, 'completed', 0)
                    self.context.watcher.add_watch(main_task_id, parent_id, 'remote')

            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    logger.info(f"Folder upload cancelled/paused: {main_task_id}")
                    raise
                logger.error(f"Folder upload failed: {e}", exc_info=True)
                self.context.controller.mark_failed(main_task_id, str(e))
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, message=str(e))
        finally:
            if main_task_id in self.context.shared_state.active_tasks:
                del self.context.shared_state.active_tasks[main_task_id]
            self.context._active_sub_tasks.pop(main_task_id, None)
            self.context.db.sync_manager.set_busy(False)

    async def _upload_single_file(self, client, main_task_id: str, sub_task_id: str,
                                  file_path: str, parent_id: int, 
                                  progress_callback: Callable,
                                  resume_context: List = None, pre_calculated_hash: str = None,
                                  defer_map_processing: bool = False):
        file_name = os.path.basename(file_path)
        
        def chunk_cb(part_num, msg_id, part_hash):
            self.context.controller.update_progress(main_task_id, sub_task_id, part_num, [msg_id, part_hash])

        last_uploaded = 0
        try:
            task_info = self.context.controller.get_task(main_task_id)
            sub_status = None
            if task_info:
                if task_info.get('is_folder'):
                    child = task_info.get('child_tasks', {}).get(sub_task_id)
                    if child: sub_status = child.get('status')
                elif main_task_id == sub_task_id:
                    sub_status = task_info.get('status')

            if sub_status == 'completed':
                try:
                    last_uploaded = os.path.getsize(file_path)
                except OSError: last_uploaded = 0
            elif resume_context:
                last_uploaded = len(resume_context) * fp.CHUNK_SIZE
        except Exception as e:
            logger.warning(f"Error initializing upload progress for {sub_task_id}: {e}")

        last_update_time = time.time()

        def ui_cb(current, total):
            nonlocal last_uploaded, last_update_time
            
            # Detect retry/rollback
            if current <= last_uploaded:
                return
                
            delta = current - last_uploaded
            now = time.time()
            time_diff = now - last_update_time

            last_uploaded = current
            last_update_time = now
            speed = delta / time_diff if time_diff > 0 else 0
            asyncio.create_task(self.context.controller.update_transferred_bytes(delta))
            progress_callback(main_task_id, delta, speed)

        async with self.context._semaphore:
            main_status = self.context.controller.db.get_main_task_status(main_task_id)
            if main_status in ['paused', 'cancelled', 'failed']:
                return

            try:
                current_task = asyncio.current_task()
                self.context.shared_state.active_tasks[sub_task_id] = current_task
                self.context._active_sub_tasks[main_task_id].add(sub_task_id)

                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"找不到檔案：{file_path}")

                self.context.controller.db.update_sub_task_status(sub_task_id, "transferring")
                self.context._trigger_folder_refresh([parent_id])

                loop = asyncio.get_running_loop()
                total_size = os.path.getsize(file_path)

                original_file_hash = pre_calculated_hash
                split_files_info = resume_context or []

                if not original_file_hash:
                    original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    self.context.controller.set_file_hash(sub_task_id, original_file_hash)
                    
                existing_file_id = await loop.run_in_executor(None, self.context.db.find_file_by_hash, original_file_hash)
                
                if existing_file_id:
                    logger.info(f"Sec-upload (Deduplication) triggered for {file_name}")
                    try:
                        def _dedup_add():
                            fid, ui_id = self.context.db.add_file(parent_id, file_name, time.time(), file_id=existing_file_id)
                            self.context.controller.record_created_artifact(main_task_id, 'file', ui_id)
                            return fid

                        await loop.run_in_executor(None, _dedup_add)
                        self.context.controller.mark_sub_task_completed(main_task_id, sub_task_id)
                        
                        remaining = total_size - last_uploaded
                        if remaining > 0: progress_callback(main_task_id, remaining, 0)
                        
                        if main_task_id == sub_task_id:
                            await self.context.metadata_manager.sync_db_to_cloud(client, self.context.shared_state.group_id, self.context.shared_state.api_id)
                            progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                            self.context.watcher.add_watch(main_task_id, parent_id, 'remote')
                            
                        return None
                    except errors.ItemAlreadyExistsError:
                        self.context.controller.mark_sub_task_failed(main_task_id, sub_task_id, "檔案已存在")
                        return

                progress_callback(main_task_id, file_name, -1, -1, 'transferring', 0, is_folder=False)

                thumb_bytes, preview_bytes = None, None
                ext = os.path.splitext(file_path)[1].lower()
                if ext in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.ico', '.tiff'}:
                    thumb_bytes, preview_bytes = await loop.run_in_executor(None, ImageProcessor.process_image, file_path)
                
                preview_msg_id = None
                preview_hash = None

                if preview_bytes:
                    preview_hash = await loop.run_in_executor(None, crypto_handler.hash_bytes, preview_bytes)
                    def hidden_progress(current, total):
                        asyncio.create_task(self.context.controller.update_transferred_bytes(current))

                    preview_upload_info = await telegram_comms.upload_data_as_file(
                        client, self.context.shared_state.group_id, preview_bytes, preview_hash,
                        progress_callback=hidden_progress
                    )
                    if preview_upload_info:
                        preview_msg_id = preview_upload_info[0][1]
                        self.context.controller.set_preview_msg_id(sub_task_id, preview_msg_id)

                from core_app.services.transfer.dispatcher import TransferDispatcher
                clients_pool = getattr(self.context.shared_state, 'clients_pool', [])
                if not clients_pool:
                    clients_pool = [client]

                split_files_info = await TransferDispatcher.dispatch_upload(
                    clients_pool, self.context.shared_state.group_id, file_path, original_file_hash, 
                    progress_callback=ui_cb, 
                    resume_context=split_files_info,
                    chunk_callback=chunk_cb
                )

                def _pre_insert_file():
                    try:
                        return self.context.db.add_file(
                            parent_id, file_name, time.time(), 
                            file_hash=original_file_hash, size=total_size, 
                            preview_msg_id=preview_msg_id, preview_hash=preview_hash,
                            map_id=None 
                        )
                    except (sqlite3.IntegrityError, errors.ItemAlreadyExistsError):
                        # Race condition detected: File was added by another task concurrently.
                        # We treat this as a successful "sec-upload" (deduplication).
                        existing_id = self.context.db.find_file_by_hash(original_file_hash)
                        if existing_id:
                            # Link to the existing file record
                            return self.context.db.add_file(parent_id, file_name, time.time(), file_id=existing_id)
                        raise
                
                fid, ui_id = await loop.run_in_executor(None, _pre_insert_file)
                self.context.controller.record_created_artifact(main_task_id, 'file', ui_id)
                
                if thumb_bytes:
                    self.context.controller.db.add_task_thumbnail(main_task_id, parent_id, fid, thumb_bytes)
                
                self.context.controller.mark_sub_task_completed(main_task_id, sub_task_id)

                if defer_map_processing:
                    return (fid, split_files_info, sub_task_id)
                    
                task_data = self.context.controller.get_task(main_task_id)
                file_details = None
                if task_data:
                    if task_data.get('is_folder'):
                        child = task_data.get('child_tasks', {}).get(sub_task_id)
                        if child:
                            file_details = child.get('file_details')
                    elif main_task_id == sub_task_id:
                        file_details = task_data.get('file_details')

                await self.context.metadata_manager.process_file_transfer(
                    client, self.context.shared_state.group_id, self.context.shared_state.api_id,
                    parent_id, fid, split_files_info, metadata=file_details
                )
                
                if main_task_id == sub_task_id:
                    await self.context.metadata_manager.sync_db_to_cloud(client, self.context.shared_state.group_id, self.context.shared_state.api_id)
                    progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                    self.context.watcher.add_watch(main_task_id, parent_id, 'remote')

            except asyncio.CancelledError:
                logger.info(f"Upload task cancelled: {file_name}")
                raise
            except Exception as e:
                logger.error(f"File upload error '{file_name}': {e}", exc_info=True)
                self.context.controller.mark_sub_task_failed(main_task_id, sub_task_id, str(e))
            finally:
                if sub_task_id in self.context.shared_state.active_tasks:
                    del self.context.shared_state.active_tasks[sub_task_id]
                self.context._active_sub_tasks[main_task_id].discard(sub_task_id)
                if main_task_id == sub_task_id:
                    self.context._active_sub_tasks.pop(main_task_id, None)

    async def _finalize_thumbnails(self, client, main_task_id: str):
        try:
            loop = asyncio.get_running_loop()
            logger.info(f"Processing thumbnails for task {main_task_id}...")
            thumbs = await loop.run_in_executor(None, self.context.controller.db.get_task_thumbnails, main_task_id)
            
            if thumbs:
                thumbs_by_folder = defaultdict(dict)
                for item in thumbs:
                    thumbs_by_folder[item['target_folder_id']][item['file_id']] = item['thumbnail_blob']
                
                for f_id, new_thumbs_map in thumbs_by_folder.items():
                    # Delegate thread-safe update to MetadataManager
                    await self.context.metadata_manager.update_folder_thumbnails(
                        client, 
                        self.context.shared_state.group_id, 
                        f_id, 
                        new_thumbs_map,
                        self.context.gallery_manager
                    )
                
                await loop.run_in_executor(None, self.context.controller.db.delete_task_thumbnails, main_task_id)
                
        except Exception as e:
            logger.error(f"Error during thumbnail DB sync for task {main_task_id}: {e}", exc_info=True)

    async def cleanup(self, task_info: Dict[str, Any]):
        client = await utils.ensure_client_connected(self.context.shared_state)
        if not client: return

        # Suppress DB sync during batch cleanup
        self.context.db.sync_manager.set_busy(True)
        
        message_ids_to_delete = []
        affected_parents = set()

        try:
            # Clean up created DB artifacts via Metadata Manager (handles map cleanup)
            task_id = task_info.get('task_id')
            logger.info(f"Cleanup: Starting artifact cleanup for task {task_id}")
            if task_id:
                artifacts = await self.context.shared_state.loop.run_in_executor(None, self.context.controller.get_created_artifacts, task_id)
                
                def _cleanup_db_items():
                    results = []
                    for artifact in artifacts:
                        try:
                            if artifact['artifact_type'] == 'file':
                                res = self.context.db.remove_file(artifact['db_id'])
                                if res: results.append(res)
                            elif artifact['artifact_type'] == 'folder':
                                res_list = self.context.db.remove_folder(artifact['db_id'])
                                results.extend(res_list)
                        except Exception: pass
                    return results

                deletion_results = await self.context.shared_state.loop.run_in_executor(None, _cleanup_db_items)
                
                # Collect affected parents for list refresh
                for res in deletion_results:
                    if res.get('parent_id'):
                        affected_parents.add(res['parent_id'])
                
                # Process cloud map cleanup
                await self.context.metadata_manager.handle_deletion(client, self.context.shared_state.group_id, self.context.shared_state.api_id, deletion_results)
                
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
                # Collect Preview Message ID
                if info.get('preview_msg_id'):
                    message_ids_to_delete.append(info['preview_msg_id'])

            if task_info.get('is_folder'):
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
                        await client.delete_messages(self.context.shared_state.group_id, batch)
                    except Exception as del_err:
                        logger.error(f"Failed to delete messages in cleanup: {del_err}")
        finally:
            self.context.db.sync_manager.set_busy(False) # Release and trigger ONE sync
                    
        logger.info("Cleanup completed successfully.")
        
        refresh_ids = list(affected_parents) + [-1]
        
        # Check parent_id (upload) or db_id (download)
        main_remote_id = task_info.get('parent_id') or task_info.get('db_id')
        if main_remote_id:
            refresh_ids.append(main_remote_id)
        
        self.context._trigger_folder_refresh(refresh_ids)
