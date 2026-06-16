from core_app.core.errors import ErrorCode
import os
import uuid
import time
import asyncio
import logging
from typing import List, Dict, Any, Callable

from core_app.application.transfer.strategies.base_strategy import TransferStrategy
from core_app.core import utils
from core_app.infrastructure.local_fs import file_processor as fp

logger = logging.getLogger(__name__)

class DownloadStrategy(TransferStrategy):

    async def start(self, task_type: str, *args, **kwargs):
        # We only have one entry point for download in original service
        return await self.download_items(*args, **kwargs)

    async def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable):
        client = await utils.ensure_client_connected(self.context.shared_state)
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
                file_details_basic = await loop.run_in_executor(None, self.context.file_repo.get_file_details, db_id)
                if not file_details_basic: 
                    progress_callback(task_id, item['name'], 0, 0, 'failed', 0, error_code=ErrorCode.TASK_FAILED)
                    continue

                # Fetch Chunks from Metadata Manager (Cloud Map)
                file_id = file_details_basic['file_id']
                chunks = await self.context.metadata_manager.get_file_chunks(client, self.context.shared_state.group_id, self.context.shared_state.api_id, file_id)
                
                # Construct full file details
                file_details = {
                    "name": file_details_basic['name'],
                    "size": file_details_basic['size'],
                    "hash": file_details_basic['hash'],
                    "chunks": [{"part_num": c[0], "message_id": c[1], "part_hash": c[2]} for c in chunks]
                }

                save_path = await loop.run_in_executor(None, fp.get_unique_filepath, destination_dir, file_details['name'])

                self.context.controller.add_download_task(
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
        self.context.shared_state.active_tasks[main_task_id] = asyncio.current_task()

        try:
            loop = asyncio.get_running_loop()
            folder_db_id = folder_item['db_id']
            
            contents = await loop.run_in_executor(None, self.context.folder_repo.get_folder_contents_recursive, folder_db_id)
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

            self.context.controller.add_download_task(
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

            self.context.controller.add_child_tasks_bulk(main_task_id, child_tasks_map)

            progress_callback(main_task_id, root_folder_name, -1, total_size, 'transferring', 0)
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            
            task_info = self.context.controller.get_task(main_task_id)
            if task_info and task_info['status'] not in ['cancelled', 'failed', 'paused']:
                self.context.controller.mark_sub_task_completed(main_task_id, main_task_id)
                progress_callback(main_task_id, root_folder_name, 0, total_size, 'completed', 0)
                self.context.watcher.add_watch(main_task_id, local_root_path, 'local')

        except Exception as e:
            logger.error(f"Download folder failed: {e}", exc_info=True)
            self.context.controller.mark_failed(main_task_id, ErrorCode.TASK_FAILED)
        finally:
            if main_task_id in self.context.shared_state.active_tasks:
                del self.context.shared_state.active_tasks[main_task_id]
            self.context._active_sub_tasks.pop(main_task_id, None)

    async def _download_single_item(self, client, main_task_id: str, sub_task_id: str, 
                                    save_path: str, file_details: Dict,
                                    progress_callback: Callable,
                                    resume_parts: set = None):
        def chunk_cb(part_num):
            self.context.controller.update_progress(main_task_id, sub_task_id, part_num)
        
        last_downloaded = 0
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
            if current <= last_downloaded:
                return

            delta = current - last_downloaded
            now = time.time()
            time_diff = now - last_update_time

            last_downloaded = current
            last_update_time = now
            speed = delta / time_diff if time_diff > 0 else 0
            asyncio.create_task(self.context.controller.update_transferred_bytes(delta))
            progress_callback(main_task_id, delta, speed)

        async with self.context._semaphore:
            main_status = self.context.controller.queue_repo.get_main_task_status(main_task_id)
            if main_status in ['paused', 'cancelled', 'failed']:
                return

            try:
                self.context.shared_state.active_tasks[sub_task_id] = asyncio.current_task()
                self.context._active_sub_tasks[main_task_id].add(sub_task_id)

                # JIT Chunk Fetching
                if not file_details.get('chunks') and file_details.get('file_id'):
                    chunks = await self.context.metadata_manager.get_file_chunks(
                        client, self.context.shared_state.group_id, self.context.shared_state.api_id, file_details['file_id']
                    )
                    file_details['chunks'] = [{"part_num": c[0], "message_id": c[1], "part_hash": c[2]} for c in chunks]

                if not file_details.get('chunks'):
                    raise Exception("Unable to retrieve file chunks from cloud.")

                progress_callback(main_task_id, file_details['name'], -1, -1, 'transferring', 0)

                from core_app.application.transfer.dispatcher import TransferDispatcher
                
                await TransferDispatcher.dispatch_download(
                    self.context.shared_state, self.context.shared_state.group_id, file_details, save_path,
                    progress_callback=ui_cb,
                    completed_parts=resume_parts,
                    chunk_callback=chunk_cb
                )
                
                self.context.controller.mark_sub_task_completed(main_task_id, sub_task_id)

                if main_task_id == sub_task_id:
                    progress_callback(main_task_id, file_details['name'], file_details['size'], file_details['size'], 'completed', 0)
                    self.context.watcher.add_watch(main_task_id, save_path, 'local')

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Download failed {save_path}: {e}")
                self.context.controller.mark_sub_task_failed(main_task_id, sub_task_id, ErrorCode.TASK_FAILED)
            finally:
                if sub_task_id in self.context.shared_state.active_tasks:
                    del self.context.shared_state.active_tasks[sub_task_id]
                self.context._active_sub_tasks[main_task_id].discard(sub_task_id)
                # Clean up tracking set for standalone tasks
                if main_task_id == sub_task_id:
                    self.context._active_sub_tasks.pop(main_task_id, None)

    async def cleanup(self, task_info: Dict[str, Any]):
        paths_to_delete = []
        is_folder = task_info.get('is_folder')
        
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
                except OSError:
                    pass
