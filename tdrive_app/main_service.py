import logging
import asyncio
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
    這是應用程式 UI 層與後端業務邏輯之間的核心介面。
    它聚合了所有子服務，並將它們的功能統一暴露給上層 (例如 Bridge)。
    """
    def __init__(self, loop: asyncio.AbstractEventLoop = None):
        logger.info("正在初始化 TDriveService...")
        self._shared_state = SharedState()
        
        # 強制使用傳入的 loop，如果沒傳入才嘗試自動獲取
        if loop:
            self._shared_state.loop = loop
            logger.info(f"TDriveService 使用傳入的事件迴圈: {loop}")
        else:
            try:
                self._shared_state.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("未提供 loop 且當前無運行中迴圈，將建立新迴圈 (這可能導致任務卡住！)。")
                self._shared_state.loop = asyncio.new_event_loop()

        self._auth_service = AuthService(self._shared_state)
        self._file_service = FileService(self._shared_state)
        self._folder_service = FolderService(self._shared_state)
        self._transfer_service = TransferService(self._shared_state)

        logger.info("TDriveService 初始化完成。")

    def _create_progress_adapter(self, signal_emitter):
        """
        建立一個轉接函式，將後端的 6 個參數轉換為前端需要的 1 個字典。
        """
        def adapter(task_id, name, progress, total, status, speed, message=None):
            data = {
                "id": task_id,
                "name": name,
                "progress": progress,
                "size": total,
                "status": status,
                "speed": speed
            }
            if message:
                data["message"] = message
            
            # 將打包好的字典發送給 Bridge (Qt Signal)
            signal_emitter(data)
        return adapter

    # --- 關鍵修復：背景任務管理 ---
    def _schedule_background_task(self, coro):
        """
        建立並排程背景任務，同時保留其引用以防止被 Python 垃圾回收 (GC)。
        當任務完成時，自動移除引用。
        """
        def _task_wrapper():
            # 1. 建立任務
            task = self._shared_state.loop.create_task(coro)
            
            # 2. 生成唯一 ID (使用記憶體位址即可)
            task_id = f"bg_task_{id(task)}"
            
            # 3. 將任務存入 shared_state.active_tasks 以保持強引用
            self._shared_state.active_tasks[task_id] = task
            
            # 4. 設定回呼：當任務結束時，從字典中移除引用
            def _on_done(_):
                self._shared_state.active_tasks.pop(task_id, None)
                
            task.add_done_callback(_on_done)

        # 確保在事件迴圈的執行緒中操作
        self._shared_state.loop.call_soon_threadsafe(_task_wrapper)

    # --- 啟動與狀態 ---
    async def check_startup_login(self) -> bool:
        return await self._auth_service.check_startup_login()

    async def close(self):
        client = self._shared_state.client
        if client and client.is_connected():
            logger.info("正在斷開與 Telegram 的連接...")
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"斷開 Telegram 連接時發生錯誤: {e}", exc_info=True)

        # 在 qasync 環境下，不要手動 stop loop，讓 app.exec() 自然結束
        logger.info("TDriveService 已關閉。")


    # --- 認證 (AuthService) ---
    async def verify_api_credentials(self, api_id: int, api_hash: str) -> Dict[str, Any]:
        return await self._auth_service.verify_api_credentials(api_id, api_hash)

    async def start_qr_login(self, event_callback) -> Dict[str, Any]:
        return await self._auth_service.start_qr_login(event_callback)

    async def send_code_request(self, phone_number: str) -> Dict[str, Any]:
        return await self._auth_service.send_code_request(phone_number)

    async def submit_verification_code(self, code: str) -> Dict[str, Any]:
        return await self._auth_service.submit_verification_code(code)

    async def submit_password(self, password: str) -> Dict[str, Any]:
        return await self._auth_service.submit_password(password)
        
    async def perform_post_login_initialization(self) -> Dict[str, Any]:
        return await self._auth_service.initialize_drive()

    async def get_user_info(self) -> Dict[str, Any]:
        return await self._auth_service.get_user_info()

    async def get_user_avatar(self) -> Dict[str, Any]:
        return await self._auth_service.get_user_avatar()

    async def logout(self) -> Dict[str, Any]:
        return await self._auth_service.perform_logout()

    # --- 資料夾 (FolderService) - 同步 ---
    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        return self._folder_service.get_folder_tree_data()

    # --- 檔案 (FileService) - 非同步讀取 ---
    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        return await self._file_service.get_folder_contents(folder_id)

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        return await self._file_service.get_folder_contents_recursive(folder_id)

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter, request_id: str):
        await self._file_service.search_db_items(base_folder_id, search_term, result_signal_emitter, request_id)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        return await self._file_service.create_folder(parent_id, folder_name)

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        return await self._file_service.rename_item(item_id, new_name, item_type)

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._file_service.delete_items(items)

    # --- 傳輸 (TransferService) - Fire and Forget ---
    def upload_files(self, parent_id: int, local_paths: List[Dict], concurrency_limit: int, progress_callback: Any) -> Dict[str, Any]:
        # 建立轉接器：把原本的 Signal Emit 包裝起來
        adapter_callback = self._create_progress_adapter(progress_callback)
        
        # 傳入 adapter_callback 給底層服務
        task_coro = self._transfer_service.upload_files(parent_id, local_paths, concurrency_limit, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "上傳任務已啟動。"}

    def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Any) -> Dict[str, Any]:
        # 建立轉接器
        adapter_callback = self._create_progress_adapter(progress_callback)
        
        # 傳入 adapter_callback 給底層服務
        task_coro = self._transfer_service.download_items(items, destination_dir, concurrency_limit, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "下載任務已啟動。"}

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.cancel_transfer(task_id)