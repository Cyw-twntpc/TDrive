import logging
import asyncio
import time
from typing import Dict, Any, List, Callable

from .data.shared_state import SharedState
from .services.auth_service import AuthService
from .services.file_service import FileService
from .services.folder_service import FolderService
from .services.transfer_service import TransferService
from .services.monitor_service import TransferMonitorService

logger = logging.getLogger(__name__)

class TDriveService:
    """
    Acts as a Façade for all backend services, providing a single, unified
    interface for the UI layer (via the Bridge) to interact with.

    It aggregates all sub-services (auth, file, folder, transfer) and exposes
    their functionalities. It's also responsible for orchestrating calls that
    span multiple services.
    """
    def __init__(self, loop: asyncio.AbstractEventLoop = None):
        logger.info("Initializing TDriveService...")
        self._shared_state = SharedState()
        
        # The service must run on the same event loop as the Qt application.
        if loop:
            self._shared_state.loop = loop
            logger.info(f"TDriveService is using the provided event loop: {loop}")
        else:
            try:
                self._shared_state.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("No running event loop found and none was provided. "
                               "Creating a new one, but this may lead to issues in a threaded environment.")
                self._shared_state.loop = asyncio.new_event_loop()

        self._auth_service = AuthService(self._shared_state)
        self._file_service = FileService(self._shared_state)
        self._folder_service = FolderService(self._shared_state)
        self._transfer_service = TransferService(self._shared_state)
        self._transfer_service.monitor = TransferMonitorService()

        logger.info("TDriveService initialized successfully.")

    def _create_progress_adapter(self, signal_emitter: Callable) -> Callable:
        """
        Creates an adapter function to transform backend progress arguments
        into a dictionary format expected by the frontend.
        Also injects global traffic stats.
        """
        # 使用 closure 變數來記錄每個任務上一次發送訊號的時間
        last_emit_time = {} 
        
        def adapter(task_id, name, progress, total, status, speed, message=None, **kwargs):
            current_time = time.time()

            should_emit = (
                status != 'transferring' or 
                task_id not in last_emit_time or 
                (current_time - last_emit_time[task_id] >= 0.03)
            )

            if should_emit:
                last_emit_time[task_id] = current_time
                
                if status in ['completed', 'failed', 'cancelled']:
                    last_emit_time.pop(task_id, None)

                data = {
                    "id": task_id,
                    "name": name,
                    "progress": progress,
                    "size": total,
                    "status": status,
                    "speed": speed,
                    "todayTraffic": self._transfer_service.monitor.get_today_traffic()
                }
                if message:
                    data["message"] = message
                
                data.update(kwargs)
                signal_emitter(data)
                
        return adapter

    def _schedule_background_task(self, coro):
        """
        Schedules a coroutine to run as a background task.

        Crucially, it holds a strong reference to the created task in the
        shared state to prevent it from being garbage-collected prematurely.
        The reference is automatically removed once the task is complete.
        """
        def _task_wrapper():
            task = self._shared_state.loop.create_task(coro)
            task_id = f"bg_task_{id(task)}"
            self._shared_state.active_tasks[task_id] = task
            
            def _on_done(_):
                # Callback to remove the task from the active list upon completion.
                self._shared_state.active_tasks.pop(task_id, None)
                
            task.add_done_callback(_on_done)

        # Ensure the task is created in the correct event loop's thread.
        self._shared_state.loop.call_soon_threadsafe(_task_wrapper)

    # --- Application Lifecycle ---

    async def check_startup_login(self) -> Dict[str, Any]:
        return await self._auth_service.check_startup_login()

    async def close(self):
        """
        Gracefully shuts down the service, including disconnecting the client.
        """
        self._transfer_service.monitor.close()
        client = self._shared_state.client
        if client and client.is_connected():
            logger.info("Disconnecting from Telegram...")
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Error during Telegram client disconnection: {e}", exc_info=True)

        logger.info("TDriveService has been closed.")


    # --- Authentication Service ---
    async def verify_api_credentials(self, api_id: int, api_hash: str) -> Dict[str, Any]:
        return await self._auth_service.verify_api_credentials(api_id, api_hash)

    async def start_qr_login(self, event_callback: Callable) -> Dict[str, Any]:
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

    async def reset_client_for_new_login_method(self) -> Dict[str, bool]:
        return await self._auth_service.reset_client_for_new_login_method()

    # --- Folder Service ---
    def get_folder_tree_data(self) -> List[Dict[str, Any]]:
        return self._folder_service.get_folder_tree_data()

    # --- File Service ---
    async def get_folder_contents(self, folder_id: int) -> Dict[str, Any]:
        return await self._file_service.get_folder_contents(folder_id)

    async def get_folder_contents_recursive(self, folder_id: int) -> Dict[str, Any]:
        return await self._file_service.get_folder_contents_recursive(folder_id)

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter: Callable, request_id: str):
        await self._file_service.search_db_items(base_folder_id, search_term, result_signal_emitter, request_id)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        return await self._file_service.create_folder(parent_id, folder_name)

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        return await self._file_service.rename_item(item_id, new_name, item_type)

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._file_service.delete_items(items)

    async def move_items(self, items: List[Dict[str, Any]], target_folder_id: int) -> Dict[str, Any]:
        return await self._file_service.move_items(items, target_folder_id)

    # --- Transfer Service ---
    def get_today_traffic_stats(self) -> int:
        return self._transfer_service.monitor.get_today_traffic()

    def upload_files(self, parent_id: int, local_paths: List[Dict], concurrency_limit: int, progress_callback: Callable) -> Dict[str, Any]:
        """
        Initiates file uploads as background tasks.
        """
        adapter_callback = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_files(parent_id, local_paths, concurrency_limit, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Upload tasks have been started."}

    def upload_folder(self, parent_id: int, folder_path: str, concurrency_limit: int, progress_callback: Callable) -> Dict[str, Any]:
        """
        Initiates a recursive folder upload as a background task.
        """
        adapter_callback = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_folder_recursive(parent_id, folder_path, concurrency_limit, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Folder upload task started."}

    def download_items(self, items: List[Dict], destination_dir: str, concurrency_limit: int, progress_callback: Callable) -> Dict[str, Any]:
        """
        Initiates item downloads as background tasks.
        """
        adapter_callback = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.download_items(items, destination_dir, concurrency_limit, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Download tasks have been started."}

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        """Permanently cancels and removes a transfer."""
        return self._transfer_service.cancel_transfer(task_id)

    def pause_transfer(self, task_id: str) -> Dict[str, Any]:
        """Pauses a transfer without removing it from state."""
        self._transfer_service.pause_transfer(task_id)
        return {"success": True, "message": "Task paused."}

    def resume_transfer(self, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        """
        Resumes a paused or failed transfer.
        """
        adapter_callback = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.resume_transfer(task_id, adapter_callback)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Resuming transfer..."}

    def get_incomplete_transfers(self) -> Dict[str, Any]:
        """Retrieves list of paused/failed transfers for startup."""
        return self._transfer_service.controller.get_incomplete_transfers()