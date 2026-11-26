import logging
import os
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, Qt, QThread, QTimer

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

    def __init__(self, state_callback: Optional[Callable[[dict[str, Any]], None]] = None):
        self._lock = threading.RLock()
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
        self._current_ms: int = 0
        self._duration_ms: int = 0

        self._init_timer()

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
        """Lazily initialize the headless VLC instance and media player."""
        if self._player is not None:
            return True
        try:
            vlc = _load_vlc_module()
            self._vlc_mod = vlc
            self._vlc_instance = vlc.Instance("--no-video", "--quiet")
            self._player = self._vlc_instance.media_player_new()
            self._player.audio_set_volume(self._volume)
            self._player.audio_set_mute(self._muted)
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
        """Poll VLC player time, duration, and state on timer ticks."""
        with self._lock:
            player = self._player
            if player is None or self._current_file_id is None:
                self._control_timer(False)
                return

            try:
                mapped_state = self._map_vlc_state(player.get_state())
                raw_time = player.get_time()
                raw_len = player.get_length()
                if isinstance(raw_len, int) and raw_len > 0:
                    self._duration_ms = raw_len
                if isinstance(raw_time, int) and raw_time >= 0:
                    self._current_ms = raw_time
                self._state = mapped_state

                if mapped_state in ("ended", "error", "stopped"):
                    if mapped_state == "ended" and self._duration_ms > 0:
                        self._current_ms = self._duration_ms
                    self._control_timer(False)
            except Exception as exc:
                logger.warning("Failed to poll VLC audio state: %s", exc)
                return

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
        with self._lock:
            if not self._ensure_player():
                self._state = "error"
                self._emit_state()
                return {"success": False, "state": self.get_state()}

            try:
                media = self._vlc_instance.media_new(stream_url)
                self._player.set_media(media)
                self._player.audio_set_volume(self._volume)
                self._player.audio_set_mute(self._muted)
                self._player.play()
                self._current_file_id = file_id
                self._current_name = file_name or ""
                self._current_ms = 0
                self._duration_ms = 0
                self._state = "buffering"
                self._control_timer(True)
            except Exception as exc:
                logger.error("Failed to start audio stream %s: %s", stream_url, exc, exc_info=True)
                self._state = "error"
                self._control_timer(False)
                self._emit_state()
                return {"success": False, "state": self.get_state()}

        self._emit_state()
        return {"success": True, "state": self.get_state()}

    def control(self, action: str, value: int = 0) -> dict[str, Any]:
        """Execute a transport or mixer command: toggle, pause, resume, stop, seek, volume, mute."""
        with self._lock:
            player = self._player
            if action == "volume":
                clamped = max(0, min(100, int(value)))
                self._volume = clamped
                if player is not None:
                    player.audio_set_volume(clamped)
            elif action == "mute":
                self._muted = (not self._muted) if int(value) < 0 else bool(value)
                if player is not None:
                    player.audio_set_mute(self._muted)
            elif action == "stop":
                if player is not None:
                    player.stop()
                self._state = "stopped"
                self._current_ms = 0
                self._duration_ms = 0
                self._current_file_id = None
                self._current_name = ""
                self._control_timer(False)
            elif action == "pause":
                if player is not None:
                    player.set_pause(1)
                self._state = "paused"
                self._control_timer(False)
            elif action == "resume":
                if player is not None:
                    player.set_pause(0)
                if self._current_file_id is not None:
                    self._state = "playing"
                    self._control_timer(True)
            elif action == "toggle":
                if self._state in ("playing", "buffering"):
                    if player is not None:
                        player.set_pause(1)
                    self._state = "paused"
                    self._control_timer(False)
                elif self._current_file_id is not None:
                    if player is not None:
                        if self._state in ("ended", "stopped"):
                            player.stop()
                            player.play()
                        else:
                            player.set_pause(0)
                    self._state = "playing"
                    self._control_timer(True)
            elif action == "seek":
                target_ms = max(0, int(value))
                if self._duration_ms > 0:
                    target_ms = min(target_ms, self._duration_ms)
                self._current_ms = target_ms
                if player is not None and self._current_file_id is not None:
                    player.set_time(target_ms)
            else:
                return {"success": False, "state": self.get_state()}

        self._emit_state()
        return {"success": True, "state": self.get_state()}

    def cleanup(self) -> None:
        """Stop playback and release VLC resources safely on logout or shutdown."""
        with self._lock:
            self._control_timer(False)
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
            self._current_file_id = None
            self._current_name = ""
            self._current_ms = 0
            self._duration_ms = 0
            self._state = "stopped"
