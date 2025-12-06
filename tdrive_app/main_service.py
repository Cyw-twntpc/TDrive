import logging
import asyncio
import threading
import eel
import types # 匯入 types 模組
from typing import Dict, Any, List

from .shared_state import SharedState
from .services.auth_service import AuthService
from .services.file_service import FileService

from .services.folder_service import FolderService
from .services.transfer_service import TransferService

logger = logging.getLogger(__name__)

class TDriveService:
    """
    TDrive 的主服務門面 (Facade)。
    這是 UI 層 (eel) 與後端業務邏輯之間的唯一介面。
    它聚合了所有子服務，並將它們的功能統一暴露給前端。
    """
    def __init__(self):
        logger.info("正在初始化 TDriveService...")
        self._shared_state = SharedState()
        self._auth_service = AuthService(self._shared_state)
        self._file_service = FileService(self._shared_state)
        self._folder_service = FolderService(self._shared_state)
        self._transfer_service = TransferService(self._shared_state)
        
        # 將 eel 實例存入共享狀態，以便後端回呼
        self._shared_state.eel_instance = eel

        # 設定並啟動非同步事件迴圈
        self._start_async_loop()
        logger.info("TDriveService 初始化完成。")

    def _start_async_loop(self):
        """在背景執行緒中啟動並執行 asyncio 事件迴圈。"""
        def run_loop():
            asyncio.set_event_loop(self._shared_state.loop)
            self._shared_state.loop.run_forever()
        
        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

    def _async_call(self, coro):
        """安全地在背景事件迴圈中執行協程。"""
        if not self._shared_state.loop.is_running():
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._shared_state.loop)
        return future.result()

    # --- 啟動與狀態 ---
    def check_startup_login(self) -> bool:
        """供 gui_main 呼叫以檢查啟動時的登入狀態。"""
        return self._async_call(self._auth_service.check_startup_login())

    def close(self):
        """關閉服務，清理資源。"""
        client = self._shared_state.client
        if client and client.is_connected():
            logger.info("正在斷開與 Telegram 的連接...")
            disconnect_coro = client.disconnect()
            if asyncio.iscoroutine(disconnect_coro) or isinstance(disconnect_coro, types.CoroutineType):
                try:
                    # [FIXED] 將斷開連接作為背景任務排程，不阻塞主執行緒
                    self._shared_state.loop.call_soon_threadsafe(asyncio.create_task, disconnect_coro)
                except Exception as e:
                    logger.error(f"排程斷開 Telegram 連接時發生錯誤: {e}", exc_info=True)
            else:
                logger.error("client.disconnect() 沒有返回一個有效的協程對象，無法斷開連接。")

        if self._shared_state.loop.is_running():
            self._shared_state.loop.call_soon_threadsafe(self._shared_state.loop.stop)
        logger.info("TDriveService 已關閉。")


    # --- 認證 (AuthService) ---
    def verify_api_credentials(self, api_id: int, api_hash: str) -> Dict[str, Any]:
        return self._async_call(self._auth_service.verify_api_credentials(api_id, api_hash))

    def start_qr_login(self) -> Dict[str, Any]:
        return self._async_call(self._auth_service.start_qr_login())

    def send_code_request(self, phone_number: str) -> Dict[str, Any]:
        return self._async_call(self._auth_service.send_code_request(phone_number))

    def submit_verification_code(self, code: str) -> Dict[str, Any]:
        return self._async_call(self._auth_service.submit_verification_code(code))

    def submit_password(self, password: str) -> Dict[str, Any]:
        return self._async_call(self._auth_service.submit_password(password))
        
    def perform_post_login_initialization(self) -> Dict[str, Any]:
        return self._async_call(self._auth_service.initialize_drive())

    def get_user_info(self) -> Dict[str, Any]:
        return self._async_call(self._auth_service.get_user_info())

    def get_user_avatar(self) -> Dict[str, Any]:
        return self._async_call(self._auth_service.get_user_avatar())

    def logout(self) -> Dict[str, Any]:
        return self._async_call(self._auth_service.perform_logout())

    # --- 資料夾 (FolderService) ---
    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        return self._folder_service.get_folder_tree_data()

    # --- 檔案 (FileService) ---
    def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        return self._file_service.get_folder_contents(folder_id)

    def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        return self._file_service.get_folder_contents_recursive(folder_id)

    def search_db_items(self, base_folder_id: int, search_term: str) -> Dict[str, Any]:
        return self._file_service.search_db_items(base_folder_id, search_term)

    def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        return self._async_call(self._file_service.create_folder(parent_id, folder_name))

    def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        return self._async_call(self._file_service.rename_item(item_id, new_name, item_type))

    def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._async_call(self._file_service.delete_items(items))

    # --- 傳輸 (TransferService) ---
    def upload_files(self, parent_id: int, local_paths: List[str], concurrency_limit: int, progress_callback: Any) -> Dict[str, Any]:
        """在背景執行緒中啟動上傳任務。"""
        task_coro = self._transfer_service.upload_files(parent_id, local_paths, concurrency_limit, progress_callback)
        # 將協程提交到事件迴圈，但不等待結果
        self._shared_state.loop.call_soon_threadsafe(lambda: asyncio.create_task(task_coro))
        return {"success": True, "message": "上傳任務已啟動。"}

    def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Any) -> Dict[str, Any]:
        """在背景執行緒中啟動下載任務。"""
        task_coro = self._transfer_service.download_items(items, destination_dir, concurrency_limit, progress_callback)
        self._shared_state.loop.call_soon_threadsafe(lambda: asyncio.create_task(task_coro))
        return {"success": True, "message": "下載任務已啟動。"}

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.cancel_transfer(task_id)
