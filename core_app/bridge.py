import os
import logging
import asyncio
from PySide6.QtCore import QObject, Slot, Signal, QEventLoop as QtEventLoop
from .main_service import TDriveService
from .ui.gui_utils import core_select_files, core_select_directory, reveal_in_explorer
from core_app.services.utils import check_path_exists

logger = logging.getLogger(__name__)

class Bridge(QObject):
    # --- UI Update Signals ---
    folderContentsReady = Signal(dict)
    searchResultsReady = Signal(dict)
    
    # --- Authentication and State Signals ---
    login_event = Signal(dict)
    transfer_progress_updated = Signal(dict)
    file_status_changed = Signal(list)
    connection_status_changed = Signal(str)
    login_and_initialization_complete = Signal()

    # --- Window Dragging Signals ---
    drag_window = Signal(int, int)
    drag_start = Signal(int, int)
    drag_end = Signal()
    
    # --- Window Action Signals ---
    window_action = Signal(str)

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop, parent=None):
        super().__init__(parent)
        self._service = tdrive_service
        self._loop = loop
        self._is_busy = False # A simple mutex to prevent re-entrant critical async operations.
        
        # Connect Watcher Signal
        self._service._transfer_service.set_file_status_callback(self.file_status_changed.emit)
        
        logger.debug("Bridge initialized.")

    def _run_background_task(self, coro, result_signal, request_id):
        """Executes a coroutine in background and emits result via signal."""
        async def task_wrapper():
            try:
                result = await coro
                payload = {'data': result, 'request_id': request_id}
                result_signal.emit(payload)
            except Exception as e:
                logger.error(f"Background task failed (request_id: {request_id}): {e}", exc_info=True)
                error_payload = {
                    'data': {"success": False, "error_code": "TASK_FAILED", "message": str(e)},
                    'request_id': request_id
                }
                result_signal.emit(error_payload)
        
        asyncio.create_task(task_wrapper())

    def _wait_for_async(self, coro):
        """Synchronously waits for an async coroutine using a local event loop."""
        if self._is_busy:
            logger.warning("BUSY: A critical operation was rejected because a previous one is still in progress.")
            return {"success": False, "error_code": "BUSY", "message": "另一個關鍵操作正在進行中，請稍候。"}

        # If the main asyncio loop isn't running, we can run the coroutine directly.
        if not self._loop.is_running():
            return self._loop.run_until_complete(coro)

        self._is_busy = True
        try:
            task = asyncio.create_task(coro)
            local_qt_loop = QtEventLoop()

            def on_done(future):
                # This callback will run in the asyncio thread, quitting the local Qt loop.
                local_qt_loop.quit()
            
            task.add_done_callback(on_done)
            # This executes the local loop, blocking until quit() is called.
            local_qt_loop.exec()

            return task.result()
        finally:
            self._is_busy = False # Always release the lock

    def _async_call(self, coro):
        try:
            return self._wait_for_async(coro)
        except Exception as e:
            logger.error(f"Error during async call: {e}", exc_info=True)
            return {"success": False, "error_code": "ASYNC_CALL_FAILED", "message": str(e)}

    # --- Window Control Slots ---
    @Slot()
    def minimize_window(self):
        self.window_action.emit("minimize")

    @Slot()
    def close_window(self):
        self.window_action.emit("close")

    @Slot(int, int)
    def handle_drag_start(self, global_x, global_y):
        self.drag_start.emit(global_x, global_y)

    @Slot(int, int)
    def handle_drag_move(self, global_x, global_y):
        self.drag_window.emit(global_x, global_y)

    @Slot()
    def handle_drag_end(self):
        self.drag_end.emit()

    # --- Native Dialog Slots ---
    @Slot(bool, str, result=list)
    def select_files(self, multiple=False, title="選擇檔案"):
        return core_select_files(multiple, title, None)

    @Slot(str, result=str)
    def select_directory(self, title="選擇資料夾"):
        return core_select_directory(title, None)

    @Slot(result=str)
    def get_os_sep(self):
        return os.sep

    @Slot(str, result=bool)
    def show_item_in_folder(self, path):
        return reveal_in_explorer(path)

    @Slot(str, result=bool)
    def check_local_exists(self, path):
        return check_path_exists(path)

    # --- Authentication Service Slots ---
    @Slot(int, str, result=dict)
    def verify_api_credentials(self, api_id, api_hash):
        return self._async_call(self._service.verify_api_credentials(api_id, api_hash))
    
    @Slot(result=dict)
    def start_qr_login(self):
        # The login_event signal is passed as a callback for real-time updates
        return self._async_call(self._service.start_qr_login(self.login_event.emit))

    @Slot(str, result=dict)
    def send_code_request(self, phone_number):
        return self._async_call(self._service.send_code_request(phone_number))

    @Slot(str, result=dict)
    def submit_verification_code(self, code):
        return self._async_call(self._service.submit_verification_code(code))

    @Slot(str, result=dict)
    def submit_password(self, password):
        return self._async_call(self._service.submit_password(password))
        
    @Slot(result=dict)
    def perform_post_login_initialization(self):
        return self._async_call(self._service.perform_post_login_initialization())

    @Slot(result=dict)
    def get_user_info(self):
        return self._async_call(self._service.get_user_info())

    @Slot(result=dict)
    def get_user_avatar(self):
        return self._async_call(self._service.get_user_avatar())

    @Slot(result=dict)
    def reset_client_for_new_login_method(self):
        return self._async_call(self._service.reset_client_for_new_login_method())

    @Slot(result=dict)
    def logout(self):
        return self._async_call(self._service.logout())

    @Slot()
    def notify_login_complete(self):
        logger.info("Frontend has confirmed login completion. Emitting signal to switch window.")
        self.login_and_initialization_complete.emit()

    @Slot(int, result=dict)
    def get_thumbnails(self, folder_id):
        return self._async_call(self._service._file_service.get_thumbnails(folder_id))

    @Slot(int, result=dict)
    def get_preview(self, file_id):
        return self._async_call(self._service._file_service.get_preview(file_id))

    # --- File and Folder Service Slots (Event-driven) ---
    @Slot(int, str)
    def get_folder_contents(self, folder_id, request_id):
        coro = self._service.get_folder_contents(folder_id)
        self._run_background_task(coro, self.folderContentsReady, request_id)

    @Slot(int, str, str)
    def search_db_items(self, base_folder_id, search_term, request_id):
        emitter = self.searchResultsReady.emit
        coro = self._service.search_db_items(base_folder_id, search_term, emitter, request_id)
        asyncio.create_task(coro)

    # --- File and Folder Service Slots (Async with return) ---
    @Slot(result=list)
    def get_folder_tree_data(self):
        return self._service.get_folder_tree_data()

    @Slot(int, result=dict)
    def get_folder_contents_recursive(self, folder_id):
        return self._async_call(self._service.get_folder_contents_recursive(folder_id))

    @Slot(int, str, result=dict)
    def create_folder(self, parent_id, folder_name):
        return self._async_call(self._service.create_folder(parent_id, folder_name))

    @Slot(int, str, str, result=dict)
    def rename_item(self, item_id, new_name, item_type):
        return self._async_call(self._service.rename_item(item_id, new_name, item_type))

    @Slot(list, result=dict)
    def delete_items(self, items):
        return self._async_call(self._service.delete_items(items))

    @Slot(list, result=dict)
    def restore_items(self, items):
        return self._async_call(self._service.restore_items(items))

    @Slot(list, result=dict)
    def delete_items_permanently(self, items):
        return self._async_call(self._service.delete_items_permanently(items))

    @Slot(result=dict)
    def empty_trash(self):
        return self._async_call(self._service.empty_trash())

    @Slot(result=dict)
    def get_trash_items(self):
        return self._async_call(self._service.get_trash_items())

    @Slot(list, int, result=dict)
    def move_items(self, items, target_folder_id):
        return self._async_call(self._service.move_items(items, target_folder_id))

    # --- Transfer Service Slots ---
    @Slot(int, list, result=dict)
    def upload_files(self, parent_id, local_paths):
        return self._service.upload_files(parent_id, local_paths, self.transfer_progress_updated.emit)

    @Slot(int, str, str, result=dict)
    def upload_folder(self, parent_id, folder_path, task_id):
        return self._service.upload_folder(parent_id, folder_path, task_id, self.transfer_progress_updated.emit)

    @Slot(list, str, result=dict)
    def download_items(self, items, destination_dir):
        return self._service.download_items(items, destination_dir, self.transfer_progress_updated.emit)

    @Slot(str, result=dict)
    def cancel_transfer(self, task_id):
        async def _do_cancel():
            try:
                self._service.cancel_transfer(task_id)
            except Exception as e:
                logger.error(f"Error cancelling task {task_id}: {e}")

        asyncio.create_task(_do_cancel())
        return {"success": True, "message": "已排程取消請求。"}

    @Slot(str, result=dict)
    def pause_transfer(self, task_id):
        async def _do_pause():
            try:
                self._service.pause_transfer(task_id, self.transfer_progress_updated.emit)
            except Exception as e:
                logger.error(f"Error pausing task {task_id}: {e}")

        asyncio.create_task(_do_pause())
        return {"success": True, "message": "已排程暫停請求。"}

    @Slot(str, result=dict)
    def resume_transfer(self, task_id):
        async def _do_resume():
            try:
                self._service.resume_transfer(task_id, self.transfer_progress_updated.emit)
            except Exception as e:
                 logger.error(f"Error resuming task {task_id}: {e}")

        asyncio.create_task(_do_resume())
        return {"success": True, "message": "已排程恢復請求。"}

    @Slot(str, result=dict)
    def remove_transfer_history(self, task_id):
        return self._service.remove_transfer_history(task_id)

    @Slot(result=dict)
    def get_incomplete_transfers(self):
        return self._service.get_incomplete_transfers()

    @Slot(result=dict)
    def get_all_file_statuses(self):
        return self._service._transfer_service.watcher.get_all_statuses()

    @Slot(result=dict)
    def get_initial_stats(self):
        return self._service.get_transfer_config()