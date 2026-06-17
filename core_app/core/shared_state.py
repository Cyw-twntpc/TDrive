"""
Centralized state management for the TDrive application.

An instance of this class is created by the main service and passed down to
all sub-services. It acts as a shared container for application-wide state,
such as the Telegram client instance, authentication details, and active tasks.
"""
import asyncio
import threading
from typing import Dict, Optional, Callable, Any
from telethon import TelegramClient


class SharedState:
    def __init__(self):
        # --- Authentication & Client ---
        self.client: Optional[TelegramClient] = None
        self.clients_pool: list[TelegramClient] = []
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.group_id: Optional[int] = None
        self.is_logged_in: bool = False

        # --- Temporary data for login flow ---
        self.phone: Optional[str] = None
        self.phone_code_hash: Optional[str] = None

        # --- Async & UI Callbacks ---
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        # Emitter for sending connection status updates to the UI.
        self.connection_emitter: Optional[Callable] = None

        # --- Task Management ---
        # Stores references to active background tasks to prevent garbage collection.
        self.active_tasks: Dict[str, asyncio.Task] = {}
        # Stores references to fire-and-forget tasks to prevent GC
        self.background_tasks: set[asyncio.Task] = set()
        # Timer for debouncing database uploads after modifications.
        self.db_upload_timer: Optional[threading.Timer] = None
        
        # --- Core Managers ---
        self.metadata_manager: Optional[Any] = None
