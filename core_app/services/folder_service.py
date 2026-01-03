import logging
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState

from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FolderService:
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state
        self.db = DatabaseHandler()

    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        logger.info("Fetching flat folder tree from the database.")
        try:
            return self.db.get_folder_tree()
        except Exception as e:
            logger.error(f"An error occurred while fetching the folder tree: {e}", exc_info=True)
            return []
