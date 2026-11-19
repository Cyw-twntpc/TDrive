"""
Centralized state management for the TDrive application.

An instance of this class is created by the main service and passed down to
all sub-services. It acts as a shared container for application-wide state,
such as the Telegram client instance, authentication details, and active tasks.
"""
import asyncio
import logging
import threading
from typing import Dict, Optional, Callable, Any
from telethon import TelegramClient

logger = logging.getLogger(__name__)


class SharedState:
    def __init__(self):
        # --- Authentication & Client ---
        self.client: Optional[TelegramClient] = None
        self.clients_pool: list[TelegramClient] = []
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        # Telegram group ID for storing uploaded files.
        self._group_id: Optional[int] = None
        self.channel_id: Optional[int] = None
        self.is_logged_in: bool = False

        # --- Temporary data for login flow ---
        self.phone: Optional[str] = None
        self.phone_code_hash: Optional[str] = None

        try:
            self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        # Emitter for sending connection status updates to the UI.
        self.connection_emitter: Optional[Callable] = None

        # --- Task Management ---
        # Stores references to active background tasks to prevent garbage collection.
        self.active_tasks: Dict[str, asyncio.Task] = {}
        # Stores references to fire-and-forget tasks to prevent GC
        self.background_tasks: set[asyncio.Task] = set()
        # Timer for debouncing database uploads after modifications.
        self.db_upload_timer: Optional[threading.Timer] = None
        
        # --- Storage Groups ---
        self.active_group_index: int = 1
        self.group_id_map: Dict[int, int] = {}
        self.current_group_msg_count: int = 0
        self.max_group_messages: int = 995000

        # --- Core Managers ---
        self.metadata_manager: Optional[Any] = None

    def get_telegram_group_id(self, group_index: int) -> int:
        """
        Returns the Telegram group ID corresponding to the given group_index.
        Zero backward compatibility: group_index must be >= 1 and exist in group_id_map.

        :param group_index: Storage group index (int >= 1).
        :return: Telegram group ID (int).
        :raises ValueError: If group_index <= 0.
        :raises KeyError: If group_index is not found in group_id_map.
        """
        if group_index <= 0:
            logger.error(f"get_telegram_group_id: invalid group_index={group_index}, must be >= 1")
            raise ValueError(f"Invalid group_index {group_index}: must be a positive integer >= 1")
        if group_index not in self.group_id_map:
            logger.error(f"get_telegram_group_id: group_index={group_index} not found. Available keys: {list(self.group_id_map.keys())}")
            raise KeyError(f"Storage group index {group_index} not found in group_id_map")
        return self.group_id_map[group_index]

    @property
    def group_id(self) -> Optional[int]:
        return self._group_id

    @group_id.setter
    def group_id(self, val: Optional[int]):
        self._group_id = val
        if val is not None and 1 not in self.group_id_map:
            self.group_id_map[1] = val
            logger.debug(f"group_id set to {val}, auto-populated group_id_map[1]")

    @property
    def active_telegram_group_id(self) -> Optional[int]:
        """
        Returns the Telegram group ID of the currently active storage group.
        Falls back to _group_id if active_group_index is not in group_id_map.
        """
        try:
            return self.get_telegram_group_id(self.active_group_index)
        except (KeyError, ValueError):
            logger.warning(
                f"Active group index {self.active_group_index} not found in group_id_map. "
                f"Falling back to _group_id."
            )
            return self._group_id


    @property
    def _background_tasks(self) -> set[asyncio.Task]:
        return self.background_tasks

    def create_background_task(self, coro) -> asyncio.Task:
        """
        Creates an asyncio task and stores a strong reference to it to prevent
        it from being garbage collected mid-execution.
        """
        task = self.loop.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

