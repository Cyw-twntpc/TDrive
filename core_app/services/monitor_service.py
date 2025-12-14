import asyncio
import time
import logging
import json
import os
from datetime import datetime
from typing import List, Dict, Callable, Optional, TypedDict

logger = logging.getLogger(__name__)

class ChartPoint(TypedDict):
    x: float  # Percentage (0-100)
    y: float  # Speed (bytes/sec)

class TransferMonitorService:
    """
    A standalone service to monitor transfer progress and generate
    speed vs. completion chart data. Handles traffic persistence.
    """
    def __init__(self):
        self._callback: Optional[Callable] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._running = False

        # Statistics State
        self.history_points: List[ChartPoint] = []
        self.total_bytes_expected: int = 0
        self.total_bytes_transferred: int = 0
        
        # Internal tracking for speed calculation
        self._last_check_time: float = 0
        self._last_total_transferred: int = 0
        self._max_speed_seen: float = 0
        self._smoothed_speed: float = 0  # For EMA

        # Traffic Persistence
        self.traffic_file = os.path.join("file", "traffic.json")
        self.today_traffic: int = 0
        self._last_traffic_date: str = ""
        self._unsaved_traffic: int = 0
        self._traffic_save_threshold: int = 500 * 1024 # 500 KB

        # Initialize Traffic Data
        self._load_traffic_stats()

    def _get_today_str(self) -> str:
        return datetime.now().strftime('%Y-%m-%d')

    def _load_traffic_stats(self):
        """Loads traffic stats from JSON file."""
        today_str = self._get_today_str()
        self._last_traffic_date = today_str
        try:
            if os.path.exists(self.traffic_file):
                with open(self.traffic_file, 'r') as f:
                    data = json.load(f)
                    self.today_traffic = data.get(today_str, 0) if today_str in data else 0
            else:
                self.today_traffic = 0
        except Exception as e:
            logger.error(f"Failed to load traffic stats: {e}")
            self.today_traffic = 0

    def _save_traffic_stats(self, force_date: str = None):
        """Saves current traffic stats to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.traffic_file), exist_ok=True)
            date_key = self._get_today_str()
            data = {date_key: self.today_traffic}
            with open(self.traffic_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save traffic stats: {e}")

    def set_callback(self, callback: Callable):
        self._callback = callback

    def reset_session(self):
        """Resets session-specific stats."""
        logger.debug("Resetting monitor session stats.")
        self.history_points = []
        self.total_bytes_expected = 0
        self.total_bytes_transferred = 0
        self._max_speed_seen = 0
        self._last_total_transferred = 0
        self._smoothed_speed = 0

    async def start(self):
        async with self._lock:
            if self._running: return
            self._running = True
            self._last_check_time = time.time()
            self._last_total_transferred = self.total_bytes_transferred
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("TransferMonitorService started.")

    async def stop(self):
        async with self._lock:
            self._running = False
            if self._monitor_task:
                self._monitor_task.cancel()
                try: await self._monitor_task
                except asyncio.CancelledError: pass
                self._monitor_task = None
            
            if self._unsaved_traffic > 0:
                self._save_traffic_stats()
                self._unsaved_traffic = 0
            
            # Emit final completed state
            if self._callback:
                y_max = self._calculate_y_max(self._max_speed_seen)
                self._callback({
                    'points': self.history_points,
                    'yMax': y_max,
                    'todayTraffic': self.today_traffic,
                    'status': 'completed'
                })
            logger.info("TransferMonitorService stopped.")

    async def add_expected_bytes(self, size: int):
        async with self._lock:
            if size <= 0: return
            old_total = self.total_bytes_expected
            self.total_bytes_expected += size
            new_total = self.total_bytes_expected
            if old_total > 0:
                ratio = old_total / new_total
                for point in self.history_points:
                    point['x'] *= ratio

    async def update_transferred_bytes(self, delta: int):
        if delta <= 0: return
        self.total_bytes_transferred += delta
        
        current_date = self._get_today_str()
        if current_date != self._last_traffic_date:
            self._last_traffic_date = current_date
            self.today_traffic = 0
            self._unsaved_traffic = 0
        
        self.today_traffic += delta
        self._unsaved_traffic += delta
        
        if self._unsaved_traffic >= self._traffic_save_threshold:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: self._save_traffic_stats())
                self._unsaved_traffic = 0
            except RuntimeError: pass

    async def _monitor_loop(self):
        while self._running:
            await asyncio.sleep(0.5)
            
            current_time = time.time()
            time_diff = current_time - self._last_check_time
            if time_diff <= 0: continue
            
            current_bytes = self.total_bytes_transferred
            diff_bytes = current_bytes - self._last_total_transferred
            
            # Calculate Instant Speed
            inst_speed = diff_bytes / time_diff if diff_bytes > 0 else 0
            
            # Apply EMA smoothing (alpha = 0.3 means 30% current, 70% history)
            # Adjust alpha based on expected volatility
            alpha = 0.3
            self._smoothed_speed = (alpha * inst_speed) + ((1 - alpha) * self._smoothed_speed)
            
            # Use smoothed speed for display, but instant for logic?
            # Let's use smoothed for display to avoid flickering.
            display_speed = self._smoothed_speed
            
            # Small threshold to snap to 0
            if display_speed < 10: display_speed = 0

            self._last_check_time = current_time
            self._last_total_transferred = current_bytes
            
            if display_speed > self._max_speed_seen:
                self._max_speed_seen = display_speed

            percent = 0
            if self.total_bytes_expected > 0:
                percent = (current_bytes / self.total_bytes_expected) * 100
                percent = min(100, max(0, percent))

            last_recorded_percent = self.history_points[-1]['x'] if self.history_points else -1
            should_record = False
            if not self.history_points: should_record = True
            elif (percent - last_recorded_percent) >= 0.5: should_record = True
            
            if should_record:
                self.history_points.append({'x': percent, 'y': display_speed})

            y_max = self._calculate_y_max(self._max_speed_seen)

            # Emit Data
            if self._callback:
                self._callback({
                    'points': self.history_points,
                    'yMax': y_max,
                    'todayTraffic': self.today_traffic,
                    'status': 'active'
                })

    def _calculate_y_max(self, max_speed: float) -> float:
        KB = 1024
        MB = KB * 1024
        GB = MB * 1024
        thresholds = [
            1*KB, 5*KB, 10*KB, 50*KB, 100*KB, 500*KB,
            1*MB, 5*MB, 10*MB, 50*MB, 100*MB, 500*MB,
            1*GB, 5*GB, 10*GB, 50*GB
        ]
        # Always have a minimum non-zero max (e.g., 1KB) to avoid div by zero
        if max_speed <= 0: return 1024.0
        
        for t in thresholds:
            if max_speed <= t: return t
        return max_speed * 1.2