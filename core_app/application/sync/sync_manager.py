import threading
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
    RETRY_BASE_DELAY = 5.0
    RETRY_MAX_DELAY = 60.0
    MAX_RETRY_ATTEMPTS = 5

    def __init__(self):
        self._score = 0
        self._dirty: bool = False
        self._change_seq: int = 0
        self._retry_count: int = 0
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._retry_timer: Optional[threading.Timer] = None
        self._sync_callback: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._busy_count = 0 # Changed from boolean to counter

    def set_callback(self, callback: Callable, loop: asyncio.AbstractEventLoop):
        """
        Sets the callback to trigger synchronization.
        The callback should be an async coroutine, which will be scheduled on the provided loop.
        """
        self._sync_callback = callback
        self._loop = loop

    def has_pending_changes(self) -> bool:
        """Return True if local database mutations have not yet been confirmed synced."""
        with self._lock:
            return bool(self._dirty or self._score > 0)

    def mark_synced(self, synced_seq: Optional[int] = None) -> None:
        """Clear dirty/retry state once a snapshot covering synced_seq finishes uploading."""
        with self._lock:
            if synced_seq is None or synced_seq == self._change_seq:
                self._dirty = False
            self._retry_count = 0
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None

    def set_busy(self, busy: bool):
        """
        Sets the busy state using a reference counter.
        Syncs are suppressed as long as _busy_count > 0.
        """
        with self._lock:
            if busy:
                self._busy_count += 1
            else:
                if self._busy_count > 0:
                    self._busy_count -= 1
            
            logger.debug(f"SyncManager busy count: {self._busy_count}")
            
            # If we just finished being busy (count dropped to 0) and have pending changes
            if self._busy_count == 0 and self._score > 0:
                self._trigger_sync_now()

    def add_change(self, score_delta: int = 1):
        """
        Adds to the change score and triggers sync logic.
        """
        with self._lock:
            self._score += score_delta
            self._change_seq += 1
            self._dirty = True
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            is_busy = self._busy_count > 0

            if is_busy:
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
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            
            # Snapshot dispatched score and change sequence before resetting score;
            # keep _dirty True until the async sync confirms completion.
            dispatched_score = self._score
            trigger_seq = self._change_seq
            self._score = 0
            callback = self._sync_callback
            loop = self._loop

        if callback and loop:
            logger.info("Triggering adaptive sync...")
            # Schedule the coroutine on the event loop
            if loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(callback(), loop)
                fut.add_done_callback(lambda f: self._on_sync_done(f, dispatched_score, trigger_seq))
            else:
                logger.warning("Event loop is not running, cannot trigger sync.")

    def _on_sync_done(self, fut, dispatched_score: int, trigger_seq: int) -> None:
        """Handle sync Future outcome and schedule exponential backoff retries on failure."""
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except Exception as err:
            logger.error(f"Failed inspecting sync future outcome: {err}", exc_info=True)
            exc = err

        if exc is not None:
            logger.error(f"Adaptive background sync raised an exception: {exc}", exc_info=exc)
            succeeded = False
        else:
            try:
                res = fut.result()
                succeeded = res is not False
            except Exception as err:
                logger.error(f"Adaptive background sync result error: {err}", exc_info=True)
                succeeded = False

        if succeeded:
            self.mark_synced(trigger_seq)
            return

        with self._lock:
            self._score += max(1, dispatched_score)
            self._dirty = True
            if self._sync_callback and self._loop and self._retry_count < self.MAX_RETRY_ATTEMPTS:
                self._retry_count += 1
                delay = min(self.RETRY_BASE_DELAY * (2 ** (self._retry_count - 1)), self.RETRY_MAX_DELAY)
                if self._retry_timer:
                    self._retry_timer.cancel()
                logger.warning(
                    f"Adaptive sync failed; scheduling retry {self._retry_count}/{self.MAX_RETRY_ATTEMPTS} in {delay:.1f}s."
                )
                self._retry_timer = threading.Timer(delay, self._trigger_sync_now)
                self._retry_timer.start()

    def stop(self):
        """
        Stops the SyncManager, cancelling any active debounce timers and resetting state.
        """
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            self._score = 0
            self._dirty = False
            self._retry_count = 0
            self._sync_callback = None
            self._loop = None

    def shutdown(self):
        """Alias for stop()."""
        self.stop()

