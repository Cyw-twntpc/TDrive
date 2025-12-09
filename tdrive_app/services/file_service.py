import logging
import asyncio
from telethon import errors as telethon_errors
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from ..shared_state import SharedState

from . import utils
from .. import errors, telegram_comms
from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FileService:
    """
    處理所有檔案與資料夾的 CRUD (建立、讀取、更新、刪除) 操作。
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        """
        根據資料夾 ID 獲取其內容（子資料夾和檔案）。
        """
        logger.info(f"正在從資料庫獲取資料夾 ID: {folder_id} 的內容。")
        try:
            # DB 操作本身是快速的同步 I/O，直接呼叫是可接受的
            db = DatabaseHandler()
            return db.get_folder_contents(folder_id)
        except Exception as e:
            logger.error(f"獲取資料夾內容時發生錯誤 (ID: {folder_id}): {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "無法獲取資料夾內容。"}

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        """
        遞迴地獲取一個資料夾的所有子項目資訊。
        """
        logger.info(f"正在遞迴地獲取資料夾 ID: {folder_id} 的內容。")
        try:
            db = DatabaseHandler()
            return db.get_folder_contents_recursive(folder_id)
        except Exception as e:
            logger.error(f"遞迴獲取資料夾內容時發生錯誤 (ID: {folder_id}): {e}", exc_info=True)
            return {"folder_name": "Error", "items": [], "success": False, "error_code": "INTERNAL_ERROR", "message": "無法獲取資料夾內容。"}

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter, request_id: str):
        """
        在背景執行緒中執行串流式資料庫搜尋。
        """
        logger.info(f"啟動串流式搜尋，基礎 ID: {base_folder_id}，關鍵字: '{search_term}'")

        def progress_callback(batch_results):
            """這是從背景執行緒呼叫的回呼函式，它將透過 signal 發送批次結果。"""
            try:
                # Signal.emit() is thread-safe in Qt
                payload = {'request_id': request_id, 'type': 'batch', 'data': batch_results}
                result_signal_emitter(payload)
                logger.debug(f"已發送一批 {len(batch_results.get('files', [])) + len(batch_results.get('folders', []))} 個搜尋結果。")
            except Exception as e:
                logger.error(f"在搜尋回呼中發送訊號時出錯: {e}", exc_info=True)

        def db_search_sync():
            """
            這個同步函式將在獨立的執行緒中執行，以避免阻塞 asyncio 事件迴圈。
            """
            try:
                # 為此執行緒建立獨立的 DatabaseHandler 實例，確保執行緒安全
                thread_local_db = DatabaseHandler()
                thread_local_db.search_db_items(search_term, base_folder_id, progress_callback)
                
                # 所有搜尋完成後，發送 'done' 訊號
                done_payload = {'request_id': request_id, 'type': 'done'}
                result_signal_emitter(done_payload)
                logger.info(f"串流式搜尋完成 (request_id: {request_id})。")
            except Exception as e:
                logger.error(f"背景搜尋執行緒發生嚴重錯誤: {e}", exc_info=True)
                error_payload = {
                    'request_id': request_id, 
                    'type': 'error',
                    'data': {'message': '搜尋時發生嚴重錯誤。'}
                }
                result_signal_emitter(error_payload)

        try:
            # 使用 asyncio.to_thread 執行同步的、會阻塞的資料庫搜尋函式
            await asyncio.to_thread(db_search_sync)
        except Exception as e:
            logger.error(f"無法啟動背景搜尋執行緒: {e}", exc_info=True)
            # Handle case where the thread itself fails to start
            error_payload = {
                'request_id': request_id,
                'type': 'error',
                'data': {'message': '無法啟動背景搜尋任務。'}
            }
            result_signal_emitter(error_payload)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        """
        在指定父資料夾 ID 下建立新資料夾。
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
        
        try:
            db = DatabaseHandler()
            db.add_folder(parent_id, folder_name)
            logger.info(f"在資料夾 ID {parent_id} 下成功建立資料夾 '{folder_name}'。")
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"建立資料夾 '{folder_name}' 失敗: {e}")
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except Exception as e:
            logger.error(f"建立資料夾 '{folder_name}' 時發生未知錯誤。", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "發生未知的內部錯誤。"}

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        """
        重新命名檔案或資料夾。
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
            
        try:
            db = DatabaseHandler()
            if item_type == 'folder':
                db.rename_folder(item_id, new_name)
            else: # 'file'
                db.rename_file(item_id, new_name)
            
            logger.info(f"{item_type} ID {item_id} 已成功重新命名為 '{new_name}'。")
            await utils.trigger_db_upload_in_background(self.shared_state)
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"重新命名項目 ID {item_id} 失敗: {e}")
            return {"success": False, "error_code": "ITEM_ALREADY_EXISTS", "message": str(e)}
        except Exception as e:
            logger.error(f"重新命名項目 ID {item_id} 時發生未知錯誤。", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "發生未知的內部錯誤。"}

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        刪除多個檔案或資料夾。
        `items` 格式: [{'id': 1, 'type': 'file'}, {'id': 2, 'type': 'folder'}]
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}

        all_message_ids_to_delete = []
        try:
            db = DatabaseHandler()
            for item in items:
                item_id, item_type = item['id'], item['type']
                deleted_ids = db.remove_folder(item_id) if item_type == 'folder' else db.remove_file(item_id)
                all_message_ids_to_delete.extend(deleted_ids)
                logger.info(f"已在資料庫中標記刪除 {item_type} ID {item_id}。")
            
            await utils.trigger_db_upload_in_background(self.shared_state)
            
            if all_message_ids_to_delete:
                logger.info(f"準備從 Telegram 刪除 {len(all_message_ids_to_delete)} 個分塊...")
                group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
                
                for i in range(0, len(all_message_ids_to_delete), 100):
                    chunk = all_message_ids_to_delete[i:i + 100]
                    await client.delete_messages(group_id, chunk)
                logger.info("Telegram 上的分塊訊息已成功刪除。")

            return {"success": True, "message": f"成功刪除 {len(items)} 個項目。"}

        except errors.PathNotFoundError as e:
            logger.warning(f"刪除項目失敗: {e}")
            return {"success": False, "error_code": "PATH_NOT_FOUND", "message": str(e)}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"刪除操作觸發請求限制，需等待 {e.seconds} 秒。")
            return {"success": False, "error_code": "FLOOD_WAIT_ERROR", "message": f"請求過於頻繁，請等待 {e.seconds} 秒。"}
        except Exception as e:
            logger.error(f"刪除項目時發生未知錯誤: {items}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "刪除時發生未知錯誤。"}
