import os
import logging
import asyncio
from PySide6.QtCore import QObject, Slot, Signal, QEventLoop as QtEventLoop
from .main_service import TDriveService
from .gui_utils import core_select_files, core_select_directory

logger = logging.getLogger(__name__)

class Bridge(QObject):
    """
    負責作為 Python 後端與 QWebEngineView 中 JavaScript 之間通訊的橋樑。
    """
    login_event = Signal(dict)
    transfer_progress_updated = Signal(dict)
    connection_status_changed = Signal(str)

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop, parent=None):
        super().__init__(parent)
        self._service = tdrive_service
        self._loop = loop

    def _wait_for_async(self, coro):
        """
        關鍵修復：在單一執行緒中同步等待非同步任務。
        使用局部的 QEventLoop 來防止 UI 死鎖。
        """
        if not self._loop.is_running():
            # 如果主迴圈沒在跑，嘗試直接運行 (主要用於測試或啟動時)
            return self._loop.run_until_complete(coro)

        # 1. 將協程包裝成 Task
        task = asyncio.create_task(coro)
        
        # 2. 建立一個局部的 Qt Event Loop
        local_qt_loop = QtEventLoop()

        # 3. 當 Task 完成時，退出局部迴圈
        def on_done(future):
            local_qt_loop.quit()
        
        task.add_done_callback(on_done)
        
        # 4. 開始「原地空轉」，這會阻塞此函式往下執行，
        # 但會持續處理 Qt 事件 (包含 asyncio 的事件)，避免死鎖
        local_qt_loop.exec()

        # 5. 返回結果或拋出異常
        return task.result()

    def _async_call(self, coro):
        """包裝器：處理錯誤與呼叫 _wait_for_async"""
        try:
            return self._wait_for_async(coro)
        except Exception as e:
            logger.error(f"非同步呼叫時發生錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "ASYNC_CALL_FAILED", "message": str(e)}

    # --- 原生對話方塊 (同步) ---
    @Slot(bool, str, result=list)
    def select_files(self, multiple=False, title="選取檔案"):
        return core_select_files(multiple, title, None)

    @Slot(str, result=str)
    def select_directory(self, title="選取資料夾"):
        return core_select_directory(title, None)

    @Slot(result=str)
    def get_os_sep(self):
        return os.sep

    # --- 認證服務 (非同步) ---
    @Slot(int, str, result=dict)
    def verify_api_credentials(self, api_id, api_hash):
        return self._async_call(self._service.verify_api_credentials(api_id, api_hash))
    
    @Slot(result=dict)
    def start_qr_login(self):
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
    def logout(self):
        return self._async_call(self._service.logout())

    # --- 資料夾與檔案服務 (同步) ---
    @Slot(result=list)
    def get_folder_tree_data(self):
        return self._service.get_folder_tree_data()

    @Slot(int, result=dict)
    def get_folder_contents(self, folder_id):
        return self._service.get_folder_contents(folder_id)

    @Slot(int, result=dict)
    def get_folder_contents_recursive(self, folder_id):
        return self._service.get_folder_contents_recursive(folder_id)

    @Slot(int, str, result=dict)
    def search_db_items(self, base_folder_id, search_term):
        return self._service.search_db_items(base_folder_id, search_term)

    # --- 資料夾與檔案服務 (非同步) ---
    @Slot(int, str, result=dict)
    def create_folder(self, parent_id, folder_name):
        return self._async_call(self._service.create_folder(parent_id, folder_name))

    @Slot(int, str, str, result=dict)
    def rename_item(self, item_id, new_name, item_type):
        return self._async_call(self._service.rename_item(item_id, new_name, item_type))

    @Slot(list, result=dict)
    def delete_items(self, items):
        return self._async_call(self._service.delete_items(items))

    # --- 傳輸服務 (Fire and Forget - 同步呼叫) ---
    @Slot(int, list, int, result=dict)
    def upload_files(self, parent_id, local_paths, concurrency_limit):
        return self._service.upload_files(parent_id, local_paths, concurrency_limit, self.transfer_progress_updated.emit)

    @Slot(list, str, int, result=dict)
    def download_items(self, items, destination_dir, concurrency_limit):
        return self._service.download_items(items, destination_dir, concurrency_limit, self.transfer_progress_updated.emit)

    @Slot(str, result=dict)
    def cancel_transfer(self, task_id):
        return self._service.cancel_transfer(task_id)
