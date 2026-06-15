import logging
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState

from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
from core_app.infrastructure.database.main_db.repositories.map_repository import MapRepository

logger = logging.getLogger(__name__)

class FolderService:
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state
        self.db = DatabaseConnection()
        self.file_repo = FileRepository(self.db)
        self.folder_repo = FolderRepository(self.db)
        self.trash_repo = TrashRepository(self.db)
        self.map_repo = MapRepository(self.db)

    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        logger.info("Fetching flat folder tree from the database.")
        try:
            return self.folder_repo.get_folder_tree()
        except Exception as e:
            logger.error(f"An error occurred while fetching the folder tree: {e}", exc_info=True)
            return []
