import logging
import subprocess
import os
from typing import List

logger = logging.getLogger(__name__)

class PlayerService:
    def __init__(self):
        self.active_processes: List[subprocess.Popen] = []
        # Determine VLC path based on relative path from project root
        # Assuming vlc folder is at project_root/vlc/vlc.exe
        self.vlc_path = os.path.abspath(os.path.join("vlc", "vlc.exe"))

    def check_vlc_exists(self) -> bool:
        return os.path.exists(self.vlc_path)

    def play_video(self, stream_url: str):
        if not self.check_vlc_exists():
            logger.error(f"VLC executable not found at: {self.vlc_path}")
            return False, "找不到播放器，請確認 vlc 目錄是否存在。"

        try:
            # Launch VLC
            # --fullscreen: Start in fullscreen (Removed as requested)
            # --play-and-exit: Close VLC when playback finishes (optional, maybe user wants to replay?)
            # Let's keep it simple first.
            cmd = [
                self.vlc_path,
                stream_url
            ]
            
            logger.info(f"Launching VLC: {cmd}")
            # Use Popen to non-block
            process = subprocess.Popen(cmd)
            self.active_processes.append(process)
            
            # Clean up dead processes from list
            self.active_processes = [p for p in self.active_processes if p.poll() is None]
            
            return True, "播放器已啟動"
        except Exception as e:
            logger.error(f"Failed to launch VLC: {e}", exc_info=True)
            return False, f"啟動播放器失敗: {e}"

    def terminate_all(self):
        """Terminates all active VLC processes."""
        for p in self.active_processes:
            if p.poll() is None:
                try:
                    logger.info(f"Terminating VLC process {p.pid}...")
                    p.terminate()
                    # p.wait(timeout=1) # Optional wait
                except Exception as e:
                    logger.error(f"Error terminating VLC process: {e}")
        self.active_processes = []
