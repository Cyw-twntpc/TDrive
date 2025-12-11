import logging
import asyncio
from telethon import errors as telethon_errors
from typing import TYPE_CHECKING, List, Dict, Any, Callable

if TYPE_CHECKING:
    from ..shared_state import SharedState

from . import utils
from .. import errors, telegram_comms
from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FileService:
    """
    Handles the business logic for all file and folder operations (CRUD).
    
    This service acts as an intermediary between the API layer (Bridge) and
    the data layers (DatabaseHandler, telegram_comms), orchestrating
    database updates and remote storage actions.
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        """
        Retrieves the contents (subfolders and files) of a specific folder from the database.
        """
        logger.info(f"Fetching contents for folder_id: {folder_id} from database.")
        try:
            # Run the synchronous database operation in a separate thread to avoid blocking the event loop.
            def _sync_db_op():
                db = DatabaseHandler()
                return db.get_folder_contents(folder_id)
            
            return await asyncio.to_thread(_sync_db_op)
        except Exception as e:
            logger.error(f"Error getting folder contents for id {folder_id}: {e}", exc_info=True)
            return {"success": False, "error_code": "DB_READ_FAILED", "message": "無法讀取資料夾內容。"}

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        """
        Recursively retrieves all descendant items for a given folder. Used for folder downloads.
        """
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
        """
        Performs a streaming database search in a background thread.
        
        Results are not returned directly but are emitted in batches via the
        `result_signal_emitter` callback to keep the UI responsive.
        """
        logger.info(f"Starting streaming search from base_id: {base_folder_id} for term: '{search_term}'")

        def progress_callback(batch_results: dict):
            """Callback function to emit batch results from the background thread."""
            try:
                # Qt signals are thread-safe, so this can be called directly.
                payload = {'request_id': request_id, 'type': 'batch', 'data': batch_results}
                result_signal_emitter(payload)
            except Exception as e:
                logger.error(f"Error emitting search results batch: {e}", exc_info=True)

        def db_search_sync():
            """
            This synchronous function runs in a separate thread to avoid blocking
            the main asyncio event loop with a potentially long-running DB query.
            """
            try:
                # A new DB handler is instantiated here to ensure thread-safety.
                thread_local_db = DatabaseHandler()
                thread_local_db.search_db_items(search_term, base_folder_id, progress_callback)
                
                # Signal that the search is complete.
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
        """Creates a new folder in the database under the given parent ID."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
        
        try:
            db = DatabaseHandler()
            db.add_folder(parent_id, folder_name)
            logger.info(f"Successfully created folder '{folder_name}' under parent_id {parent_id}.")
            # After a structural change, trigger a database backup to the cloud.
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to create folder '{folder_name}': {e}")
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except Exception as e:
            logger.error(f"Unknown error creating folder '{folder_name}'.", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "建立資料夾時發生未知的內部錯誤。"}

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        """Renames a file or a folder."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
            
        try:
            db = DatabaseHandler()
            if item_type == 'folder':
                db.rename_folder(item_id, new_name)
            else:
                db.rename_file(item_id, new_name)
            
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
        """
        Deletes a list of files and/or folders from the database and remote storage.
        `items` format: [{'id': 1, 'type': 'file'}, {'id': 2, 'type': 'folder'}]
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

        all_message_ids_to_delete = []
        try:
            db = DatabaseHandler()
            # First, remove items from the local database and collect all remote message_ids.
            for item in items:
                item_id, item_type = item['id'], item['type']
                deleted_ids = db.remove_folder(item_id) if item_type == 'folder' else db.remove_file(item_id)
                all_message_ids_to_delete.extend(deleted_ids)
                logger.info(f"Marked {item_type} id {item_id} for deletion from database.")
            
            # After all DB changes are done, trigger a single DB upload.
            await utils.trigger_db_upload_in_background(self.shared_state)
            
            # Now, delete the corresponding messages from Telegram in batches.
            if all_message_ids_to_delete:
                logger.info(f"Preparing to delete {len(all_message_ids_to_delete)} chunks from Telegram.")
                group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
                
                # Telegram's delete_messages can handle up to 100 IDs at a time.
                for i in range(0, len(all_message_ids_to_delete), 100):
                    chunk = all_message_ids_to_delete[i:i + 100]
                    await client.delete_messages(group_id, chunk)
                logger.info("Successfully deleted chunks from Telegram.")

            return {"success": True, "message": f"成功刪除 {len(items)} 個項目。"}

        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to delete item: {e}")
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"Delete operation hit a flood wait for {e.seconds} seconds.")
            return {"success": False, "error_code": "FLOOD_WAIT_ERROR", "message": f"請求過多，請等待 {e.seconds} 秒。"}
        except Exception as e:
            logger.error(f"An unknown error occurred while deleting items: {items}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "刪除過程中發生未知的錯誤。"}
