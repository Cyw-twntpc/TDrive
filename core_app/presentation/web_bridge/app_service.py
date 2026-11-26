import logging
import asyncio
import time
from typing import Dict, Any, List, Callable

from core_app.core.errors import ErrorCode
from core_app.core.shared_state import SharedState
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
from core_app.infrastructure.telegram.metadata_manager import MetadataManager

from core_app.application.auth.auth_service import AuthService
from core_app.application.file_system.file_service import FileService
from core_app.application.file_system.folder_service import FolderService
from core_app.application.transfer.transfer_service import TransferService
from core_app.application.file_system.thumbnail.manager import ThumbnailManager
from core_app.application.preview.buffer import StreamBuffer
from core_app.application.preview.video.player import VideoStreamer
from core_app.application.preview.audio.player import AudioPlayerManager
from core_app.application.preview.audio.playlist_manager import PlaylistManager
from core_app.application.auth.session_manager import SessionManager
from core_app.application.file_system.preview_cache_manager import PreviewCacheManager

logger = logging.getLogger(__name__)

class TDriveService:
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

        # Initialize Data Layer
        self.db_handler = DatabaseConnection()
        self.file_repo = FileRepository(self.db_handler)
        self.folder_repo = FolderRepository(self.db_handler)
        self.trash_repo = TrashRepository(self.db_handler)
        self.metadata_manager = MetadataManager(self.db_handler, self._shared_state)
        self._shared_state.metadata_manager = self.metadata_manager
        
        # Initialize Thumbnail Manager (Shared Instance)
        self.thumb_manager = ThumbnailManager()
        
        # Initialize Preview Cache before StreamBuffer so streaming shares the encrypted disk cache
        self._preview_cache = PreviewCacheManager()

        # Initialize Streaming Components
        self.buffer = StreamBuffer(self._shared_state, preview_cache=self._preview_cache)

        self.video_player = VideoStreamer(self.buffer, self.db_handler)
        self.audio_player = AudioPlayerManager()
        self._audio_play_seq: int = 0
        self.playlist_manager = PlaylistManager(
            db_handler=self.db_handler,
            account_scope_getter=lambda: getattr(self._shared_state, "api_id", None),
        )

        # Initialize sub-services
        self._auth_service = AuthService(self._shared_state)
        self._file_service = FileService(self._shared_state, self.thumb_manager, buffer=self.buffer, preview_cache=self._preview_cache)
        self._folder_service = FolderService(self._shared_state)
        
        # Inject MetadataManager into TransferService
        self._transfer_service = TransferService(self._shared_state, self.thumb_manager, self.metadata_manager)

    # --- Helper: Progress Adapter ---
    
    def _create_progress_adapter(self, bridge_emit_signal: Callable):
        """Normalizes TransferService callbacks for Bridge signals."""
        last_emit_time = {}
        last_status = {} # Track status changes to bypass throttle
        accumulated_deltas = {}

        def adapter(*args, **kwargs):
            current_time = time.time()
            data = {}
            should_emit = True
            task_id = args[0]

            # Type 1: Initialization or Full Status Update
            if len(args) >= 5: 
                status = args[4]
                
                # Check if status has changed
                status_changed = task_id not in last_status or last_status[task_id] != status
                
                if status in ['completed', 'failed', 'cancelled', 'queued', 'paused', 'transferring']:
                    # Force emit on status change or critical states
                    if status_changed:
                        should_emit = True
                    elif task_id in last_emit_time and (current_time - last_emit_time[task_id] < 0.03):
                        should_emit = False
                    
                    if status in ['completed', 'failed', 'cancelled']:
                        last_emit_time.pop(task_id, None)
                        last_status.pop(task_id, None)
                        accumulated_deltas.pop(task_id, None)
                    else:
                        last_status[task_id] = status
                else:
                    if not status_changed and task_id in last_emit_time and (current_time - last_emit_time[task_id] < 0.03):
                        should_emit = False
                
                if should_emit:
                    data = {
                        "id": args[0],
                        "name": args[1],
                        "status": args[4],
                        "speed": args[5] if len(args) > 5 else 0,
                        "is_folder": kwargs.get("is_folder", False),
                        "error_code": kwargs.get("error_code", ""),
                        "todayTraffic": self._transfer_service.controller.get_today_traffic()
                    }
                    
                    if args[2] >= 0:
                        data["transferred"] = args[2]
                    
                    if args[3] > 0:
                        data["total"] = args[3]

            # Type 2: Delta Update (Progress)
            elif len(args) == 3:
                delta = args[1]
                accumulated_deltas[task_id] = accumulated_deltas.get(task_id, 0) + delta
                
                if task_id in last_emit_time and (current_time - last_emit_time[task_id] < 0.03):
                    should_emit = False
                else:
                    data = {
                        "id": args[0],
                        "delta": accumulated_deltas[task_id],
                        "speed": args[2],
                        "todayTraffic": self._transfer_service.controller.get_today_traffic()
                    }
                    accumulated_deltas[task_id] = 0
            
            if should_emit and data:
                last_emit_time[task_id] = current_time
                bridge_emit_signal(data)
        
        return adapter

    def _schedule_background_task(self, coro):
        def _task_wrapper():
            task = self._shared_state.loop.create_task(coro)
            task_id = f"bg_task_{id(task)}"
            self._shared_state.active_tasks[task_id] = task
            self._shared_state.background_tasks.add(task)
            
            def _on_done(t):
                self._shared_state.active_tasks.pop(task_id, None)
                self._shared_state.background_tasks.discard(t)
                if not t.cancelled():
                    try:
                        exc = t.exception()
                        if exc:
                            logger.error(f"Background task failed: {exc}", exc_info=exc)
                    except (asyncio.CancelledError, RuntimeError):
                        pass
                
            task.add_done_callback(_on_done)

        if self._shared_state.loop.is_running():
            self._shared_state.loop.call_soon_threadsafe(_task_wrapper)
        else:
            task = self._shared_state.create_background_task(coro)
            task_id = f"bg_task_{id(task)}"
            self._shared_state.active_tasks[task_id] = task

    # --- Application Lifecycle ---

    async def check_startup_login(self) -> Dict[str, Any]:
        # initialize_drive() inside check_startup_login already calls initialize_db()
        return await self._auth_service.check_startup_login()

    MEMORY_CACHE_IDLE_TTL_SECONDS = 300.0
    MEMORY_CACHE_SWEEP_INTERVAL_SECONDS = 60.0

    def evict_idle_memory_caches(self, max_idle_seconds: float = MEMORY_CACHE_IDLE_TTL_SECONDS) -> int:
        """Evict idle entries from StreamBuffer, ImagePreviewer, and TextPreviewer memory caches."""
        evicted = 0
        if getattr(self, "buffer", None) and hasattr(self.buffer, "evict_idle"):
            evicted += self.buffer.evict_idle(max_idle_seconds)
        if getattr(self, "_file_service", None) and hasattr(self._file_service, "evict_idle_memory_caches"):
            evicted += self._file_service.evict_idle_memory_caches(max_idle_seconds)
        return evicted

    async def _memory_cache_cleanup_loop(self):
        """Periodically evict idle preview memory caches while the app stays open."""
        try:
            while True:
                await asyncio.sleep(self.MEMORY_CACHE_SWEEP_INTERVAL_SECONDS)
                evicted = self.evict_idle_memory_caches()
                if evicted > 0:
                    logger.debug("Evicted %d idle preview entries from memory caches", evicted)
        except asyncio.CancelledError:
            return

    async def start_background_services(self):
        """Starts background tasks safely decoupled from synchronous initialization phases."""
        # Schedule maintenance tasks
        self._schedule_background_task(self._file_service.cleanup_expired_trash())
        self._schedule_background_task(self._memory_cache_cleanup_loop())
        
        # --- Start Bot Token Pool provisioning in background ---
        try:
            from core_app.infrastructure.telegram.bot_manager import BotManager
            bot_manager = BotManager()
            
            async def setup_worker_bots():
                await bot_manager.init_saved_bots(self._shared_state)
                await bot_manager.recover_lost_bots(self._shared_state)
                await bot_manager.provision_missing_bots(self._shared_state, target_count=5)
            
            self._schedule_background_task(setup_worker_bots())
        except Exception as e:
            logger.error(f"Failed to start bot manager: {e}", exc_info=True)
            
    async def _disconnect_worker_bots(self):
        """Disconnect and clear all worker bot clients in the shared pool."""
        pool = getattr(self._shared_state, "clients_pool", None) if self._shared_state else None
        if not pool:
            return
        for bot_client in list(pool):
            try:
                if bot_client and bot_client.is_connected():
                    await bot_client.disconnect()
            except Exception as e:
                logger.warning(f"Failed to disconnect worker bot during shutdown: {e}")
        if isinstance(pool, list):
            pool.clear()

    async def close(self):
        """Gracefully stop background workers, timers, clients, and local storage."""
        try:
            running_loop = asyncio.get_running_loop()
            current_task = asyncio.current_task()
        except RuntimeError:
            running_loop = None
            current_task = None

        tasks_to_wait: list[asyncio.Future] = []

        def _collect_pending_task(task_obj):
            if (
                isinstance(task_obj, asyncio.Future)
                and task_obj is not current_task
                and not task_obj.done()
                and (running_loop is None or task_obj.get_loop() is running_loop)
                and task_obj not in tasks_to_wait
            ):
                tasks_to_wait.append(task_obj)

        if hasattr(self, "_file_service") and getattr(self._file_service, "defrag_worker", None):
            defrag_worker = self._file_service.defrag_worker
            _collect_pending_task(getattr(defrag_worker, "worker_task", None))
            _collect_pending_task(getattr(defrag_worker, "timer_task", None))
            defrag_worker.stop()

        if hasattr(self, "db_handler") and getattr(self.db_handler, "sync_manager", None):
            self.db_handler.sync_manager.stop()

        transfer_service = getattr(self, "_transfer_service", None)
        if transfer_service is not None:
            watcher = getattr(transfer_service, "watcher", None)
            if watcher is not None:
                _collect_pending_task(getattr(watcher, "_loop_task", None))

        if self._shared_state:
            if self._shared_state.db_upload_timer:
                self._shared_state.db_upload_timer.cancel()
                self._shared_state.db_upload_timer = None
            for task in list(getattr(self._shared_state, "background_tasks", []) or []):
                if task is not current_task and not task.done():
                    _collect_pending_task(task)
                    task.cancel()
            active_tasks = getattr(self._shared_state, "active_tasks", None)
            if isinstance(active_tasks, dict):
                for task in list(active_tasks.values()):
                    if task is not current_task and not task.done():
                        _collect_pending_task(task)
                        task.cancel()

        self._transfer_service.shutdown()

        # Drain cancelled background tasks before disconnecting Telegram clients
        # so in-flight setup tasks cannot spawn new connections during teardown.
        if tasks_to_wait:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_wait, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for background tasks to cancel during shutdown.")

        if getattr(self, "audio_player", None):
            await asyncio.to_thread(self.audio_player.cleanup)

        if getattr(self, "video_player", None):
            await self.video_player.stop()

        if getattr(self, "buffer", None) and hasattr(self.buffer, "clear"):
            self.buffer.clear()

        if getattr(self, "_file_service", None) and hasattr(self._file_service, "clear_memory_caches"):
            self._file_service.clear_memory_caches()

        preview_cache = getattr(self, "_preview_cache", None)
        if preview_cache and hasattr(preview_cache, "close"):
            preview_cache.close()

        thumb_mgr = getattr(self, "thumb_manager", None)
        if thumb_mgr and hasattr(thumb_mgr, "close"):
            thumb_mgr.close()

        self._transfer_service.controller.save_pending_traffic_stats()

        if self._shared_state and self._shared_state.client and self._shared_state.client.is_connected() and self._shared_state.is_logged_in:
            try:
                SessionManager.save_session(self._shared_state.client.session, self._shared_state.api_id)
                logger.info("Syncing database to cloud before shutdown...")
                await self.metadata_manager.sync_db_to_cloud(
                    self._shared_state.client,
                    self._shared_state.group_id,
                    self._shared_state.api_id
                )
            except Exception as e:
                logger.error(f"Failed to sync DB on exit: {e}")

        await self._disconnect_worker_bots()

        client = self._shared_state.client if self._shared_state else None
        if client and client.is_connected():
            logger.info("Disconnecting from Telegram...")
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Error during Telegram client disconnection: {e}", exc_info=True)

        logger.info("TDriveService has been closed.")

    @property
    def defrag_worker(self):
        return getattr(self._file_service, "defrag_worker", None)

    # --- Streaming Logic ---
    def _get_file_name_by_id(self, file_id: int, default_name: str = "Unknown Video") -> str:
        conn = self.db_handler._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name FROM file_folder_map WHERE id = ?", (file_id,))
        row = cur.fetchone()
        return row['name'] if row else default_name

    async def play_video(self, file_id: int) -> Dict[str, Any]:
        """
        Starts the streaming proxy (if needed) and returns the stream URL.
        """
        if getattr(self, "audio_player", None):
            state = self.audio_player.get_state().get("state")
            if state in ("playing", "buffering"):
                if hasattr(self.audio_player, "dispatch_control"):
                    self.audio_player.dispatch_control("pause", 0)
                else:
                    self.audio_player.control("pause")

        # Ensure proxy is running
        if not self.video_player.runner:
            await self.video_player.start()
            
        url = self.video_player.get_stream_url(file_id)
        if not url:
            logger.error("Failed to generate stream URL")
            return {"success": False}
            
        # Fetch file name
        loop = asyncio.get_running_loop()
        file_name = await loop.run_in_executor(None, self._get_file_name_by_id, file_id)
            
        return {"success": True, "stream_url": url, "file_name": file_name}

    async def play_audio(self, file_id: int) -> Dict[str, Any]:
        """Start streaming proxy if needed and begin headless VLC audio playback on its worker."""
        self._audio_play_seq += 1
        req_seq = self._audio_play_seq

        loop = asyncio.get_running_loop()
        file_info = await loop.run_in_executor(
            None,
            lambda: FileRepository(self.db_handler).get_active_stream_file_info(file_id),
        )
        if not file_info or int(file_info.get("size") or 0) <= 0:
            logger.warning("Audio track not found or in trash for file_id=%s", file_id)
            return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

        if not self.video_player.runner:
            await self.video_player.start()

        url = self.video_player.get_stream_url(file_id)
        if not url:
            logger.error("Failed to generate audio stream URL for file_id=%s", file_id)
            return {"success": False, "error_code": ErrorCode.INTERNAL_ERROR}

        file_name = file_info.get("name") or "Unknown Audio"
        # Ignore stale async setup if a newer track or stop superseded this request.
        if req_seq != self._audio_play_seq:
            return {
                "success": True,
                "stale": True,
                "file_id": file_id,
                "file_name": file_name,
                "stream_url": url,
                "state": self.audio_player.get_state(),
            }

        vlc_executor = getattr(self.audio_player, "vlc_executor", None)
        res = await loop.run_in_executor(
            vlc_executor,
            lambda: self.audio_player.play_url(file_id, url, file_name=file_name),
        )
        return {
            "success": bool(res.get("success")),
            "file_id": file_id,
            "file_name": file_name,
            "stream_url": url,
            "state": res.get("state", self.audio_player.get_state()),
        }

    def control_audio(self, action: str, value: int = 0) -> Dict[str, Any]:
        """Dispatch non-blocking audio transport/volume commands."""
        if action == "stop":
            # Invalidate any in-flight play_audio request before stopping playback.
            self._audio_play_seq += 1
        if hasattr(self.audio_player, "dispatch_control"):
            return self.audio_player.dispatch_control(action, value)
        return self.audio_player.control(action, value)

    async def preload_audio(self, file_id: int) -> None:
        """Warm the initial stream chunk in StreamBuffer for an upcoming active track."""
        try:
            row = await asyncio.to_thread(
                lambda: FileRepository(self.db_handler).get_active_stream_file_info(file_id)
            )
            if not row or not row.get("size") or int(row["size"]) <= 0:
                return
            size = int(row["size"])
            await self.buffer.read(
                int(row["content_id"]), 0, min(65536, size), size, row["hash"], category="audio_chunk"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Audio preload skipped for file_id=%s: %s", file_id, exc)

    def get_all_audio_files(self) -> Dict[str, Any]:
        """Return all non-trashed cloud audio files."""
        return self._file_service.get_all_audio_files_sync()

    def get_playlists(self) -> Dict[str, Any]:
        """Return summary metadata for all local playlists."""
        return {"success": True, "playlists": self.playlist_manager.get_playlists()}

    def get_playlist_items(self, playlist_id: str) -> Dict[str, Any]:
        """Return resolved tracks for a given local playlist."""
        return self.playlist_manager.get_playlist_items(playlist_id)

    def create_playlist(self, name: str) -> Dict[str, Any]:
        """Create a new local playlist."""
        return self.playlist_manager.create_playlist(name)

    def rename_playlist(self, playlist_id: str, new_name: str) -> Dict[str, Any]:
        """Rename a local playlist."""
        return self.playlist_manager.rename_playlist(playlist_id, new_name)

    def delete_playlist(self, playlist_id: str) -> Dict[str, Any]:
        """Delete a local playlist."""
        return self.playlist_manager.delete_playlist(playlist_id)

    def add_to_playlist(self, playlist_id: str, map_ids: list[int]) -> Dict[str, Any]:
        """Add cloud audio tracks by map ID to a local playlist."""
        return self.playlist_manager.add_items_to_playlist(playlist_id, map_ids)

    def remove_from_playlist(self, playlist_id: str, map_id: int) -> Dict[str, Any]:
        """Remove an audio track from a local playlist."""
        return self.playlist_manager.remove_item_from_playlist(playlist_id, map_id)

    def reorder_playlist_items(self, playlist_id: str, ordered_map_ids: list[int]) -> Dict[str, Any]:
        """Reorder tracks in a local playlist."""
        return self.playlist_manager.reorder_playlist_items(playlist_id, ordered_map_ids)

    # --- Preview Delegates ---
    async def get_preview_file(self, map_id: int) -> dict:
        return await self._file_service.get_preview_file(map_id)

    async def load_full_document(self, map_id: int) -> dict:
        return await self._file_service.load_full_document(map_id)

    async def get_preview_text(self, map_id: int) -> dict:
        return await self._file_service.get_preview_text(map_id)

    async def get_text_page(self, map_id: int, page_index: int) -> dict:
        return await self._file_service.get_text_page(map_id, page_index)

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
        # initialize_drive() already calls initialize_db() internally
        result = await self._auth_service.initialize_drive()
        
        if result.get("success"):
            self._schedule_background_task(self.start_background_services())
            
        return result

    async def get_user_info(self) -> Dict[str, Any]:
        return await self._auth_service.get_user_info()

    async def get_user_avatar(self) -> Dict[str, Any]:
        return await self._auth_service.get_user_avatar()

    async def logout(self) -> Dict[str, Any]:
        logger.info("Logging out, shutting down services...")
        if hasattr(self, '_file_service') and getattr(self._file_service, 'defrag_worker', None):
            self._file_service.defrag_worker.stop()
        if hasattr(self, 'db_handler') and getattr(self.db_handler, 'sync_manager', None):
            self.db_handler.sync_manager.stop()

        # Cancel timers and background tasks before disconnecting clients/removing cache
        if getattr(self, '_shared_state', None):
            if self._shared_state.db_upload_timer:
                self._shared_state.db_upload_timer.cancel()
                self._shared_state.db_upload_timer = None
            current_task = asyncio.current_task()
            for task in list(self._shared_state.background_tasks):
                if task is not current_task and not task.done():
                    task.cancel()

        self._transfer_service.shutdown()

        if getattr(self, 'audio_player', None):
            self.audio_player.cleanup()

        if getattr(self, 'video_player', None):
            await self.video_player.stop()

        if getattr(self, "buffer", None) and hasattr(self.buffer, "clear"):
            self.buffer.clear()

        if getattr(self, "_file_service", None) and hasattr(self._file_service, "clear_memory_caches"):
            self._file_service.clear_memory_caches()

        if getattr(self, "_preview_cache", None) and hasattr(self._preview_cache, "close"):
            self._preview_cache.close()

        if hasattr(self, 'thumb_manager') and hasattr(self.thumb_manager, 'close'):
            self.thumb_manager.close()
            
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

    async def get_file_extended_details(self, map_id: int) -> Dict[str, Any]:
        return await self._file_service.get_file_extended_details(map_id)

    async def search_db_items(self, base_folder_id: int, search_term: str, result_signal_emitter: Callable, request_id: str):
        await self._file_service.search_db_items(base_folder_id, search_term, result_signal_emitter, request_id)

    async def create_folder(self, parent_id: int, folder_name: str) -> Dict[str, Any]:
        return await self._file_service.create_folder(parent_id, folder_name)

    async def rename_item(self, item_id: int, new_name: str, item_type: str) -> Dict[str, Any]:
        return await self._file_service.rename_item(item_id, new_name, item_type)

    async def delete_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._file_service.delete_items(items)

    async def restore_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._file_service.restore_items(items)

    async def delete_items_permanently(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._file_service.delete_items_permanently(items)

    async def empty_trash(self) -> Dict[str, Any]:
        return await self._file_service.empty_trash()

    async def get_trash_items(self) -> Dict[str, Any]:
        return await self._file_service.get_trash_items()

    async def move_items(self, items: List[Dict[str, Any]], target_folder_id: int) -> Dict[str, Any]:
        return await self._file_service.move_items(items, target_folder_id)

    # --- Transfer Service ---
    def get_transfer_config(self) -> Dict[str, Any]:
        return self._transfer_service.get_transfer_config()

    def upload_files(self, parent_id: int, files: List[Dict], progress_callback: Callable) -> Dict[str, Any]:
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_files(parent_id, files, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True}

    def upload_folder(self, parent_id: int, folder_path: str, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.upload_folder_recursive(parent_id, folder_path, task_id, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True}

    def download_items(self, items: List[Dict], destination_dir: str, progress_callback: Callable) -> Dict[str, Any]:
        adapter = self._create_progress_adapter(progress_callback)
        task_coro = self._transfer_service.download_items(items, destination_dir, adapter)
        self._schedule_background_task(task_coro)
        return {"success": True}

    def cancel_transfer(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.cancel_transfer(task_id)

    def pause_transfer(self, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        self._transfer_service.pause_transfer(task_id)
        
        # Use adapter to send immediate status update with -1 to preserve UI progress
        adapter = self._create_progress_adapter(progress_callback)
        adapter(task_id, '', -1, -1, 'paused', 0)
        
        return {"success": True}

    def resume_transfer(self, task_id: str, progress_callback: Callable) -> Dict[str, Any]:
        adapter = self._create_progress_adapter(progress_callback)
        
        # Immediately notify 'queued'
        adapter(task_id, '', 0, 0, 'queued', 0)
        
        # Schedule the actual resume operation
        task_coro = self._transfer_service.resume_transfer(task_id, adapter)
        self._schedule_background_task(task_coro)
        
        return {"success": True}

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        return self._transfer_service.get_incomplete_transfers()

    def remove_transfer_history(self, task_id: str) -> Dict[str, Any]:
        return self._transfer_service.remove_transfer_history(task_id)

    def reorder_queued_tasks(self, ordered_task_ids: list[str]) -> Dict[str, Any]:
        """Reorder queued transfer tasks according to frontend drag priority."""
        return self._transfer_service.reorder_queued_tasks(ordered_task_ids)