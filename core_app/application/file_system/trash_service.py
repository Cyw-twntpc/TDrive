import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable, Dict, List

from telethon import errors as telethon_errors

from core_app.core import errors, utils
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from core_app.application.file_system.defrag_worker import DefragWorker

logger = logging.getLogger(__name__)


class TrashService:
    """Manages soft-deletion, restoration, expiration cleanup, and permanent Telegram purge."""

    def __init__(
        self,
        shared_state: 'SharedState',
        defrag_worker: 'DefragWorker',
        invalidate_caches_cb: Callable,
    ):
        self.shared_state = shared_state
        self.defrag_worker = defrag_worker
        self._invalidate_caches_cb = invalidate_caches_cb

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Soft-delete files/folders to the Recycle Bin and schedule defragmentation evaluation."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_soft_delete():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
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
                        trash_repo.soft_delete_item(item_id, item_type)

                    cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
                    rb_row = cursor.fetchone()
                    rb_id = rb_row['id'] if rb_row else None
                return list(old_parent_ids), rb_id

            old_parent_ids, recycle_bin_id = await asyncio.to_thread(_sync_soft_delete)
            folders_to_evaluate = list(old_parent_ids)
            if recycle_bin_id:
                folders_to_evaluate.append(recycle_bin_id)
            self.defrag_worker.evaluate_folders(folders_to_evaluate)

            logger.info(f"Successfully moved {len(items)} items to Recycle Bin.")
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
        """Restore files/folders from Recycle Bin and evaluate affected folders for defrag."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        try:
            def _sync_restore():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                new_parent_ids = set()
                restored_names = []
                with db.batch_write():
                    conn = db._get_conn()
                    cursor = conn.cursor()
                    for item in items:
                        item_id, item_type = item['id'], item['type']
                        restored_names.append(trash_repo.restore_item(item_id, item_type))
                        parent_col = "parent_id" if item_type == 'folder' else "folder_id"
                        table_name = "folders" if item_type == 'folder' else "file_folder_map"
                        cursor.execute(f"SELECT {parent_col} FROM {table_name} WHERE id = ?", (item_id,))
                        row = cursor.fetchone()
                        if row and row[parent_col] is not None:
                            new_parent_ids.add(row[parent_col])

                    cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
                    rb_row = cursor.fetchone()
                    rb_id = rb_row['id'] if rb_row else None
                return restored_names, list(new_parent_ids), rb_id

            _restored_names, new_parent_ids, recycle_bin_id = await asyncio.to_thread(_sync_restore)
            folders_to_evaluate = list(new_parent_ids)
            if recycle_bin_id:
                folders_to_evaluate.append(recycle_bin_id)
            self.defrag_worker.evaluate_folders(folders_to_evaluate)

            logger.info(f"Successfully restored {len(items)} items.")
            return {"success": True}
        except errors.PathNotFoundError as e:
            logger.error(f"Exception: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except Exception as e:
            logger.error(f"Error restoring items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def _finalize_permanent_deletion(
        self, client, deletion_results: List[Dict[str, Any]], log_prefix: str
    ) -> None:
        """Delete cloud metadata maps, batched preview/thumb Telegram messages, and local caches."""
        await self.shared_state.metadata_manager.handle_deletion(
            client, self.shared_state.group_id, self.shared_state.api_id, deletion_results
        )

        extra_msg_ids = []
        for res in deletion_results:
            if res.get('msg_ids_to_delete'):
                extra_msg_ids.extend(res['msg_ids_to_delete'])

        if extra_msg_ids:
            msgs_by_group = defaultdict(set)
            for global_msg_id in extra_msg_ids:
                g_idx, raw_id = utils.decode_global_msg_id(global_msg_id)
                target_gid = self.shared_state.get_telegram_group_id(g_idx)
                msgs_by_group[target_gid].add(raw_id)

            for target_gid, raw_ids in msgs_by_group.items():
                raw_list = list(raw_ids)
                logger.info(f"{log_prefix}: deleting {len(raw_list)} extra chunks (previews/thumbs) from Telegram group {target_gid}.")
                for i in range(0, len(raw_list), 100):
                    await client.delete_messages(target_gid, raw_list[i:i + 100])

        await self._invalidate_caches_cb(deletion_results)

    async def delete_items_permanently(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Permanently delete selected items from DB, Telegram storage, and preview caches."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        if not self.shared_state.metadata_manager:
            logger.error("MetadataManager is not initialized.")
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

        try:
            def _sync_delete():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                deletion_results = []
                with db.batch_write():
                    for item in items:
                        item_id, item_type = item['id'], item['type']
                        if item_type == 'folder':
                            deletion_results.extend(trash_repo.remove_folder(item_id))
                        else:
                            res = trash_repo.remove_file(item_id)
                            if res:
                                deletion_results.append(res)
                logger.info(f"Marked {len(items)} items for permanent deletion.")
                return deletion_results

            deletion_results = await asyncio.to_thread(_sync_delete)
            await self._finalize_permanent_deletion(client, deletion_results, "Permanent delete")
            return {"success": True}
        except errors.PathNotFoundError as e:
            logger.warning(f"Failed to delete item: {e}")
            return {"success": False, "error_code": errors.ErrorCode.PATH_NOT_FOUND}
        except telethon_errors.FloodWaitError as e:
            logger.warning(f"Delete operation hit a flood wait for {e.seconds} seconds.")
            return {"success": False, "error_code": errors.ErrorCode.FLOOD_WAIT_ERROR}
        except Exception as e:
            logger.error(f"An unknown error occurred while deleting {len(items)} items", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def empty_trash(self) -> Dict[str, Any]:
        """Permanently remove every item currently in the Recycle Bin."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            logger.error("Connection failed. Please check network or log in again.")
            return {"success": False, "error_code": errors.ErrorCode.CONNECTION_FAILED}

        if not self.shared_state.metadata_manager:
            logger.error("MetadataManager is not initialized.")
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

        try:
            def _sync_empty():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                trash_repo = TrashRepository(db)
                with db.batch_write():
                    return trash_repo.empty_trash()

            deletion_results = await asyncio.to_thread(_sync_empty)
            await self._finalize_permanent_deletion(client, deletion_results, "Emptying trash")
            return {"success": True}
        except Exception as e:
            logger.error(f"Error emptying trash: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.INTERNAL_ERROR}

    async def get_trash_items(self) -> Dict[str, Any]:
        """Fetch trashed files and folders for UI rendering."""
        try:
            def _sync_get():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                return TrashRepository(db).get_trashed_items()

            return await asyncio.to_thread(_sync_get)
        except Exception as e:
            logger.error(f"Error fetching trash items: {e}", exc_info=True)
            return {"success": False, "error_code": errors.ErrorCode.DB_READ_FAILED}

    async def cleanup_expired_trash(self) -> None:
        """Delete items whose Recycle Bin retention window has expired."""
        logger.info("Starting expired trash cleanup...")
        try:
            if not self.shared_state.client or not self.shared_state.client.is_connected():
                logger.warning("Skipping trash cleanup: Client not connected.")
                return

            def _get_expired():
                db = DatabaseConnection()
                FileRepository(db)
                FolderRepository(db)
                return TrashRepository(db).get_expired_items()

            expired_items = await self.shared_state.loop.run_in_executor(None, _get_expired)
            if expired_items:
                logger.info(f"Found {len(expired_items)} expired items. Deleting permanently...")
                await self.delete_items_permanently(expired_items)
                logger.info("Expired trash cleanup complete.")
            else:
                logger.info("No expired trash items found.")
        except Exception as e:
            logger.error(f"Error during expired trash cleanup: {e}", exc_info=True)
