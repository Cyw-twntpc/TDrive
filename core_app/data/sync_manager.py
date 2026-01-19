import threading
import time
import logging
import asyncio
from typing import Callable, Optional

logger = logging.getLogger(__name__)

class SyncManager:
    """
    Manages adaptive synchronization based on operation 'score'.
    """
    SCORE_THRESHOLD_IMMEDIATE = 20
    FORCE_SYNC_SCORE = 1000
    DEBOUNCE_DELAY = 2.0  # Seconds

    def __init__(self):
        self._score = 0
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._sync_callback: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._busy = False

    def set_callback(self, callback: Callable, loop: asyncio.AbstractEventLoop):
        """
        Sets the callback to trigger synchronization.
        The callback should be an async coroutine, which will be scheduled on the provided loop.
        """
        self._sync_callback = callback
        self._loop = loop

    def set_busy(self, busy: bool):
        """
        Sets the busy state. When busy, syncs are suppressed until:
        1. Busy state ends (set_busy(False))
        2. Score exceeds FORCE_SYNC_SCORE
        """
        with self._lock:
            if self._busy == busy:
                return
            
            self._busy = busy
            logger.info(f"SyncManager busy state set to: {busy}")
            
            if not self._busy and self._score > 0:
                # If we just finished being busy and have pending changes, sync immediately
                self._trigger_sync_now()

    def add_change(self, score_delta: int = 1):
        """
        Adds to the change score and triggers sync logic.
        """
        with self._lock:
            self._score += score_delta
            logger.debug(f"Sync score updated: {self._score} (Busy: {self._busy})")

            if self._busy:
                # In busy mode, only sync if we hit the safety ceiling
                if self._score >= self.FORCE_SYNC_SCORE:
                    logger.info("Force sync triggered during busy mode (score limit reached).")
                    self._trigger_sync_now()
            else:
                # Normal mode
                if self._score >= self.SCORE_THRESHOLD_IMMEDIATE:
                    self._trigger_sync_now()
                else:
                    self._reset_debounce_timer()

    def _reset_debounce_timer(self):
        if self._timer:
            self._timer.cancel()
        
        self._timer = threading.Timer(self.DEBOUNCE_DELAY, self._trigger_sync_now)
        self._timer.start()

    def _trigger_sync_now(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            
            # Reset score BEFORE callback to accumulate new changes happening during sync
            # (Though ideally sync snapshots current state)
            # For this simple model, we reset score when we trigger.
            self._score = 0

        if self._sync_callback and self._loop:
            logger.info("Triggering adaptive sync...")
            # Schedule the coroutine on the event loop
            if self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._sync_callback(), self._loop)
            else:
                logger.warning("Event loop is not running, cannot trigger sync.")
