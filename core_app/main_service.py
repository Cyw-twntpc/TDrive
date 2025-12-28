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
    Refactored to include a progress adapter for the new TransferService logic.
    """
    def __init__(self, loop: asyncio.AbstractEventLoop = None):
        logger.info("Initializing TDriveService...")
        self._shared_state = SharedState()
        
        if loop:
            self._shared_state.loop = loop
            logger.info(f"TDriveService is using the provided event loop: {loop}")
        else:
            try:
                self._shared_state.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("No running event loop found. Creating a new one.")
                self._shared_state.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._shared_state.loop)

        # Initialize sub-services
        self._auth_service = AuthService(self._shared_state)
        self._file_service = FileService(self._shared_state)
        self._folder_service = FolderService(self._shared_state)
        self._monitor_service = TransferMonitorService()
        self._transfer_service = TransferService(self._shared_state, self._monitor_service)

    # --- Helper: Progress Adapter ---
    
    def _create_progress_adapter(self, bridge_emit_signal: Callable):
        """
        Creates an adapter function that normalizes the variable arguments from 
        TransferService callbacks into a standard dictionary for the Bridge signal.
        
        Handles two types of calls from TransferService:
        1. Initialization/Status Change: (task_id, name, transferred, total, status, speed, is_folder=False, message=None)
        2. Delta Update: (task_id, delta_bytes, speed)
        
        [Updated] Includes throttling (30ms) to prevent UI thread flooding.
        """
        last_emit_time = {}

        def adapter(*args, **kwargs):
            current_time = time.time()
            data = {}
            should_emit = True
            task_id = args[0]

            # Type 1: Initialization or Full Status Update
            # args: (task_id, name, transferred, total, status, speed)
            if len(args) >= 5: 
                status = args[4]
                # Always emit significant status changes
                if status in ['completed', 'failed', 'cancelled', 'queued', 'paused']:
                    should_emit = True
                    if status in ['completed', 'failed', 'cancelled']:
                        last_emit_time.pop(task_id, None) # Cleanup
                else:
                    # Throttle 'transferring' updates if they come as full state
                    if task_id in last_emit_time and (current_time - last_emit_time[task_id] < 0.03):
                        should_emit = False
                
                if should_emit:
                    data = {
                        "id": args[0],
                        "name": args[1],
                        "transferred": args[2],
                        "total": args[3],
                        "status": args[4],
                        "speed": args[5],
                        "is_folder": kwargs.get("is_folder", False),
                        "error_message": kwargs.get("message", ""),
                        "todayTraffic": self._monitor_service.get_today_traffic()
                    }

            # Type 2: Delta Update (Progress)
            # args: (task_id, delta_bytes, speed)
            elif len(args) == 3:
                # Throttle delta updates
                if task_id in last_emit_time and (current_time - last_emit_time[task_id] < 0.03):
                    should_emit = False
                else:
                    data = {
                        "id": args[0],
                        "delta": args[1],     # Bytes transferred since last update
                        "speed": args[2],     # Current speed
                        "status": "transferring",
                        "todayTraffic": self._monitor_service.get_today_traffic()
                    }
            
            if should_emit and data:
                last_emit_time[task_id] = current_time
                bridge_emit_signal(data)
        
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
    def get_transfer_config(self) -> Dict[str, Any]:
        return self._transfer_service.get_transfer_config()

    def upload_files(self, parent_id: int, files: List[Dict], progress_callback: Callable) -> Dict[str, Any]:
        """Initiates file uploads."""
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_files(parent_id, files, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Upload started"}

    def upload_folder(self, parent_id: int, folder_path: str, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        """Initiates folder upload."""
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_folder_recursive(parent_id, folder_path, task_id, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Folder upload started"}

    def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable) -> Dict[str, Any]:
        """Initiates item downloads."""
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.download_items(items, destination_dir, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Download started"}

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.cancel_transfer(task_id)

    def pause_transfer(self, task_id: str) -> Dict[str, Any]:
        self._transfer_service.pause_transfer(task_id)
        return {"success": True, "message": "Task paused."}

    def resume_transfer(self, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.resume_transfer(task_id, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True, "message": "Resuming transfer..."}

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        return self._transfer_service.controller.get_incomplete_transfers()

    def remove_transfer_history(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.remove_history_item(task_id)
