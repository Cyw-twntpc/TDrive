from concurrent.futures import ThreadPoolExecutor
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QMetaObject, Qt, QThread, QTimer

from core_app.core.utils import ensure_extracted

logger = logging.getLogger(__name__)


def _load_vlc_module():
    """Locate bundled VLC binaries and import python-vlc."""
    vlc_path = ensure_extracted("vlc")
    os.environ["PYTHON_VLC_MODULE_PATH"] = vlc_path
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(vlc_path)
        except Exception as exc:
            logger.debug("add_dll_directory failed for %s: %s", vlc_path, exc)
    import vlc

    return vlc


class AudioPlayerManager:
    """Headless VLC-backed audio player for HTTP Range streams from VideoStreamer."""

    SEEK_HOLD_SECONDS: float = 1.5

    def __init__(self, state_callback: Optional[Callable[[dict[str, Any]], None]] = None):
        self._lock = threading.RLock()
        self._vlc_lock = threading.Lock()
        self._vlc_executor: Optional[ThreadPoolExecutor] = None
        self._state_callback = state_callback
        self._vlc_mod = None
        self._vlc_instance = None
        self._player = None
        self._timer: Optional[QTimer] = None

        self._current_file_id: Optional[int] = None
        self._current_name: str = ""
        self._state: str = "stopped"
        self._volume: int = 80
        self._muted: bool = False
        self._volume_dirty: bool = False
        self._current_ms: int = 0
        self._duration_ms: int = 0
        self._pending_seek_ms: Optional[int] = None
        self._pending_seek_until: float = 0.0

        self._init_timer()

    @property
    def vlc_executor(self) -> ThreadPoolExecutor:
        """Lazily create a dedicated single-thread executor to keep libVLC C calls off UI/asyncio loops."""
        with self._lock:
            if self._vlc_executor is None:
                self._vlc_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="vlc-audio",
                )
            return self._vlc_executor

    def _init_timer(self) -> None:
        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            self._timer = QTimer(app)
            self._timer.setInterval(250)
            self._timer.timeout.connect(self.poll_state)

    def _control_timer(self, start: bool) -> None:
        if self._timer is None:
            return
        app = QCoreApplication.instance()
        slot_name = "start" if start else "stop"
        if app is not None and QThread.currentThread() != self._timer.thread():
            QMetaObject.invokeMethod(self._timer, slot_name, Qt.ConnectionType.QueuedConnection)
        elif start:
            self._timer.start()
        else:
            self._timer.stop()

    def set_state_callback(self, callback: Optional[Callable[[dict[str, Any]], None]]) -> None:
        """Register or replace the callback invoked on playback state updates."""
        with self._lock:
            self._state_callback = callback

    def _ensure_player(self) -> bool:
        """Lazily initialize the headless VLC instance and media player (caller holds _vlc_lock)."""
        if self._player is not None:
            return True
        try:
            vlc = _load_vlc_module()
            vlc_instance = vlc.Instance("--no-video", "--quiet")
            player = vlc_instance.media_player_new()
            with self._lock:
                vol = self._volume
                muted = self._muted
                self._vlc_mod = vlc
                self._vlc_instance = vlc_instance
                self._player = player
            player.audio_set_volume(vol)
            player.audio_set_mute(muted)
            return True
        except Exception as exc:
            logger.error("Failed to initialize VLC audio player: %s", exc, exc_info=True)
            return False

    def get_state(self) -> dict[str, Any]:
        """Return a snapshot of the current playback state."""
        with self._lock:
            return {
                "file_id": self._current_file_id,
                "file_name": self._current_name,
                "state": self._state,
                "current_ms": self._current_ms,
                "duration_ms": self._duration_ms,
                "volume": self._volume,
                "muted": self._muted,
            }

    def _emit_state(self) -> None:
        cb = self._state_callback
        if cb is not None:
            try:
                cb(self.get_state())
            except Exception as exc:
                logger.error("Audio state callback raised error: %s", exc, exc_info=True)

    def _map_vlc_state(self, raw_state) -> str:
        vlc = self._vlc_mod
        if vlc is None:
            return self._state
        state_map = {
            vlc.State.Playing: "playing",
            vlc.State.Paused: "paused",
            vlc.State.Buffering: "buffering",
            vlc.State.Opening: "buffering",
            vlc.State.Ended: "ended",
            vlc.State.Error: "error",
            vlc.State.Stopped: "stopped",
            vlc.State.NothingSpecial: "stopped",
        }
        return state_map.get(raw_state, "stopped")

    def poll_state(self) -> None:
        """Poll VLC player time, duration, and state on timer ticks without blocking the UI thread."""
        if not self._vlc_lock.acquire(blocking=False):
            return

        should_emit = False
        try:
            with self._lock:
                player = self._player
                if player is None or self._current_file_id is None:
                    self._control_timer(False)
                    return

            try:
                mapped_state = self._map_vlc_state(player.get_state())
                raw_time = player.get_time()
                raw_len = player.get_length()

                # Windows libVLC often ignores volume/mute before audio output opens; re-apply once playing.
                with self._lock:
                    apply_audio_settings = mapped_state == "playing" and self._volume_dirty
                    vol = self._volume
                    muted = self._muted
                if apply_audio_settings:
                    player.audio_set_volume(vol)
                    player.audio_set_mute(muted)
                    with self._lock:
                        self._volume_dirty = False

                now = time.monotonic()
                with self._lock:
                    if isinstance(raw_len, int) and raw_len > 0:
                        self._duration_ms = raw_len
                    if isinstance(raw_time, int) and raw_time >= 0:
                        if self._pending_seek_ms is not None:
                            if now < self._pending_seek_until and abs(raw_time - self._pending_seek_ms) > 2000:
                                self._current_ms = self._pending_seek_ms
                            else:
                                self._pending_seek_ms = None
                                self._current_ms = raw_time
                        else:
                            self._current_ms = raw_time
                    self._state = mapped_state

                    if mapped_state in ("ended", "error", "stopped"):
                        self._pending_seek_ms = None
                        if mapped_state == "ended" and self._duration_ms > 0:
                            self._current_ms = self._duration_ms
                        self._control_timer(False)
                should_emit = True
            except Exception as exc:
                logger.warning("Failed to poll VLC audio state: %s", exc)
                return
        finally:
            self._vlc_lock.release()

        if should_emit:
            self._emit_state()

    def play_url(
        self,
        file_id: int,
        stream_url: str,
        file_name: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Load a stream URL into VLC and begin playback immediately."""
        del metadata  # Reserved for future tag/artwork hints
        with self._vlc_lock:
            if not self._ensure_player():
                with self._lock:
                    self._state = "error"
                    self._control_timer(False)
                self._emit_state()
                return {"success": False, "state": self.get_state()}

            try:
                media = self._vlc_instance.media_new(stream_url)
                if media is None:
                    raise RuntimeError("VLC media_new returned None")
                self._player.set_media(media)
                if hasattr(media, "release"):
                    try:
                        media.release()
                    except Exception as e:
                        logger.debug("Failed to release VLC media handle: %s", e)
                with self._lock:
                    vol = self._volume
                    muted = self._muted
                self._player.audio_set_volume(vol)
                self._player.audio_set_mute(muted)
                self._player.play()
                with self._lock:
                    self._current_file_id = file_id
                    self._current_name = file_name or ""
                    self._current_ms = 0
                    self._duration_ms = 0
                    self._state = "buffering"
                    self._volume_dirty = True
                    self._pending_seek_ms = None
                    self._pending_seek_until = 0.0
                    self._control_timer(True)
            except Exception as exc:
                logger.error("Failed to start audio stream %s: %s", stream_url, exc, exc_info=True)
                with self._lock:
                    self._state = "error"
                    self._pending_seek_ms = None
                    self._control_timer(False)
                self._emit_state()
                return {"success": False, "state": self.get_state()}

        self._emit_state()
        return {"success": True, "state": self.get_state()}

    def _apply_logical_control(self, action: str, value: int = 0) -> tuple[bool, Optional[dict[str, Any]]]:
        """Update thread-safe playback state synchronously and return the deferred VLC command spec."""
        with self._lock:
            if action == "volume":
                clamped = max(0, min(100, int(value)))
                self._volume = clamped
                self._volume_dirty = True
                return True, {"action": "volume", "value": clamped}
            if action == "mute":
                self._muted = (not self._muted) if int(value) < 0 else bool(value)
                self._volume_dirty = True
                return True, {"action": "mute", "value": self._muted}
            if action == "stop":
                self._state = "stopped"
                self._current_ms = 0
                self._duration_ms = 0
                self._current_file_id = None
                self._current_name = ""
                self._pending_seek_ms = None
                self._pending_seek_until = 0.0
                self._control_timer(False)
                return True, {"action": "stop"}
            if action == "pause":
                self._state = "paused"
                self._control_timer(False)
                return True, {"action": "pause"}
            if action == "resume":
                if self._current_file_id is not None:
                    restart = self._state in ("ended", "stopped")
                    self._state = "playing"
                    if restart:
                        self._volume_dirty = True
                    self._control_timer(True)
                    return True, {"action": "resume", "restart": restart}
                return True, None
            if action == "toggle":
                if self._state in ("playing", "buffering"):
                    self._state = "paused"
                    self._control_timer(False)
                    return True, {"action": "pause"}
                if self._current_file_id is not None:
                    restart = self._state in ("ended", "stopped")
                    self._state = "playing"
                    if restart:
                        self._volume_dirty = True
                    self._control_timer(True)
                    return True, {"action": "toggle_play", "restart": restart}
                return True, None
            if action == "seek":
                target_ms = max(0, int(value))
                if self._duration_ms > 0:
                    target_ms = min(target_ms, self._duration_ms)
                was_ended = self._state in ("ended", "stopped")
                self._current_ms = target_ms
                if self._current_file_id is not None:
                    self._pending_seek_ms = target_ms
                    self._pending_seek_until = time.monotonic() + self.SEEK_HOLD_SECONDS
                    if was_ended:
                        self._state = "playing"
                        self._volume_dirty = True
                        self._control_timer(True)
                    return True, {"action": "seek", "target_ms": target_ms, "restart": was_ended}
                return True, None
            return False, None

    def _execute_vlc_control(self, op: Optional[dict[str, Any]]) -> None:
        """Execute a blocking libVLC C API operation under _vlc_lock."""
        if not op:
            return
        with self._vlc_lock:
            player = self._player
            if player is None:
                return
            action = op.get("action")
            try:
                if action == "volume":
                    player.audio_set_volume(op["value"])
                elif action == "mute":
                    player.audio_set_mute(op["value"])
                elif action == "stop":
                    player.stop()
                elif action == "pause":
                    player.set_pause(1)
                elif action in ("resume", "toggle_play"):
                    if op.get("restart"):
                        player.stop()
                        player.play()
                    else:
                        player.set_pause(0)
                elif action == "seek":
                    if op.get("restart"):
                        player.stop()
                        player.play()
                    player.set_time(op["target_ms"])
            except Exception as exc:
                logger.warning("VLC control action '%s' failed: %s", action, exc)

    def control(self, action: str, value: int = 0) -> dict[str, Any]:
        """Execute a transport or mixer command synchronously: toggle, pause, resume, stop, seek, volume, mute."""
        valid, op = self._apply_logical_control(action, value)
        if not valid:
            return {"success": False, "state": self.get_state()}

        self._execute_vlc_control(op)
        self._emit_state()
        return {"success": True, "state": self.get_state()}

    def dispatch_control(self, action: str, value: int = 0) -> dict[str, Any]:
        """Update state immediately and run the libVLC control call on the dedicated audio worker."""
        valid, op = self._apply_logical_control(action, value)
        if not valid:
            return {"success": False, "state": self.get_state()}

        self._emit_state()
        if op:
            try:
                self.vlc_executor.submit(self._execute_vlc_control, op)
            except Exception as exc:
                logger.warning("Failed to dispatch VLC control '%s': %s", action, exc)
        return {"success": True, "state": self.get_state()}

    def cleanup(self) -> None:
        """Stop playback, drain queued VLC commands, and release VLC resources safely."""
        self._control_timer(False)
        with self._lock:
            executor = self._vlc_executor
            self._vlc_executor = None

        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=False)
            except Exception as exc:
                logger.debug("Error shutting down VLC audio executor: %s", exc)

        with self._vlc_lock:
            if self._player is not None:
                try:
                    self._player.stop()
                    self._player.release()
                except Exception as exc:
                    logger.debug("Error releasing VLC audio player: %s", exc)
                self._player = None
            if self._vlc_instance is not None:
                try:
                    self._vlc_instance.release()
                except Exception as exc:
                    logger.debug("Error releasing VLC audio instance: %s", exc)
                self._vlc_instance = None
            self._vlc_mod = None

        with self._lock:
            self._current_file_id = None
            self._current_name = ""
            self._current_ms = 0
            self._duration_ms = 0
            self._state = "stopped"
            self._volume_dirty = False
            self._pending_seek_ms = None
            self._pending_seek_until = 0.0
