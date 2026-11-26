import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from ..thumbnail.manager import ThumbnailManager

from core_app.core import errors, utils
from core_app.infrastructure.telegram import telegram_comms
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository

from core_app.application.file_system.defrag_worker import DefragWorker
from core_app.application.file_system.preview_service import PreviewService
from core_app.application.file_system.trash_service import TrashService

logger = logging.getLogger(__name__)


class FileService:
    """Facade for folder/file browsing, search, item mutations, trash, and previews."""

    def __init__(self, shared_state: 'SharedState', thumb_manager: 'ThumbnailManager', buffer=None, preview_cache=None):
        self.shared_state = shared_state
        self.preview_service = PreviewService(shared_state, thumb_manager, buffer=buffer, preview_cache=preview_cache)
        self.defrag_worker = DefragWorker(shared_state, self)
        self.defrag_worker.start()
        self.trash_service = TrashService(shared_state, self.defrag_worker, self._invalidate_deleted_caches)

    # Attribute properties preserve compatibility with tests reassigning caches directly on FileService

    @property
    def thumb_manager(self):
        return self.preview_service.thumb_manager if hasattr(self, 'preview_service') else getattr(self, '_fallback_thumb_manager', None)

    @thumb_manager.setter
    def thumb_manager(self, value) -> None:
        if hasattr(self, 'preview_service'):
            self.preview_service.thumb_manager = value
        else:
            self._fallback_thumb_manager = value

    @property
    def buffer(self):
        return self.preview_service.buffer if hasattr(self, 'preview_service') else getattr(self, '_fallback_buffer', None)

    @buffer.setter
    def buffer(self, value) -> None:
        if hasattr(self, 'preview_service'):
            self.preview_service.buffer = value
        else:
            self._fallback_buffer = value

    @property
    def preview_cache(self):
        return self.preview_service.preview_cache if hasattr(self, 'preview_service') else getattr(self, '_fallback_preview_cache', None)

    @preview_cache.setter
    def preview_cache(self, value) -> None:
        if hasattr(self, 'preview_service'):
            self.preview_service.preview_cache = value
        else:
            self._fallback_preview_cache = value

    @property
    def previewer(self):
        return self.preview_service.previewer if hasattr(self, 'preview_service') else getattr(self, '_fallback_previewer', None)

    @previewer.setter
    def previewer(self, value) -> None:
        if hasattr(self, 'preview_service'):
            self.preview_service.previewer = value
        else:
            self._fallback_previewer = value

    @property
    def text_previewer(self):
        return self.preview_service.text_previewer if hasattr(self, 'preview_service') else getattr(self, '_fallback_text_previewer', None)

    @text_previewer.setter
    def text_previewer(self, value) -> None:
        if hasattr(self, 'preview_service'):
            self.preview_service.text_previewer = value
        else:
            self._fallback_text_previewer = value

    @property
    def _active_preview_downloads(self) -> Dict[int, asyncio.Future]:
        return self.preview_service._active_preview_downloads

    @property
    def _active_doc_downloads(self) -> Dict[tuple, asyncio.Future]:
        return self.preview_service._active_doc_downloads

    # --- Preview & Thumbnail Delegation ---

    async def get_thumbnails(self, folder_id: int, return_file_id_keys: bool = False) -> Dict[str, Any]:
        """Return base64 thumbnails for a folder via PreviewService."""
        return await self.preview_service.get_thumbnails(folder_id, return_file_id_keys=return_file_id_keys)

    async def get_preview(self, map_id: int) -> Dict[str, Any]:
        """Fetch an image preview for map_id via PreviewService."""
        return await self.preview_service.get_preview(map_id)

    async def _ensure_client(self):
        return await utils.ensure_client_connected(self.shared_state)

    async def _download_full_file(self, content_id: int, file_size: int, file_hash: str) -> bytes:
        return await self.preview_service.download_full_file(content_id, file_size, file_hash)

    async def _get_file_info(self, map_id: int) -> dict:
        return await self.preview_service.get_file_info(map_id)

    async def get_preview_file(self, map_id: int) -> dict:
        """Render a document preview for map_id."""
        from core_app.application.preview.document.viewer import DocumentPreviewer
        return await DocumentPreviewer.get_preview(map_id, self)

    async def load_full_document(self, map_id: int) -> dict:
        """Load full converted document bytes for map_id."""
        from core_app.application.preview.document.viewer import DocumentPreviewer
        return await DocumentPreviewer.load_full(map_id, self)

    async def get_preview_text(self, map_id: int) -> dict:
        """Fetch initial paged text preview for map_id."""
        file_info = await self._get_file_info(map_id)
        return await self.text_previewer.get_preview(file_info)

    async def get_text_page(self, map_id: int, page_index: int) -> dict:
        """Fetch a specific page from a cached/streamed text document."""
        file_info = await self._get_file_info(map_id)
        return await self.text_previewer.get_text_page(page_index, file_info)

    async def get_file_extended_details(self, map_id: int) -> Dict[str, Any]:
        """Fetch extended media/document metadata via PreviewService."""
        return await self.preview_service.get_file_extended_details(map_id)

    async def _invalidate_deleted_caches(self, deletion_results: List[Dict[str, Any]]) -> None:
        await self.preview_service.invalidate_deleted_caches(deletion_results)

    def evict_idle_memory_caches(self, max_idle_seconds: float = 300.0) -> int:
        """Evict idle image and text entries from in-memory caches after inactivity."""
        return self.preview_service.evict_idle_memory_caches(max_idle_seconds)

    def clear_memory_caches(self) -> None:
        """Release all in-memory preview caches on logout or shutdown."""
        self.preview_service.clear_memory_caches()

    # --- Trash Delegation ---

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Move selected items to Recycle Bin via TrashService."""
        return await self.trash_service.delete_items(items)

    async def restore_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Restore trashed items via TrashService."""
        return await self.trash_service.restore_items(items)

    async def delete_items_permanently(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Permanently delete items via TrashService."""
        return await self.trash_service.delete_items_permanently(items)

    async def empty_trash(self) -> Dict[str, Any]:
        """Empty Recycle Bin via TrashService."""
        return await self.trash_service.empty_trash()

    async def get_trash_items(self) -> Dict[str, Any]:
        """List trashed items via TrashService."""
        return await self.trash_service.get_trash_items()

    async def cleanup_expired_trash(self) -> None:
        """Remove expired Recycle Bin items via TrashService."""
        await self.trash_service.cleanup_expired_trash()

    # --- Folder & File Navigation / Mutations ---

    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        """Fetch folder items and append any currently active upload placeholders."""
        logger.debug(f"Fetching contents for folder_id: {folder_id} from database.")
        try:
            def _sync_db_op():
                db = DatabaseConnection()
                FileRepository(db)
                folder_repo = FolderRepository(db)
                TrashRepository(db)
                contents = folder_repo.get_folder_contents(folder_id)

                from core_app.infrastructure.database.transfer_db.transfer_database import TransferDatabaseConnection
                from core_app.infrastructure.database.transfer_db.queue_repository import QueueRepository
                tdb = QueueRepository(TransferDatabaseConnection())
                active_uploads = tdb.get_active_uploads(folder_id)

                if active_uploads:
                    for task in active_uploads:
                        file_name = os.path.basename(task['local_path'])
                        if any(f['name'] == file_name for f in contents['files']):
                            continue
                        contents['files'].append({
                            "id": task['task_id'],
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
            logger.error("Folder does not exist.")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except Exception as e:
            logger.error(f"Error getting folder contents for id {folder_id}: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        """Fetch flat folder tree rows from the in-memory SQLite repository."""
        try:
            return FolderRepository(DatabaseConnection()).get_folder_tree()
        except Exception as e:
            logger.error(f"An error occurred while fetching the folder tree: {e}", exc_info=True)
            return []

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        """Fetch flat recursive contents of a folder for bulk downloads or exports."""
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
        """Stream FTS5 folder/file search batches asynchronously via callback signal."""
        logger.debug(f"Starting streaming search from base_id: {base_folder_id} for term: '{search_term}'")

        def progress_callback(batch_results: dict):
            try:
                result_signal_emitter({'request_id': request_id, 'type': 'batch', 'data': batch_results})
            except Exception as e:
                logger.error(f"Error emitting search results batch: {e}", exc_info=True)

        def db_search_sync():
            try:
                thread_local_db = DatabaseConnection()
                file_repo = FileRepository(thread_local_db)
                file_repo.search_db_items(search_term, base_folder_id, progress_callback)
                result_signal_emitter({'request_id': request_id, 'type': 'done'})
                logger.debug(f"Streaming search completed for request_id: {request_id}.")
            except Exception as e:
                logger.error(f"Critical error in background search thread: {e}", exc_info=True)
                result_signal_emitter({'request_id': request_id, 'type': 'error', 'data': {'error_code': errors.ErrorCode.INTERNAL_ERROR}})

        try:
            await asyncio.to_thread(db_search_sync)
        except Exception as e:
            logger.error(f"Failed to start background search thread: {e}", exc_info=True)
            result_signal_emitter({'request_id': request_id, 'type': 'error', 'data': {'error_code': errors.ErrorCode.ASYNC_CALL_FAILED}})

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        """Create a new cloud folder under parent_id."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
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
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to create folder '{folder_name}': {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except Exception as e:
            logger.error(f"Unknown error creating folder '{folder_name}'.", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        """Rename a folder or file entry."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
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
            return {"success": True}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Failed to rename item {item_id}: {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except Exception as e:
            logger.error(f"Unknown error renaming item {item_id}.", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def move_items(self, items: List[Dict[str, Any]], target_folder_id: int) -> Dict[str, Any]:
        """Move files/folders into target_folder_id and trigger defragmentation checks."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_move():
                db = DatabaseConnection()
                file_repo = FileRepository(db)
                folder_repo = FolderRepository(db)
                old_parent_ids = set()
                with db.batch_write():
                    conn = db._get_conn()
                    cursor = conn.cursor()
                    for item in items:
                        item_id, item_type = item['id'], item['type']
                        parent_col = "parent_id" if item_type == 'folder' else "folder_id"
                        table_name = "folders" if item_type == 'folder' else "file_folder_map"
                        cursor.execute(f"SELECT {parent_col} FROM {table_name} WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row[parent_col] is not None:
                            old_parent_ids.add(row[parent_col])
                        if item_type == 'folder':
                            folder_repo.move_folder(item_id, target_folder_id)
                        else:
                            file_repo.move_file(item_id, target_folder_id)
                return list(old_parent_ids)

            old_parent_ids = await asyncio.to_thread(_sync_move)
            self.defrag_worker.evaluate_folders(old_parent_ids + [target_folder_id])
            return {"success": True}
        except errors.PathNotFoundError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except errors.ItemAlreadyExistsError as e:
            logger.warning(f"Item already exists during move: {e}")
            return {"success": False, "error_code": errors.ErrorCode.ITEM_ALREADY_EXISTS}
        except errors.InvalidNameError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.INVALID_OPERATION}
        except Exception as e:
            logger.error(f"Unknown error moving items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    def get_all_audio_files_sync(self) -> Dict[str, Any]:
        """Synchronously return all active cloud audio files with folder metadata."""
        try:
            items = FileRepository(DatabaseConnection()).get_all_audio_files()
            return {"success": True, "items": items}
        except Exception as e:
            logger.error(f"Error fetching all audio files: {e}", exc_info=True)
            return {"success": False, "items": [], "error_code": errors.ErrorCode.DB_READ_FAILED}
