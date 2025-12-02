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
db = DatabaseHandler()

class FileService:
    """
    處理所有檔案與資料夾的 CRUD (建立、讀取、更新、刪除) 操作。
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        """
        根據資料夾 ID 獲取其內容（子資料夾和檔案）。
        """
        logger.info(f"正在從資料庫獲取資料夾 ID: {folder_id} 的內容。")
        try:
            return db.get_folder_contents(folder_id)
        except Exception as e:
            logger.error(f"獲取資料夾內容時發生錯誤 (ID: {folder_id}): {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "無法獲取資料夾內容。"}

    def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        """
        遞迴地獲取一個資料夾的所有子項目資訊。
        """
        logger.info(f"正在遞迴地獲取資料夾 ID: {folder_id} 的內容。")
        try:
            return db.get_folder_contents_recursive(folder_id)
        except Exception as e:
            logger.error(f"遞迴獲取資料夾內容時發生錯誤 (ID: {folder_id}): {e}", exc_info=True)
            return {"folder_name": "Error", "items": [], "success": False, "error_code": "INTERNAL_ERROR", "message": "無法獲取資料夾內容。"}

    def search_db_items(self, base_folder_id: int, search_term: str) -> Dict[str, Any]:
        """
        在資料庫中搜尋項目並返回結果。
        """
        logger.info(f"正在搜尋，基礎資料夾 ID: {base_folder_id}，關鍵字: '{search_term}'")
        try:
            return db.search_db_items(search_term, base_folder_id)
        except Exception as e:
            logger.error(f"搜尋項目時發生錯誤 (關鍵字: '{search_term}'): {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "搜尋時發生未知錯誤。"}

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        """
        在指定父資料夾 ID 下建立新資料夾。
        """
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗，請檢查網路或重新登入。"}
        
        try:
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
