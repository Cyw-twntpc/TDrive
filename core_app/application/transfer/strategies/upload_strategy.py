from core_app.core.errors import ErrorCode
import os
import time
import uuid
import asyncio
import logging
import sqlite3
from collections import defaultdict
from typing import List, Dict, Any, Callable, Optional

from core_app.application.transfer.strategies.base_strategy import TransferStrategy
from core_app.core import utils
from core_app.infrastructure.telegram import telegram_comms
from core_app.core import crypto_handler
from core_app.core import errors
from core_app.infrastructure.local_fs import file_processor as fp
from core_app.application.file_system.thumbnail.image_generator import ImageThumbnailGenerator
from core_app.application.file_system.thumbnail.video_generator import VideoThumbnailGenerator
from core_app.application.preview.image.viewer import ImagePreviewer
from core_app.application.transfer.metadata_extractor import extract_metadata

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
                progress_callback(item['task_id'], os.path.basename(item['local_path']), 0, 0, 'failed', 0, error_code=ErrorCode.CONNECTION_FAILED)
            return

        self.context.db.sync_manager.set_busy(True)
        
        try:
            loop = asyncio.get_running_loop()
            tasks_to_run = []
            for item in upload_items:
                task_id = item['task_id']
                file_path = item['local_path']
                file_name = os.path.basename(file_path)
                
                try:
                    total_size = await loop.run_in_executor(None, os.path.getsize, file_path)
                    meta = await loop.run_in_executor(None, extract_metadata, file_path)
                except OSError:
                    total_size = 0
                    meta = None

                await self.context.controller.add_upload_task_async(
                    task_id, file_path, parent_id, total_size, is_folder=False, file_hash=None, file_details=meta
                )

                progress_callback(task_id, file_name, 0, total_size, 'queued', 0, is_folder=False)

                tasks_to_run.append(
                    self._upload_single_file(
                        client, task_id, task_id, 
                        file_path, parent_id,
                        progress_callback,
                        known_total_size=total_size,
                        known_file_details=meta,
                        is_new_task=True
                    )
                )
                
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            
            for item in upload_items:
                await self._finalize_thumbnails(client, item['task_id'])
        
        finally:
            # Release sync_manager only after the whole batch finishes to avoid mid-upload snapshots
            self.context.db.sync_manager.set_busy(False)
            self.context._trigger_folder_refresh([parent_id])

    def _scan_local_folder(self, local_folder_path: str) -> tuple[int, list[tuple[str, int]]]:
        """Walk local_folder_path (with Windows long-path support) and collect normalized files and sizes."""
        total = 0
        files_list = []
        scan_path = local_folder_path
        if os.name == 'nt' and not scan_path.startswith(r'\\?\\'):
            scan_path = r'\\?\\' + os.path.abspath(scan_path)
        for root, _dirs, files in os.walk(scan_path):
            for f in files:
                full_path = os.path.join(root, f)
                try:
                    f_size = os.path.getsize(full_path)
                    total += f_size
                    stored_path = self.context._normalize_path(full_path)
                    files_list.append((stored_path, f_size))
                except OSError as e:
                    logger.warning(f"[SizeCalc] Skipping file due to error: {full_path} - {e}")
        return total, files_list

    async def _ensure_remote_folder_hierarchy(
        self, parent_id: int, local_folder_path: str, base_folder_name: str, main_task_id: str
    ) -> dict[str, int]:
        """Create root and nested remote folders idempotently and return normalized local-path -> folder_id map."""
        loop = asyncio.get_running_loop()
        try:
            root_remote_id = await loop.run_in_executor(None, self.context.folder_repo.add_folder, parent_id, base_folder_name)
            await self.context.controller.record_created_artifact_async(main_task_id, 'folder', root_remote_id)
        except errors.ItemAlreadyExistsError:
            existing = await loop.run_in_executor(None, self.context.folder_repo.get_folder_contents, parent_id)
            found = next((f for f in existing['folders'] if f['name'] == base_folder_name), None)
            if not found:
                raise Exception("資料夾已存在但無法找到。")
            root_remote_id = found['id']

        local_folder_normalized = self.context._normalize_path(local_folder_path).lower()
        path_to_remote_id = {local_folder_normalized: root_remote_id}

        scan_path_structure = os.path.abspath(local_folder_path)
        if os.name == 'nt' and not scan_path_structure.startswith(r'\\?\\'):
            scan_path_structure = r'\\?\\' + scan_path_structure

        def _collect_local_subdirs(root_path: str) -> list[tuple[str, list[str]]]:
            return [(root, dirs) for root, dirs, _ in os.walk(root_path)]

        dir_walk_entries = await loop.run_in_executor(None, _collect_local_subdirs, scan_path_structure)
        for root, dirs in dir_walk_entries:
            current_remote_id = path_to_remote_id.get(self.context._normalize_path(root).lower())
            if current_remote_id is None:
                continue
            for d in dirs:
                local_dir_key = self.context._normalize_path(os.path.join(root, d)).lower()
                try:
                    new_folder_id = await loop.run_in_executor(None, self.context.folder_repo.add_folder, current_remote_id, d)
                    await self.context.controller.record_created_artifact_async(main_task_id, 'folder', new_folder_id)
                    path_to_remote_id[local_dir_key] = new_folder_id
                except errors.ItemAlreadyExistsError:
                    contents = await loop.run_in_executor(None, self.context.folder_repo.get_folder_contents, current_remote_id)
                    found = next((f for f in contents['folders'] if f['name'] == d), None)
                    if found:
                        path_to_remote_id[local_dir_key] = found['id']

        return path_to_remote_id

    async def _build_folder_upload_tasks(
        self,
        client,
        main_task_id: str,
        base_folder_name: str,
        parent_id: int,
        total_size: int,
        file_list: list[tuple[str, int]],
        path_to_remote_id: dict[str, int],
        progress_callback: Callable,
    ) -> tuple[list, int]:
        """Extract metadata, queue bulk child tasks in DB, and return coroutines plus verified total size."""
        loop = asyncio.get_running_loop()
        valid_files = []
        for file_path, f_size in file_list:
            file_dir = self.context._normalize_path(os.path.dirname(file_path)).lower()
            target_parent_id = path_to_remote_id.get(file_dir)
            if target_parent_id:
                valid_files.append((file_path, f_size, target_parent_id))
            else:
                logger.warning(f"[TaskGen] Skipping file {file_path} because parent folder ID not found.")

        metas = await asyncio.gather(
            *(loop.run_in_executor(None, extract_metadata, fp_path) for fp_path, _, _ in valid_files)
        ) if valid_files else []

        base_concurrency = getattr(self.context, "concurrency_limit", 3)
        effective_base = base_concurrency if isinstance(base_concurrency, int) and base_concurrency > 0 else 3
        small_file_sem = asyncio.Semaphore(max(effective_base * 4, 12))

        child_tasks_map = {}
        tasks_to_run = []
        real_total_size = 0

        for (file_path, f_size, target_parent_id), meta in zip(valid_files, metas):
            sub_task_id = str(uuid.uuid4())
            real_total_size += f_size
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
                    known_total_size=f_size,
                    known_file_details=meta,
                    is_new_task=True,
                    semaphore_override=small_file_sem if f_size <= fp.CHUNK_SIZE else None
                )
            )

        if real_total_size != total_size:
            await self.context.controller.update_task_total_size_async(main_task_id, real_total_size)
            total_size = real_total_size
            progress_callback(main_task_id, base_folder_name, 0, total_size, 'transferring', 0)

        await self.context.controller.add_child_tasks_bulk_async(main_task_id, child_tasks_map)
        return tasks_to_run, total_size

    async def upload_folder_recursive(self, parent_id: int, local_folder_path: str, main_task_id: str, progress_callback: Callable):
        """Recursively upload a local directory while preserving hierarchy and batching DB snapshots."""
        base_folder_name = os.path.basename(local_folder_path)
        logger.info(f"Starting folder upload: '{local_folder_path}' (Task: {main_task_id})")
        self.context.shared_state.active_tasks[main_task_id] = asyncio.current_task()
        self.context.db.sync_manager.set_busy(True)

        try:
            loop = asyncio.get_running_loop()
            total_size, file_list = await loop.run_in_executor(None, self._scan_local_folder, local_folder_path)

            await self.context.controller.add_upload_task_async(
                main_task_id, local_folder_path, parent_id, total_size, is_folder=True
            )
            progress_callback(main_task_id, base_folder_name, 0, total_size, 'queued', 0, is_folder=True)

            client = await utils.ensure_client_connected(self.context.shared_state)
            if not client:
                await self.context.controller.mark_failed_async(main_task_id, ErrorCode.CLIENT_NOT_CONNECTED)
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, error_code=ErrorCode.CLIENT_NOT_CONNECTED)
                return

            try:
                path_to_remote_id = await self._ensure_remote_folder_hierarchy(
                    parent_id, local_folder_path, base_folder_name, main_task_id
                )
                self.context._trigger_folder_refresh([parent_id, -1])

                tasks_to_run, total_size = await self._build_folder_upload_tasks(
                    client, main_task_id, base_folder_name, parent_id, total_size,
                    file_list, path_to_remote_id, progress_callback
                )
                progress_callback(main_task_id, base_folder_name, -1, total_size, 'transferring', 0)

                results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        logger.error(f"Sub-task failed: {res}")

                affected_folders = list(set(path_to_remote_id.values())) + [parent_id]
                self.context._trigger_folder_refresh(affected_folders)
                await self._finalize_thumbnails(client, main_task_id)

                task_info = await self.context.controller.get_task_async(main_task_id)
                if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
                    await self.context.controller.mark_sub_task_completed_async(main_task_id, main_task_id)
                    progress_callback(main_task_id, base_folder_name, 0, total_size, 'completed', 0)
                    self.context.watcher.add_watch(main_task_id, parent_id, 'remote')

            except asyncio.CancelledError:
                logger.info(f"Folder upload cancelled/paused: {main_task_id}")
                raise
            except Exception as e:
                logger.error(f"Folder upload failed: {e}", exc_info=True)
                await self.context.controller.mark_failed_async(main_task_id, ErrorCode.TASK_FAILED)
                progress_callback(main_task_id, base_folder_name, 0, 0, 'failed', 0, error_code=ErrorCode.TASK_FAILED)
        finally:
            if main_task_id in self.context.shared_state.active_tasks:
                del self.context.shared_state.active_tasks[main_task_id]
            self.context._active_sub_tasks.pop(main_task_id, None)
            # Release sync_manager only after the folder upload completes to avoid mid-upload DB snapshots
            self.context.db.sync_manager.set_busy(False)

    async def _restore_completed_sub_progress(
        self, main_task_id: str, sub_task_id: str, file_path: str, is_new_task: bool, resume_context: Optional[List]
    ) -> int:
        """Return previously uploaded byte count when resuming an already-completed sub-task."""
        last_uploaded = len(resume_context) * fp.CHUNK_SIZE if resume_context else 0
        if is_new_task or resume_context:
            return last_uploaded

        loop = asyncio.get_running_loop()
        try:
            task_info = await self.context.controller.get_task_async(main_task_id)
            sub_status = None
            if task_info:
                if task_info.get('is_folder'):
                    child = task_info.get('child_tasks', {}).get(sub_task_id)
                    if child:
                        sub_status = child.get('status')
                elif main_task_id == sub_task_id:
                    sub_status = task_info.get('status')
            if sub_status == 'completed':
                return await loop.run_in_executor(None, os.path.getsize, file_path)
        except OSError:
            return 0
        except Exception as e:
            logger.warning(f"Error initializing upload progress for {sub_task_id}: {e}")
        return last_uploaded

    async def _try_dedup_upload(
        self,
        client,
        main_task_id: str,
        sub_task_id: str,
        parent_id: int,
        file_name: str,
        existing_file_id: int,
        original_file_hash: str,
        total_size: int,
        split_files_info: List,
        last_uploaded: int,
        is_new_task: bool,
        progress_callback: Callable,
    ) -> None:
        """Handle sec-upload deduplication and idempotent resume reconciliation for existing file hashes."""
        loop = asyncio.get_running_loop()
        logger.debug(f"Sec-upload (Deduplication) triggered for {file_name}")
        try:
            def _dedup_add():
                fid, ui_id = self.context.file_repo.add_file(parent_id, file_name, time.time(), file_id=existing_file_id)
                self.context.controller.record_created_artifact(main_task_id, 'file', ui_id)
                return fid

            await loop.run_in_executor(None, _dedup_add)
            await self.context.controller.mark_sub_task_completed_async(main_task_id, sub_task_id)

            remaining = total_size - last_uploaded
            if remaining > 0:
                progress_callback(main_task_id, remaining, 0)

            if main_task_id == sub_task_id:
                await self.context.metadata_manager.sync_db_to_cloud(client, self.context.shared_state.group_id, self.context.shared_state.api_id)
                progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                self.context.watcher.add_watch(main_task_id, parent_id, 'remote')
            return
        except errors.ItemAlreadyExistsError:
            # Handle idempotent resume if this task had already persisted the same file in main_db
            is_reconcilable = False
            if not is_new_task:
                expected_parts = max(1, (total_size + fp.CHUNK_SIZE - 1) // fp.CHUNK_SIZE) if total_size > 0 else 0
                has_all_parts = (expected_parts == 0) or (len(split_files_info) >= expected_parts)
                artifacts = await loop.run_in_executor(None, self.context.controller.get_created_artifacts, main_task_id)
                has_file_artifact = isinstance(artifacts, list) and any(
                    isinstance(a, dict) and a.get('artifact_type') == 'file' for a in artifacts
                )
                has_file_fn = getattr(self.context.file_repo, 'has_file_in_folder', None)
                if (has_all_parts or has_file_artifact) and callable(has_file_fn):
                    try:
                        is_reconcilable = bool(
                            await loop.run_in_executor(
                                None, has_file_fn, parent_id, file_name, original_file_hash, True
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Failed to verify existing file during upload resume for {file_name}: {e}")

            if is_reconcilable:
                await self.context.controller.mark_sub_task_completed_async(main_task_id, sub_task_id)
                remaining = total_size - last_uploaded
                if remaining > 0:
                    progress_callback(main_task_id, remaining, 0)
                if main_task_id == sub_task_id:
                    progress_callback(main_task_id, file_name, total_size, total_size, 'completed', 0)
                    self.context.watcher.add_watch(main_task_id, parent_id, 'remote')
                return

            await self.context.controller.mark_sub_task_failed_async(main_task_id, sub_task_id, ErrorCode.ITEM_ALREADY_EXISTS)
            progress_callback(main_task_id, file_name, 0, total_size, 'failed', 0, error_code=ErrorCode.ITEM_ALREADY_EXISTS)

    async def _generate_upload_previews(self, file_path: str):
        """Prepare thumbnail bytes, inline preview bytes, or background PDF preview task by extension."""
        loop = asyncio.get_running_loop()
        ext = os.path.splitext(file_path)[1].lower()
        if ext in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.ico', '.tiff'}:
            thumb_bytes = await loop.run_in_executor(None, ImageThumbnailGenerator.generate, file_path)
            preview_bytes = await loop.run_in_executor(None, ImagePreviewer().generate, file_path)
            return thumb_bytes, preview_bytes, None
        if ext in {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm', '.flv'}:
            thumb_bytes = await loop.run_in_executor(None, VideoThumbnailGenerator.generate, file_path)
            return thumb_bytes, None, None
        if ext == '.pdf':
            doc_preview_task = asyncio.create_task(UploadStrategy._generate_pdf_preview_async(file_path, ext))
            return None, None, doc_preview_task
        return None, None, None

    async def _upload_preview_payload(
        self, client, sub_task_id: str, payload_bytes: bytes, hidden_progress_fn: Callable, label: str
    ) -> tuple[Optional[int], Optional[str]]:
        """Hash, encrypt-upload, and persist a preview asset message ID for an upload task."""
        loop = asyncio.get_running_loop()
        preview_hash = await loop.run_in_executor(None, crypto_handler.hash_bytes, payload_bytes)
        upload_group_index, target_upload_group_id = utils.get_upload_target(
            self.context.shared_state, self.context.shared_state.group_id
        )
        logger.debug(f"Upload {label}: target_group={target_upload_group_id} (idx={upload_group_index})")
        preview_upload_info = await telegram_comms.upload_data_as_file(
            client, target_upload_group_id, payload_bytes, preview_hash,
            progress_callback=hidden_progress_fn
        )
        preview_msg_id = None
        if preview_upload_info:
            raw_preview_msg_id = preview_upload_info[0][1]
            preview_msg_id = utils.encode_global_msg_id(upload_group_index, raw_preview_msg_id)
            logger.debug(f"Upload {label}: encoded preview_msg_id -> global_id={preview_msg_id}")
            self.context.shared_state.current_group_msg_count += len(preview_upload_info)
            await telegram_comms.check_and_rotate_group_if_needed(self.context.shared_state)
            await self.context.controller.set_preview_msg_id_async(sub_task_id, preview_msg_id)
        return preview_msg_id, preview_hash

    async def _resolve_file_details(
        self, main_task_id: str, sub_task_id: str, known_file_details: Optional[Dict[str, Any]], is_new_task: bool
    ) -> Optional[Dict[str, Any]]:
        """Return known metadata or look up persisted task metadata from QueueRepository."""
        if known_file_details is not None or is_new_task:
            return known_file_details
        task_data = await self.context.controller.get_task_async(main_task_id)
        if not task_data:
            return None
        if task_data.get('is_folder'):
            child = task_data.get('child_tasks', {}).get(sub_task_id)
            return child.get('file_details') if child else None
        if main_task_id == sub_task_id:
            return task_data.get('file_details')
        return None

    async def _upload_single_file(self, client, main_task_id: str, sub_task_id: str,
                                  file_path: str, parent_id: int, 
                                  progress_callback: Callable,
                                  resume_context: List = None, pre_calculated_hash: str = None,
                                  known_total_size: int = None,
                                  known_file_details: Dict[str, Any] = None,
                                  is_new_task: bool = False,
                                  semaphore_override: asyncio.Semaphore = None):
        """Upload one file (with deduplication, preview generation, chunk dispatch, and DB persistence)."""
        file_name = os.path.basename(file_path)
        pending_chunk_writes: List[asyncio.Future] = []
        last_uploaded = len(resume_context) * fp.CHUNK_SIZE if resume_context else 0
        last_update_time = time.time()

        def chunk_cb(part_num, msg_id, part_hash):
            running_loop = asyncio.get_running_loop()
            fut = running_loop.run_in_executor(
                None, self.context.controller.update_progress, main_task_id, sub_task_id, part_num, [msg_id, part_hash]
            )
            pending_chunk_writes.append(fut)

        def ui_cb(current, total):
            nonlocal last_uploaded, last_update_time
            if current <= last_uploaded:
                return
            delta = current - last_uploaded
            now = time.time()
            time_diff = now - last_update_time
            last_uploaded = current
            last_update_time = now
            speed = delta / time_diff if time_diff > 0 else 0
            _t = asyncio.create_task(self.context.controller.update_transferred_bytes(delta))
            self.context.shared_state.background_tasks.add(_t)
            _t.add_done_callback(self.context.shared_state.background_tasks.discard)
            progress_callback(main_task_id, delta, speed)

        def _make_hidden_progress():
            last_preview_uploaded = 0
            def _hidden_progress(current, total):
                nonlocal last_preview_uploaded
                if current <= last_preview_uploaded:
                    return
                delta = current - last_preview_uploaded
                last_preview_uploaded = current
                _t = asyncio.create_task(self.context.controller.update_transferred_bytes(delta))
                self.context.shared_state.background_tasks.add(_t)
                _t.add_done_callback(self.context.shared_state.background_tasks.discard)
            return _hidden_progress

        curr_task = asyncio.current_task()
        if curr_task is not None:
            curr_task._tdrive_main_task_id = main_task_id
        loop = asyncio.get_running_loop()
        active_sem = semaphore_override or self.context._semaphore
        async with active_sem:
            main_status = await loop.run_in_executor(None, self.context.controller.queue_repo.get_main_task_status, main_task_id)
            if main_status in ['paused', 'cancelled', 'failed']:
                return

            last_uploaded = await self._restore_completed_sub_progress(
                main_task_id, sub_task_id, file_path, is_new_task, resume_context
            )

            try:
                self.context.shared_state.active_tasks[sub_task_id] = asyncio.current_task()
                self.context._active_sub_tasks[main_task_id].add(sub_task_id)

                if not await loop.run_in_executor(None, os.path.exists, file_path):
                    raise FileNotFoundError(f"File not found: {file_path}")

                await loop.run_in_executor(None, self.context.controller.queue_repo.update_sub_task_status, sub_task_id, "transferring")
                if main_task_id == sub_task_id:
                    self.context._trigger_folder_refresh([parent_id])

                total_size = known_total_size if (known_total_size is not None and known_total_size > 0) else await loop.run_in_executor(None, os.path.getsize, file_path)
                original_file_hash = pre_calculated_hash
                split_files_info = resume_context or []

                if not original_file_hash:
                    original_file_hash = await loop.run_in_executor(None, crypto_handler.hash_data, file_path)
                    await self.context.controller.set_file_hash_async(sub_task_id, original_file_hash)

                existing_file_id = await loop.run_in_executor(None, self.context.file_repo.find_file_by_hash, original_file_hash)
                if existing_file_id:
                    await self._try_dedup_upload(
                        client, main_task_id, sub_task_id, parent_id, file_name,
                        existing_file_id, original_file_hash, total_size, split_files_info,
                        last_uploaded, is_new_task, progress_callback
                    )
                    return None

                progress_callback(main_task_id, file_name, -1, -1, 'transferring', 0, is_folder=False)
                thumb_bytes, preview_bytes, doc_preview_task = await self._generate_upload_previews(file_path)

                preview_msg_id, preview_hash = None, None
                if preview_bytes:
                    preview_msg_id, preview_hash = await self._upload_preview_payload(
                        client, sub_task_id, preview_bytes, _make_hidden_progress(), "preview"
                    )

                from core_app.application.transfer.dispatcher import TransferDispatcher

                skip_chunk_db_cb = is_new_task and main_task_id != sub_task_id and total_size <= fp.CHUNK_SIZE
                split_files_info = await TransferDispatcher.dispatch_upload(
                    self.context.shared_state, self.context.shared_state.group_id, file_path, original_file_hash,
                    progress_callback=ui_cb,
                    resume_context=split_files_info,
                    chunk_callback=None if skip_chunk_db_cb else chunk_cb
                )
                if pending_chunk_writes:
                    await asyncio.gather(*pending_chunk_writes, return_exceptions=True)
                    pending_chunk_writes.clear()

                if doc_preview_task:
                    preview_result = None
                    try:
                        preview_result = await doc_preview_task
                    except Exception as e:
                        logger.warning(f"Document preview failed for {file_path}: {e}")
                    if preview_result:
                        preview_pdf_bytes, doc_thumb_bytes, _source_hash = preview_result
                        thumb_bytes = doc_thumb_bytes
                        if preview_pdf_bytes:
                            preview_msg_id, preview_hash = await self._upload_preview_payload(
                                client, sub_task_id, preview_pdf_bytes, _make_hidden_progress(), "doc_preview"
                            )

                def _pre_insert_file():
                    try:
                        return self.context.file_repo.add_file(
                            parent_id, file_name, time.time(),
                            file_hash=original_file_hash, size=total_size,
                            preview_msg_id=preview_msg_id, preview_hash=preview_hash,
                            map_msg_id=None, has_thumb=bool(thumb_bytes)
                        )
                    except (sqlite3.IntegrityError, errors.ItemAlreadyExistsError):
                        existing_id = self.context.file_repo.find_file_by_hash(original_file_hash)
                        if existing_id:
                            return self.context.file_repo.add_file(parent_id, file_name, time.time(), file_id=existing_id)
                        raise

                fid, ui_id = await loop.run_in_executor(None, _pre_insert_file)
                await self.context.controller.record_created_artifact_async(main_task_id, 'file', ui_id)

                if thumb_bytes:
                    await loop.run_in_executor(None, self.context.controller.queue_repo.add_task_thumbnail, main_task_id, parent_id, fid, thumb_bytes)

                await self.context.controller.mark_sub_task_completed_async(main_task_id, sub_task_id)
                file_details = await self._resolve_file_details(main_task_id, sub_task_id, known_file_details, is_new_task)

                await self.context.metadata_manager.process_file_transfer(
                    client, self.context.shared_state.api_id,
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
                await self.context.controller.mark_sub_task_failed_async(main_task_id, sub_task_id, ErrorCode.TASK_FAILED)
                progress_callback(main_task_id, file_name, 0, 0, 'failed', 0, error_code=ErrorCode.TASK_FAILED)
            finally:
                if sub_task_id in self.context.shared_state.active_tasks:
                    del self.context.shared_state.active_tasks[sub_task_id]
                self.context._active_sub_tasks[main_task_id].discard(sub_task_id)
                if main_task_id == sub_task_id:
                    self.context._active_sub_tasks.pop(main_task_id, None)

    async def _finalize_thumbnails(self, client, main_task_id: str):
        try:
            loop = asyncio.get_running_loop()
            logger.debug(f"Processing thumbnails for task {main_task_id}...")
            thumbs = await loop.run_in_executor(None, self.context.controller.queue_repo.get_task_thumbnails, main_task_id)
            
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
                        self.context.thumb_manager
                    )
                
                await loop.run_in_executor(None, self.context.controller.queue_repo.delete_task_thumbnails, main_task_id)
                
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
            logger.debug(f"Cleanup: Starting artifact cleanup for task {task_id}")
            if task_id:
                artifacts = task_info.get('created_artifacts', [])
                
                def _cleanup_db_items():
                    results = []
                    for artifact in artifacts:
                        try:
                            if artifact['artifact_type'] == 'file':
                                res = self.context.trash_repo.remove_file(artifact['db_id'])
                                if res: results.append(res)
                            elif artifact['artifact_type'] == 'folder':
                                res_list = self.context.trash_repo.remove_folder(artifact['db_id'])
                                results.extend(res_list)
                        except Exception as e:
                            logger.warning(f"Cleanup: failed to remove DB artifact {artifact.get('artifact_type', '?')} {artifact.get('db_id', '?')}: {e}")
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
                msgs_by_group = defaultdict(set)
                for global_msg_id in message_ids_to_delete:
                    g_idx, raw_id = utils.decode_global_msg_id(global_msg_id)
                    target_gid = self.context.shared_state.get_telegram_group_id(g_idx)
                    msgs_by_group[target_gid].add(raw_id)

                logger.debug(f"Cleanup: decoded {len(message_ids_to_delete)} global IDs into {len(msgs_by_group)} groups")
                for target_gid, raw_ids in msgs_by_group.items():
                    raw_list = list(raw_ids)
                    batch_size = 100
                    for i in range(0, len(raw_list), batch_size):
                        batch = raw_list[i:i + batch_size]
                        try:
                            await client.delete_messages(target_gid, batch)
                        except Exception as del_err:
                            logger.error(f"Failed to delete messages in cleanup: {del_err}")
        finally:
            self.context.db.sync_manager.set_busy(False) # Release and trigger ONE sync
                    
        logger.debug("Cleanup completed successfully.")
        
        refresh_ids = list(affected_parents) + [-1]
        
        # Check parent_id (upload) or db_id (download)
        main_remote_id = task_info.get('parent_id') or task_info.get('db_id')
        if main_remote_id:
            refresh_ids.append(main_remote_id)
        
        self.context._trigger_folder_refresh(refresh_ids)

    @staticmethod
    async def _generate_pdf_preview_async(file_path: str, ext: str):
        from core_app.application.preview.document.viewer import generate_pdf_preview
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, generate_pdf_preview, file_path, ext)
