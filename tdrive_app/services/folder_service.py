import logging
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from ..shared_state import SharedState

from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FolderService:
    """
    Provides services related to reading and traversing the folder structure.
    """
    def __init__(self, shared_state: 'SharedState'):
        """
        Initializes the FolderService.

        Args:
            shared_state: The shared state object. Although not directly used
                          in this service currently, it's passed for architectural
                          consistency.
        """
        self.shared_state = shared_state
        # Note: A single, module-level db handler instance might cause issues
        # in a multi-threaded context. For this service, it's acceptable as
        # it's only performing read operations.
        self.db = DatabaseHandler()

    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        """
        Retrieves a flat list of all folders from the database, which is
        used by the frontend to construct a tree view.
        
        This method is a simple wrapper around the corresponding db_handler call.
        """
        logger.info("Fetching flat folder tree from the database.")
        try:
            return self.db.get_folder_tree()
        except Exception as e:
            logger.error(f"An error occurred while fetching the folder tree: {e}", exc_info=True)
            return [] # Return an empty list on error to prevent UI crashes.
