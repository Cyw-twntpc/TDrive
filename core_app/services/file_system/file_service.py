from core_app.common.errors import ErrorCode
import logging
import asyncio
from telethon import errors as telethon_errors
from typing import TYPE_CHECKING, List, Dict, Any, Callable

if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState
    from ..media.gallery_manager import GalleryManager

from ..common import utils
from core_app.common import errors
from core_app.api import telegram_comms
from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FileService:
    def __init__(self, shared_state: 'SharedState', gallery_manager: 'GalleryManager'):
        self.shared_state = shared_state
        self.gallery_manager = gallery_manager

    # --- Gallery Integration ---

    async def get_thumbnails(self, folder_id: int) -> Dict[str, Any]:
        """Returns base64 thumbnails for the folder. Downloads DB if not in memory."""
        try:
            if not self.gallery_manager.has_db(folder_id):
                # Try to load from cloud
                def _get_db_info():
                    db = DatabaseHandler()
                    conn = db._get_conn()
                    cur = conn.cursor()
                    cur.execute("SELECT thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id = ?", (folder_id,))
                    return cur.fetchone()

                db_info = await asyncio.to_thread(_get_db_info)
                
                if db_info and db_info['thumbs_db_msg_id']:
                    client = await utils.ensure_client_connected(self.shared_state)
                    if client:
                        logger.info(f"Downloading thumbs.db for folder {folder_id}...")
                        db_bytes = await telegram_comms.download_data_as_bytes(
                            client, self.shared_state.group_id, [db_info['thumbs_db_msg_id']], db_info['thumbs_db_hash']
                        )
                        if db_bytes:
                            self.gallery_manager.load_thumbs_db_from_bytes(folder_id, db_bytes)
            
            thumbs = self.gallery_manager.get_folder_thumbnails(folder_id)
            
            # [Fix] Convert Content IDs to Map IDs for frontend consistency.
            # The frontend uses Map ID (file_folder_map.id) for data-id attributes,
            # but thumbs.db stores Content ID (files.id). We must map them.
            if thumbs:
                def _get_id_mapping():
                    db = DatabaseHandler()
                    conn = db._get_conn()
                    cur = conn.cursor()
                    # Get map_id (id) and content_id (file_id) for items in this folder
                    cur.execute("SELECT id, file_id FROM file_folder_map WHERE folder_id = ?", (folder_id,))
                    return {row['file_id']: row['id'] for row in cur.fetchall()}

                # Run DB query in thread pool
                id_map = await asyncio.to_thread(_get_id_mapping)
                
                # Re-key the thumbnails dictionary
                mapped_thumbs = {}
                for content_id_str, b64_data in thumbs.items():
                    try:
                        content_id = int(content_id_str)
                        if content_id in id_map:
                            map_id = id_map[content_id]
                            mapped_thumbs[str(map_id)] = b64_data
                    except ValueError:
                        pass
                
                thumbs = mapped_thumbs

            logger.info(f"FileService returning {len(thumbs)} thumbnails for {folder_id} (Mapped IDs)")
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
                db = DatabaseHandler()
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
                logger.info(f"Downloading preview for file map {file_id} (content {info['content_id']})...")
                preview_bytes = await telegram_comms.download_data_as_bytes(
                    client, self.shared_state.group_id, [info['preview_msg_id']], info['preview_hash']
                )
            else:
                # Fallback: Download first chunk of original file if no preview exists
                logger.info(f"No preview found for {file_id}, attempting fallback to original file...")
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
                import base64
                b64_str = base64.b64encode(preview_bytes).decode('utf-8')
                return {"success": True, "preview": b64_str}
            
            logger.error("Download failed")
            return {"success": False}

        except Exception as e:
            logger.error(f"Error fetching preview: {e}", exc_info=True)
            return {"success": False}

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        logger.info(f"Fetching contents for folder_id: {folder_id} from database.")
        try:
            def _sync_db_op():
                db = DatabaseHandler()
                contents = db.get_folder_contents(folder_id)
                
                # Integrate active uploads from TransferDB
                from core_app.data.transfer_db_handler import TransferDBHandler
                tdb = TransferDBHandler()
                active_uploads = tdb.get_active_uploads(folder_id)
                
                if active_uploads:
                    import time
                    import os
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
                            "size": db._format_size(task['total_size']),
                            "modif_date": db._format_timestamp(time.time()),
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
        logger.info(f"Recursively fetching contents for folder_id: {folder_id}.")
        try:
            def _sync_db_op():
                db = DatabaseHandler()
                return db.get_folder_contents_recursive(folder_id)
            
            return await asyncio.to_thread(_sync_db_op)
        except Exception as e:
            logger.error(f"Error recursively fetching folder contents for id {folder_id}: {e}", exc_info=True)
            return {"folder_name": "Error", "items": [], "success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter: Callable, request_id: str):
        logger.info(f"Starting streaming search from base_id: {base_folder_id} for term: '{search_term}'")

        def progress_callback(batch_results: dict):
            try:
                payload = {'request_id': request_id, 'type': 'batch', 'data': batch_results}
                result_signal_emitter(payload)
            except Exception as e:
                logger.error(f"Error emitting search results batch: {e}", exc_info=True)

        def db_search_sync():
            try:
                thread_local_db = DatabaseHandler()
                thread_local_db.search_db_items(search_term, base_folder_id, progress_callback)
                
                done_payload = {'request_id': request_id, 'type': 'done'}
                result_signal_emitter(done_payload)
                logger.info(f"Streaming search completed for request_id: {request_id}.")
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
                db = DatabaseHandler()
                db.add_folder(parent_id, folder_name)
            
            await asyncio.to_thread(_sync_create)
            
            logger.info(f"Successfully created folder '{folder_name}' under parent_id {parent_id}.")
            # Adaptive sync handled by DatabaseHandler
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
                db = DatabaseHandler()
                if item_type == 'folder':
                    db.rename_folder(item_id, new_name)
                else:
                    db.rename_file(item_id, new_name)
            
            await asyncio.to_thread(_sync_rename)
            
            logger.info(f"Successfully renamed {item_type} with id {item_id} to '{new_name}'.")
            # Adaptive sync handled by DatabaseHandler
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
                db = DatabaseHandler()
                for item in items:
                    db.soft_delete_item(item['id'], item['type'])
            
            await asyncio.to_thread(_sync_soft_delete)
            
            logger.info(f"Successfully moved {len(items)} items to Recycle Bin.")
            # Adaptive sync handled by DatabaseHandler
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
                db = DatabaseHandler()
                restored_names = []
                for item in items:
                    name = db.restore_item(item['id'], item['type'])
                    restored_names.append(name)
                return restored_names
            
            restored_names = await asyncio.to_thread(_sync_restore)
            
            logger.info(f"Successfully restored {len(items)} items.")
            # Adaptive sync handled by DatabaseHandler
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
                db = DatabaseHandler()
                deletion_results = []
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    if item_type == 'folder':
                        res_list = db.remove_folder(item_id)
                        deletion_results.extend(res_list)
                    else:
                        res = db.remove_file(item_id)
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
            
            # Adaptive sync handled by DatabaseHandler
            return {"success": True}

        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to delete item: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"Delete operation hit a flood wait for {e.seconds} seconds.")
            return {"success": False, "error_code": errors.ErrorCode.FLOOD_WAIT_ERROR}
        except Exception as e:
            logger.error(f"An unknown error occurred while deleting items: {items}", exc_info=True)
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
                db = DatabaseHandler()
                return db.empty_trash()

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
            
            # Adaptive sync handled by DatabaseHandler
            return {"success": True}

        except Exception as e:
            logger.error(f"Error emptying trash: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def get_trash_items(self) -> Dict[str, Any]:
        try:
            def _sync_get():
                db = DatabaseHandler()
                return db.get_trashed_items()
            
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
                db = DatabaseHandler()
                return db.get_expired_items()

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
                db = DatabaseHandler()
                count = 0
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    if item_type == 'folder':
                        db.move_folder(item_id, target_folder_id)
                    else:
                        db.move_file(item_id, target_folder_id)
                    count += 1
                return count

            moved_count = await asyncio.to_thread(_sync_move)
                
            # Adaptive sync handled by DatabaseHandler
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
