import asyncio
import time
import logging
import json
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class TransferMonitorService:
    """
    負責監控並統計傳輸流量。
    僅保留每日流量統計功能，移除即時圖表繪製相關邏輯。
    """
    def __init__(self):
        # 流量持久化設定
        self.traffic_file = os.path.join("file", "traffic.json")
        self.today_traffic: int = 0
        self._last_traffic_date: str = ""
        self._unsaved_traffic: int = 0
        self._traffic_save_threshold: int = 500 * 1024 # 500 KB

        self._load_traffic_stats()

    def get_today_traffic(self) -> int:
        return self.today_traffic

    def close(self):
        """
        關閉監控服務並強制寫入未儲存的流量數據。
        """
        if self._unsaved_traffic > 0:
            self._save_traffic_stats()
            self._unsaved_traffic = 0

    async def update_transferred_bytes(self, delta: int):
        if delta <= 0: return
        
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