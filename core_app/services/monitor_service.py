import asyncio
import time
import logging
import json
import os
from datetime import datetime
from typing import List, Dict, Callable, Optional, TypedDict

logger = logging.getLogger(__name__)

class TransferMonitorService:
    """
    負責監控傳輸進度與速度。
    採用「脈衝偵測」模式：後端僅負責偵測流量到達的瞬間，並將原始數據(包含時間差)
    傳送給前端，由前端負責繪製平滑的補間動畫。
    """
    def __init__(self):
        self._callback: Optional[Callable] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._running = False

        # 統計數據
        self.total_bytes_expected: int = 0
        self.total_bytes_transferred: int = 0
        
        # 內部追蹤
        self._last_total_transferred: int = 0
        self._max_speed_seen: float = 0

        # 流量持久化設定
        self.traffic_file = os.path.join("file", "traffic.json")
        self.today_traffic: int = 0
        self._last_traffic_date: str = ""
        self._unsaved_traffic: int = 0
        self._traffic_save_threshold: int = 500 * 1024 # 500 KB

        self._load_traffic_stats()

    def set_callback(self, callback: Callable):
        self._callback = callback

    def reset_session(self):
        """重置單次工作階段的統計數據"""
        self.total_bytes_expected = 0
        self.total_bytes_transferred = 0
        self._last_total_transferred = 0
        self._max_speed_seen = 0

    def get_today_traffic(self) -> int:
        return self.today_traffic

    async def start(self):
        async with self._lock:
            if self._running: return
            self._running = True
            # 初始化計數器，避免剛啟動時數據錯亂
            self._last_total_transferred = self.total_bytes_transferred
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("TransferMonitorService started (Pulse Mode).")

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
            
            # 發送結束訊號，讓圖表變色
            if self._callback:
                y_max = self._calculate_y_max(self._max_speed_seen)
                
                # 判斷是否真正完成：只有當預期流量大於 0 且 已傳輸量 >= 預期量時，才視為 Completed
                # 若因任務被取消導致 expected 歸零，則視為 Idle (重置)
                is_completed = (self.total_bytes_expected > 0) and \
                               (self.total_bytes_transferred >= self.total_bytes_expected)
                
                final_status = 'completed' if is_completed else 'idle'

                self._callback({
                    'status': final_status,
                    'todayTraffic': self.today_traffic,
                    'yMax': y_max,
                    'points': [] # 結束時清空或保持原樣由前端決定
                })

    async def add_expected_bytes(self, size: int):
        async with self._lock:
            self.total_bytes_expected += size

    async def update_transferred_bytes(self, delta: int):
        if delta <= 0: return
        self.total_bytes_transferred += delta
        
        # 處理每日流量統計
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

    async def remove_task_stats(self, expected_delta: int, transferred_delta: int):
        """當任務取消時，扣除相應的統計量"""
        async with self._lock:
            self.total_bytes_expected = max(0, self.total_bytes_expected - expected_delta)
            self.total_bytes_transferred = max(0, self.total_bytes_transferred - transferred_delta)
            self._last_total_transferred = max(0, self._last_total_transferred - transferred_delta)
            self._max_speed_seen = 0 

    async def _monitor_loop(self):
        """
        核心監控迴圈 (精確脈衝偵測版)
        不進行任何 sleep(1.0) 或插值，只偵測 diff_bytes > 0 的瞬間。
        """
        last_pulse_time = time.time()
        
        while self._running:
            # 高頻率 Polling (100Hz)，確保能敏銳捕捉封包到達時間
            await asyncio.sleep(0.01)
            
            current_time = time.time()
            current_bytes = self.total_bytes_transferred
            diff_bytes = current_bytes - self._last_total_transferred
            
            # [核心] 只有當資料量真的增加時，才視為一個「脈衝」並發送訊號
            if diff_bytes > 0:
                # 計算真實的脈衝間隔 (給前端做動畫時長參考)
                interval = current_time - last_pulse_time
                last_pulse_time = current_time
                
                # 計算真實物理速度
                # safe_interval 防呆：避免間隔過短導致除以零
                safe_interval = max(0.01, interval) 
                real_speed = diff_bytes / safe_interval
                
                if real_speed > self._max_speed_seen:
                    self._max_speed_seen = real_speed
                
                percent = 0
                if self.total_bytes_expected > 0:
                    percent = (current_bytes / self.total_bytes_expected) * 100

                self._last_total_transferred = current_bytes
                
                if self._callback:
                    y_max = self._calculate_y_max(self._max_speed_seen)
                    self._callback({
                        'type': 'pulse',      # 標記這是脈衝訊號
                        'x': percent,         # 目標進度
                        'y': real_speed,      # 目標速度
                        'dt': interval,       # 距離上次脈衝過了多久 (秒)
                        'yMax': y_max,
                        'todayTraffic': self.today_traffic,
                        'status': 'active'
                    })

    # --- 輔助方法 ---
    def _get_today_str(self) -> str:
        return datetime.now().strftime('%Y-%m-%d')

    def _load_traffic_stats(self):
        today_str = self._get_today_str()
        self._last_traffic_date = today_str
        try:
            if os.path.exists(self.traffic_file):
                with open(self.traffic_file, 'r') as f:
                    data = json.load(f)
                    self.today_traffic = data.get(today_str, 0)
            else:
                self.today_traffic = 0
        except Exception:
            self.today_traffic = 0

    def _save_traffic_stats(self):
        try:
            os.makedirs(os.path.dirname(self.traffic_file), exist_ok=True)
            data = {self._get_today_str(): self.today_traffic}
            with open(self.traffic_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save traffic stats: {e}")

    def _calculate_y_max(self, max_speed: float) -> float:
        KB = 1024
        MB = KB * 1024
        GB = MB * 1024
        thresholds = [
            1*KB, 5*KB, 10*KB, 50*KB, 100*KB, 500*KB,
            1*MB, 5*MB, 10*MB, 50*MB, 100*MB, 500*MB,
            1*GB, 5*GB
        ]
        if max_speed <= 0: return 1024.0
        for t in thresholds:
            if max_speed <= t: return float(t)
        return max_speed * 1.2