import logging
import asyncio
from telethon import errors as telethon_errors
from typing import TYPE_CHECKING, List, Dict, Any, Callable

if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState

from . import utils
from core_app.common import errors
from core_app.api import telegram_comms
from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FileService:
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        logger.info(f"Fetching contents for folder_id: {folder_id} from database.")
        try:
            def _sync_db_op():
                db = DatabaseHandler()
                return db.get_folder_contents(folder_id)
            
            return await asyncio.to_thread(_sync_db_op)
        except Exception as e:
            logger.error(f"Error getting folder contents for id {folder_id}: {e}", exc_info=True)
            return {"success": False, "error_code": "DB_READ_FAILED", "message": "無法讀取資料夾內容。"}

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        logger.info(f"Recursively fetching contents for folder_id: {folder_id}.")
        try:
            def _sync_db_op():
                db = DatabaseHandler()
                return db.get_folder_contents_recursive(folder_id)
            
            return await asyncio.to_thread(_sync_db_op)
        except Exception as e:
            logger.error(f"Error recursively fetching folder contents for id {folder_id}: {e}", exc_info=True)
            return {"folder_name": "Error", "items": [], "success": False, "error_code": "DB_READ_FAILED", "message": "無法讀取資料夾內容。"}

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
                error_payload = {'request_id': request_id, 'type': 'error', 'data': {'message': '搜尋過程中發生嚴重錯誤。'}}
                result_signal_emitter(error_payload)

        try:
            await asyncio.to_thread(db_search_sync)
        except Exception as e:
            logger.error(f"Failed to start background search thread: {e}", exc_info=True)
            error_payload = {'request_id': request_id, 'type': 'error', 'data': {'message': '無法啟動背景搜尋任務。'}}
            result_signal_emitter(error_payload)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
        
        try:
            def _sync_create():
                db = DatabaseHandler()
                db.add_folder(parent_id, folder_name)
            
            await asyncio.to_thread(_sync_create)
            
            logger.info(f"Successfully created folder '{folder_name}' under parent_id {parent_id}.")
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to create folder '{folder_name}': {e}")
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except Exception as e:
            logger.error(f"Unknown error creating folder '{folder_name}'.", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "建立資料夾時發生未知的內部錯誤。"}

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
            
        try:
            def _sync_rename():
                db = DatabaseHandler()
                if item_type == 'folder':
                    db.rename_folder(item_id, new_name)
                else:
                    db.rename_file(item_id, new_name)
            
            await asyncio.to_thread(_sync_rename)
            
            logger.info(f"Successfully renamed {item_type} with id {item_id} to '{new_name}'.")
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to rename item {item_id}: {e}")
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except Exception as e:
            logger.error(f"Unknown error renaming item {item_id}.", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "重新命名時發生未知的內部錯誤。"}

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

        try:
            def _sync_soft_delete():
                db = DatabaseHandler()
                for item in items:
                    db.soft_delete_item(item['id'], item['type'])
            
            await asyncio.to_thread(_sync_soft_delete)
            
            logger.info(f"Successfully moved {len(items)} items to Recycle Bin.")
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True, "message": f"成功將 {len(items)} 個項目移至回收桶。"}
        
        except errors.PathNotFoundError as e:
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except errors.InvalidOperationError as e:
            return {"success": False, "error_code": "INVALID_OPERATION", "message": str(e)}
        except Exception as e:
            logger.error(f"Error soft deleting items: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "刪除過程中發生未知的錯誤。"}

    async def restore_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

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
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True, "message": f"成功還原 {len(items)} 個項目。"}

        except errors.PathNotFoundError as e:
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except Exception as e:
            logger.error(f"Error restoring items: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "還原過程中發生未知的錯誤。"}

    async def delete_items_permanently(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

        all_message_ids_to_delete = []
        try:
            def _sync_delete():
                db = DatabaseHandler()
                ids_to_del = []
                for item in items:
                    item_id, item_type = item['id'], item['type']
                    deleted_ids = db.remove_folder(item_id) if item_type == 'folder' else db.remove_file(item_id)
                    ids_to_del.extend(deleted_ids)
                    logger.info(f"Marked {item_type} id {item_id} for permanent deletion.")
                return ids_to_del

            all_message_ids_to_delete = await asyncio.to_thread(_sync_delete)
            
            await utils.trigger_db_upload_in_background(self.shared_state)
            
            if all_message_ids_to_delete:
                logger.info(f"Preparing to delete {len(all_message_ids_to_delete)} chunks from Telegram.")
                
                for i in range(0, len(all_message_ids_to_delete), 100):
                    chunk = all_message_ids_to_delete[i:i + 100]
                    await client.delete_messages(self.shared_state.group_id, chunk)
                logger.info("Successfully deleted chunks from Telegram.")
            else:
                logger.info("No remote chunks need to delete from Telegram.")

            return {"success": True, "message": f"成功永久刪除 {len(items)} 個項目。"}

        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to delete item: {e}")
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"Delete operation hit a flood wait for {e.seconds} seconds.")
            return {"success": False, "error_code": "FLOOD_WAIT_ERROR", "message": f"請求過多，請等待 {e.seconds} 秒。"}
        except Exception as e:
            logger.error(f"An unknown error occurred while deleting items: {items}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "刪除過程中發生未知的錯誤。"}

    async def empty_trash(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

        try:
            def _sync_empty():
                db = DatabaseHandler()
                return db.empty_trash()

            message_ids = await asyncio.to_thread(_sync_empty)
            
            await utils.trigger_db_upload_in_background(self.shared_state)
            
            if message_ids:
                logger.info(f"Emptying trash: Deleting {len(message_ids)} chunks from Telegram.")
                for i in range(0, len(message_ids), 100):
                    chunk = message_ids[i:i + 100]
                    await client.delete_messages(self.shared_state.group_id, chunk)
            
            return {"success": True, "message": "回收桶已清空。"}

        except Exception as e:
            logger.error(f"Error emptying trash: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "清空回收桶時發生錯誤。"}

    async def get_trash_items(self) -> Dict[str, Any]:
        try:
            def _sync_get():
                db = DatabaseHandler()
                return db.get_trashed_items()
            
            return await asyncio.to_thread(_sync_get)
        except Exception as e:
            logger.error(f"Error fetching trash items: {e}", exc_info=True)
            return {"success": False, "error_code": "DB_READ_FAILED", "message": "無法讀取回收桶內容。"}

    async def cleanup_expired_trash(self):
        logger.info("Starting expired trash cleanup...")
        try:
            if not self.shared_state.client or not self.shared_state.client.is_connected():
                logger.warning("Skipping trash cleanup: Client not connected.")
                return

            def _get_expired():
                db = DatabaseHandler()
                return db.get_expired_items()

            expired_items = await asyncio.to_thread(_get_expired)
            
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
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

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
                
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True, "message": f"成功移動 {moved_count} 個項目。"}

        except errors.PathNotFoundError as e:
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except errors.ItemAlreadyExistsError as e:
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except errors.InvalidNameError as e: # Catch circular dependency error
            return {"success": False, "error_code": "INVALID_OPERATION", "message": str(e)}
        except Exception as e:
            logger.error(f"Unknown error moving items: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "移動過程中發生未知的錯誤。"}
