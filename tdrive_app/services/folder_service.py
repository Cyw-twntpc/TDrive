import logging
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from ..shared_state import SharedState

from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)
db = DatabaseHandler()

class FolderService:
    """
    處理所有與資料夾結構讀取和遍歷相關的服務。
    """
    def __init__(self, shared_state: 'SharedState'):
        """
        雖然此服務目前不直接使用 shared_state，但為了架構一致性依然傳入。
        """
        self.shared_state = shared_state

    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        """
        獲取資料庫中所有資料夾的扁平化列表，供前端建構樹狀結構。
        這是對 db_handler.get_folder_tree 的一個簡單封裝。
        """
        logger.info("正在從資料庫獲取扁平化資料夾樹。")
        try:
            return db.get_folder_tree()
        except Exception as e:
            logger.error(f"獲取資料夾樹時發生錯誤: {e}", exc_info=True)
            return [] # 發生錯誤時返回空列表
