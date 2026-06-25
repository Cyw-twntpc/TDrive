import logging
import asyncio
import base64
import time
import os
from telethon import errors as telethon_errors
from typing import TYPE_CHECKING, List, Dict, Any, Callable

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from ..media.gallery_manager import GalleryManager

from core_app.core import utils
from core_app.core import errors
from core_app.infrastructure.telegram import telegram_comms
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository

from core_app.application.file_system.defrag_worker import DefragWorker

logger = logging.getLogger(__name__)

class FileService:
    def __init__(self, shared_state: 'SharedState', gallery_manager: 'GalleryManager'):
        self.shared_state = shared_state
        self.gallery_manager = gallery_manager
        self.defrag_worker = DefragWorker(shared_state, self)
        self.defrag_worker.start()

    # --- Gallery Integration ---

    async def get_thumbnails(self, folder_id: int, return_file_id_keys: bool = False) -> Dict[str, Any]:
        """Returns base64 thumbnails for the folder. Downloads DB if not in memory."""
        try:
            # 1. Get all file_ids in this folder and their map_ids
            def _get_folder_files_info():
                db = DatabaseConnection()
                conn = db._get_conn()
                cur = conn.cursor()
                cur.execute('''
                    SELECT m.id as map_id, m.file_id, f.thumb_src_folder_id, f.has_thumb
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id 
                    WHERE m.folder_id = ?
                ''', (folder_id,))
                
                # Also include files from Recycle Bin or global map logic if necessary
                # But to correctly handle Trash thumbnails, we should use the item mapping.
                # Actually, Trash is just a folder.
                return cur.fetchall()

            files_info = await asyncio.to_thread(_get_folder_files_info)
            if not files_info:
                return {"success": True, "thumbnails": {}}

            file_ids = [row['file_id'] for row in files_info]
            id_map = {row['file_id']: row['map_id'] for row in files_info}
            
            # 2. Local-First Cache Retrieval
            thumbs = self.gallery_manager.get_thumbnails(file_ids)
            
            # 3. Check for missing thumbnails and download their packages
            # Absolutely strictly ONLY consider files that are explicitly marked with has_thumb=1
            expecting_thumb_file_ids = {row['file_id'] for row in files_info if row['has_thumb'] == 1}
            missing_file_ids = expecting_thumb_file_ids - {int(k) for k in thumbs.keys()}
            
            if missing_file_ids:
                # Find which source folders we need to download from
                # If thumb_src_folder_id is NULL, the thumbnail is in the CURRENT folder's package
                needed_folders = {row['thumb_src_folder_id'] if row['thumb_src_folder_id'] else folder_id for row in files_info if row['file_id'] in missing_file_ids}
                
                if needed_folders:
                    def _get_package_cloud_info(f_ids):
                        db = DatabaseConnection()
                        conn = db._get_conn()
                        cur = conn.cursor()
                        placeholders = ",".join("?" for _ in f_ids)
                        cur.execute(f"SELECT id, thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id IN ({placeholders})", list(f_ids))
                        return cur.fetchall()
                    
                    cloud_packages = await asyncio.to_thread(_get_package_cloud_info, needed_folders)
                    
                    client = await utils.ensure_client_connected(self.shared_state)
                    if client:
                        for pkg in cloud_packages:
                            if pkg['thumbs_db_msg_id'] and pkg['thumbs_db_msg_id'] != 0:
                                logger.debug(f"Downloading thumbs package for folder {pkg['id']} (msg_id: {pkg['thumbs_db_msg_id']})...")
                                db_bytes = await telegram_comms.download_data_as_bytes(
                                    client, self.shared_state.group_id, [pkg['thumbs_db_msg_id']], pkg['thumbs_db_hash']
                                )
                                if db_bytes:
                                    self.gallery_manager.import_package_bytes(db_bytes)
                        
                        # Re-fetch from local cache after importing missing packages
                        thumbs = self.gallery_manager.get_thumbnails(file_ids)

            # 4. Convert Content IDs (file_id) to Map IDs (file_folder_map.id) for frontend
            if return_file_id_keys:
                mapped_thumbs = thumbs
            else:
                mapped_thumbs = {}
                for content_id_str, b64_data in thumbs.items():
                    try:
                        content_id = int(content_id_str)
                        if content_id in id_map:
                            map_id = id_map[content_id]
                            mapped_thumbs[str(map_id)] = b64_data
                    except ValueError:
                        logger.debug("Skipping invalid content_id: %s", content_id_str)
            
            thumbs = mapped_thumbs

            logger.debug(f"FileService returning {len(thumbs)} thumbnails for {folder_id} (Mapped IDs)")
            return {"success": True, "thumbnails": thumbs}
        except Exception as e:
            logger.error(f"Error fetching thumbnails: {e}", exc_info=True)
            return {"success": False, "thumbnails": {}}

    async def get_preview(self, file_id: int) -> Dict[str, Any]:
        try:
            # 1. Check Cache
            preview_b64 = self.gallery_manager.get_cached_preview(file_id)
            if preview_b64:
                return {"success": True, "preview": preview_b64}

            # 2. If not in cache, check DB for preview info
            def _get_file_preview_info():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                TrashRepository(db)
                conn = db._get_conn()
                
                cur = conn.cursor()
                query = """
                    SELECT f.preview_msg_id, f.preview_hash, f.id as content_id, f.hash as file_hash, f.size
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """
                cur.execute(query, (file_id,))
                return cur.fetchone()

            info = await asyncio.to_thread(_get_file_preview_info)
            
            if not info:
                logger.error("File not found")
                return {"success": False}

            # 3. Download
            client = await utils.ensure_client_connected(self.shared_state)
            if not client:
                logger.error("Client not connected")
                return {"success": False}

            preview_bytes = None
            
            if info['preview_msg_id']:
                logger.debug(f"Downloading preview for file map {file_id} (content {info['content_id']})...")
                preview_bytes = await telegram_comms.download_data_as_bytes(
                    client, self.shared_state.group_id, [info['preview_msg_id']], info['preview_hash']
                )
            else:
                # Fallback: Download first chunk of original file if no preview exists
                logger.debug(f"No preview found for {file_id}, attempting fallback to original file...")
                try:
                    chunks = await self.shared_state.metadata_manager.get_file_chunks(
                        client, self.shared_state.group_id, self.shared_state.api_id, info['content_id']
                    )
                    if chunks and len(chunks) > 0:
                        first_chunk = chunks[0]
                        chunk_msg_id = first_chunk[1]
                        preview_bytes = await telegram_comms.download_data_as_bytes(
                            client, self.shared_state.group_id, [chunk_msg_id], info['file_hash']
                        )
                except Exception as fb_e:
                    logger.warning(f"Fallback preview download failed: {fb_e}")

            if preview_bytes:
                # 4. Cache and Return
                # We cache by Map ID or Content ID? 
                # GalleryManager cache uses `file_id`. 
                # To be consistent with frontend requests, let's use the Map ID (which is unique per item in folder).
                # However, if multiple maps point to same file, we duplicate cache. 
                # Ideally cache by content_id, but frontend sends map_id.
                # Let's stick to Map ID for now as the key for simplicity in 1:1 mapping with UI.
                self.gallery_manager.cache_preview(file_id, preview_bytes)
                b64_str = base64.b64encode(preview_bytes).decode('utf-8')
                return {"success": True, "preview": b64_str}
            
            logger.error("Download failed")
            return {"success": False}

        except Exception as e:
            logger.error(f"Error fetching preview: {e}", exc_info=True)
            return {"success": False}

    async def get_file_extended_details(self, file_id: int) -> Dict[str, Any]:
        try:
            def _get_db_info():
                db = DatabaseConnection()
                conn = db._get_conn()
                cur = conn.cursor()
                cur.execute("""
                    SELECT f.map_msg_id, f.id as real_file_id 
                    FROM file_folder_map m 
                    JOIN files f ON m.file_id = f.id 
                    WHERE m.id = ?
                """, (file_id,))
                return cur.fetchone()
                
            row = await asyncio.to_thread(_get_db_info)
            if not row or not row['map_msg_id']:
                return {"success": False}
            
            real_file_id = row['real_file_id']
            map_msg_id = row['map_msg_id']
            
            client = await utils.ensure_client_connected(self.shared_state)
            if not client:
                return {"success": False}
            
            map_data = await self.shared_state.metadata_manager.fetch_map_file(
                client, self.shared_state.group_id, 
                self.shared_state.api_id, map_msg_id
            )
            
            if str(real_file_id) in map_data:
                file_info = map_data[str(real_file_id)]
                if isinstance(file_info, dict) and 'm' in file_info:
                    return {"success": True, "metadata": file_info['m']}
            
            return {"success": False}
        except Exception as e:
            logger.error(f"Error fetching extended details for file {file_id}: {e}", exc_info=True)
            return {"success": False}

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        logger.debug(f"Fetching contents for folder_id: {folder_id} from database.")
        try:
            def _sync_db_op():
                db = DatabaseConnection()
                FileRepository(db)
                folder_repo = FolderRepository(db)
                TrashRepository(db)
                contents = folder_repo.get_folder_contents(folder_id)
                
                # Integrate active uploads from TransferDB
                from core_app.infrastructure.database.transfer_db.transfer_database import TransferDatabaseConnection
                from core_app.infrastructure.database.transfer_db.queue_repository import QueueRepository
                transfer_db = TransferDatabaseConnection()
                tdb = QueueRepository(transfer_db)
                active_uploads = tdb.get_active_uploads(folder_id)
                
                if active_uploads:
                    for task in active_uploads:
                        file_name = os.path.basename(task['local_path'])
                        # Ensure we don't add duplicates if DB already has the file 
                        # (though during 'transferring' stage, DB usually doesn't have it yet)
                        if any(f['name'] == file_name for f in contents['files']):
                            continue
                            
                        contents['files'].append({
                            "id": task['task_id'], # Use task_id as temporary id
                            "name": file_name,
                            "raw_size": task['total_size'],
                            "size": folder_repo._format_size(task['total_size']),
                            "modif_date": folder_repo._format_timestamp(time.time()),
                            "isUploading": True,
                            "type": "file"
                        })
                return contents
            
            return await asyncio.to_thread(_sync_db_op)
        except errors.PathNotFoundError:
            logger.error("資料夾不存在。")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except Exception as e:
            logger.error(f"Error getting folder contents for id {folder_id}: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        logger.debug(f"Recursively fetching contents for folder_id: {folder_id}.")
        try:
            def _sync_db_op():
                db = DatabaseConnection()
                FileRepository(db)
                folder_repo = FolderRepository(db)
                TrashRepository(db)
                return folder_repo.get_folder_contents_recursive(folder_id)
            
            return await asyncio.to_thread(_sync_db_op)
        except Exception as e:
            logger.error(f"Error recursively fetching folder contents for id {folder_id}: {e}", exc_info=True)
            return {"folder_name": "Error", "items": [], "success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter: Callable, request_id: str):
        logger.debug(f"Starting streaming search from base_id: {base_folder_id} for term: '{search_term}'")

        def progress_callback(batch_results: dict):
            try:
                payload = {'request_id': request_id, 'type': 'batch', 'data': batch_results}
                result_signal_emitter(payload)
            except Exception as e:
                logger.error(f"Error emitting search results batch: {e}", exc_info=True)

        def db_search_sync():
            try:
                thread_local_db = DatabaseConnection()
                file_repo = FileRepository(thread_local_db)
                file_repo.search_db_items(search_term, base_folder_id, progress_callback)
                
                done_payload = {'request_id': request_id, 'type': 'done'}
                result_signal_emitter(done_payload)
                logger.debug(f"Streaming search completed for request_id: {request_id}.")
            except Exception as e:
                logger.error(f"Critical error in background search thread: {e}", exc_info=True)
                error_payload = {'request_id': request_id, 'type': 'error', 'data': {'error_code': errors.ErrorCode.INTERNAL_ERROR}}
                result_signal_emitter(error_payload)

        try:
            await asyncio.to_thread(db_search_sync)
        except Exception as e:
            logger.error(f"Failed to start background search thread: {e}", exc_info=True)
            error_payload = {'request_id': request_id, 'type': 'error', 'data': {'error_code': errors.ErrorCode.ASYNC_CALL_FAILED}}
            result_signal_emitter(error_payload)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}
        
        try:
            def _sync_create():
                db = DatabaseConnection()
                FileRepository(db)
                folder_repo = FolderRepository(db)
                TrashRepository(db)
                folder_repo.add_folder(parent_id, folder_name)
            
            await asyncio.to_thread(_sync_create)
            
            logger.info(f"Successfully created folder '{folder_name}' under parent_id {parent_id}.")
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to create folder '{folder_name}': {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except Exception as e:
            logger.error(f"Unknown error creating folder '{folder_name}'.", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}
            
        try:
            def _sync_rename():
                db = DatabaseConnection()
                file_repo = FileRepository(db)
                folder_repo = FolderRepository(db)
                TrashRepository(db)
                if item_type == 'folder':
                    folder_repo.rename_folder(item_id, new_name)
                else:
                    file_repo.rename_file(item_id, new_name)
            
            await asyncio.to_thread(_sync_rename)
            
            logger.info(f"Successfully renamed {item_type} with id {item_id} to '{new_name}'.")
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to rename item {item_id}: {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except Exception as e:
            logger.error(f"Unknown error renaming item {item_id}.", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_soft_delete():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                
                old_parent_ids = set()
                conn = db._get_conn()
                cursor = conn.cursor()
                
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    if item_type == 'folder':
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['parent_id'] is not None:
                            old_parent_ids.add(row['parent_id'])
                    else:
                        cursor.execute("SELECT folder_id FROM file_folder_map WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['folder_id'] is not None:
                            old_parent_ids.add(row['folder_id'])
                    
                    trash_repo.soft_delete_item(item_id, item_type)
                    
                cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
                rb_row = cursor.fetchone()
                rb_id = rb_row['id'] if rb_row else None
                
                return list(old_parent_ids), rb_id
            
            old_parent_ids, recycle_bin_id = await asyncio.to_thread(_sync_soft_delete)
            
            folders_to_evaluate = old_parent_ids
            if recycle_bin_id:
                folders_to_evaluate.append(recycle_bin_id)
            self.defrag_worker.evaluate_folders(folders_to_evaluate)
            
            logger.info(f"Successfully moved {len(items)} items to Recycle Bin.")
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}
        
        except errors.PathNotFoundError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except errors.InvalidOperationError as e:
            logger.warning(f"Invalid operation during soft delete: {e}")
            return {"success": False, "error_code": errors.ErrorCode.INVALID_OPERATION}
        except Exception as e:
            logger.error(f"Error soft deleting items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def restore_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_restore():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                
                new_parent_ids = set()
                conn = db._get_conn()
                cursor = conn.cursor()
                
                restored_names = []
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    name = trash_repo.restore_item(item_id, item_type)
                    restored_names.append(name)
                    
                    if item_type == 'folder':
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['parent_id'] is not None:
                            new_parent_ids.add(row['parent_id'])
                    else:
                        cursor.execute("SELECT folder_id FROM file_folder_map WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['folder_id'] is not None:
                            new_parent_ids.add(row['folder_id'])
                            
                cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
                rb_row = cursor.fetchone()
                rb_id = rb_row['id'] if rb_row else None
                            
                return restored_names, list(new_parent_ids), rb_id
            
            restored_names, new_parent_ids, recycle_bin_id = await asyncio.to_thread(_sync_restore)
            
            folders_to_evaluate = new_parent_ids
            if recycle_bin_id:
                folders_to_evaluate.append(recycle_bin_id)
            self.defrag_worker.evaluate_folders(folders_to_evaluate)
            
            logger.info(f"Successfully restored {len(items)} items.")
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}

        except errors.PathNotFoundError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except Exception as e:
            logger.error(f"Error restoring items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def delete_items_permanently(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        if not self.shared_state.metadata_manager:
             logger.error("MetadataManager 未初始化。")
             return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

        try:
            # 1. Perform DB Deletion and Collect Metadata
            def _sync_delete():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                deletion_results = []
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    if item_type == 'folder':
                        res_list = trash_repo.remove_folder(item_id)
                        deletion_results.extend(res_list)
                    else:
                        res = trash_repo.remove_file(item_id)
                        if res: deletion_results.append(res)
                    logger.info(f"Marked {item_type} id {item_id} for permanent deletion.")
                return deletion_results

            deletion_results = await asyncio.to_thread(_sync_delete)
            
            # 2. Handle Cloud Map Cleanup via MetadataManager
            # This handles re-uploading maps or deleting empty maps
            await self.shared_state.metadata_manager.handle_deletion(
                client, self.shared_state.group_id, self.shared_state.api_id, deletion_results
            )
            
            # 3. Collect extra message IDs to delete (e.g. previews, thumbs.db)
            extra_msg_ids = []
            for res in deletion_results:
                if res.get('msg_ids_to_delete'):
                    extra_msg_ids.extend(res['msg_ids_to_delete'])

            # 4. Delete Extra Messages
            if extra_msg_ids:
                unique_ids = list(set(extra_msg_ids))
                logger.info(f"Preparing to delete {len(unique_ids)} extra chunks (previews/thumbs) from Telegram.")
                
                for i in range(0, len(unique_ids), 100):
                    chunk = unique_ids[i:i + 100]
                    await client.delete_messages(self.shared_state.group_id, chunk)
            
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}

        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to delete item: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"Delete operation hit a flood wait for {e.seconds} seconds.")
            return {"success": False, "error_code": errors.ErrorCode.FLOOD_WAIT_ERROR}
        except Exception as e:
            logger.error(f"An unknown error occurred while deleting {len(items)} items", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def empty_trash(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        if not self.shared_state.metadata_manager:
             logger.error("MetadataManager 未初始化。")
             return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

        try:
            def _sync_empty():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                return trash_repo.empty_trash()

            deletion_results = await asyncio.to_thread(_sync_empty)
            
            # Handle Cloud Map Cleanup
            await self.shared_state.metadata_manager.handle_deletion(
                client, self.shared_state.group_id, self.shared_state.api_id, deletion_results
            )
            
            # Handle extra messages
            extra_msg_ids = []
            for res in deletion_results:
                if res.get('msg_ids_to_delete'):
                    extra_msg_ids.extend(res['msg_ids_to_delete'])
            
            if extra_msg_ids:
                unique_ids = list(set(extra_msg_ids))
                logger.info(f"Emptying trash: Deleting {len(unique_ids)} extra chunks from Telegram.")
                for i in range(0, len(unique_ids), 100):
                    chunk = unique_ids[i:i + 100]
                    await client.delete_messages(self.shared_state.group_id, chunk)
            
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}

        except Exception as e:
            logger.error(f"Error emptying trash: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def get_trash_items(self) -> Dict[str, Any]:
        try:
            def _sync_get():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                return trash_repo.get_trashed_items()
            
            return await asyncio.to_thread(_sync_get)
        except Exception as e:
            logger.error(f"Error fetching trash items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    async def cleanup_expired_trash(self):
        logger.info("Starting expired trash cleanup...")
        try:
            if not self.shared_state.client or not self.shared_state.client.is_connected():
                logger.warning("Skipping trash cleanup: Client not connected.")
                return

            def _get_expired():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                return trash_repo.get_expired_items()

            expired_items = await self.shared_state.loop.run_in_executor(None, _get_expired)
            
            if expired_items:
                logger.info(f"Found {len(expired_items)} expired items. Deleting permanently...")
                await self.delete_items_permanently(expired_items)
                logger.info("Expired trash cleanup complete.")
            else:
                logger.info("No expired trash items found.")

        except Exception as e:
            logger.error(f"Error during expired trash cleanup: {e}", exc_info=True)

    async def move_items(self, items: List[Dict[str, Any]], target_folder_id: int) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("連線失敗，請檢查網路或重新登入。")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_move():
                db = DatabaseConnection()
                file_repo = FileRepository(db)
                folder_repo = FolderRepository(db)
                
                # Fetch old parent IDs before moving
                old_parent_ids = set()
                conn = db._get_conn()
                cursor = conn.cursor()
                
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    if item_type == 'folder':
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['parent_id'] is not None:
                            old_parent_ids.add(row['parent_id'])
                        folder_repo.move_folder(item_id, target_folder_id)
                    else:
                        cursor.execute("SELECT folder_id FROM file_folder_map WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row['folder_id'] is not None:
                            old_parent_ids.add(row['folder_id'])
                        file_repo.move_file(item_id, target_folder_id)
                return list(old_parent_ids)

            old_parent_ids = await asyncio.to_thread(_sync_move)
            
            # Evaluate fragmentation for origin and destination folders
            folders_to_evaluate = old_parent_ids + [target_folder_id]
            self.defrag_worker.evaluate_folders(folders_to_evaluate)
                
            # Adaptive sync handled by DatabaseConnection
            return {"success": True}

        except errors.PathNotFoundError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Item already exists during move: {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except errors.InvalidNameError as e: # Catch circular dependency error
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.INVALID_OPERATION}
        except Exception as e:
            logger.error(f"Unknown error moving items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}
