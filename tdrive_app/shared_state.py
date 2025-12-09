import asyncio
import os
import threading
from typing import Dict, Any, Optional
from telethon import TelegramClient

TEMP_DIR = os.path.join('file', 'temp')


class SharedState:
    """
    一個用來集中管理所有服務共享狀態的類別。
    其實例會被傳遞給所有需要存取或修改共享狀態的子服務。
    """
    def __init__(self):
        # 認證與連線相關
        self.client: Optional[TelegramClient] = None
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.is_logged_in: bool = False

        # 登入流程中暫存的資訊
        self.phone: Optional[str] = None
        self.phone_code_hash: Optional[str] = None

        # 非同步與回呼相關
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self.connection_emitter: Optional[callable] = None

        # 傳輸任務管理
        self.active_tasks: Dict[str, asyncio.Task] = {}
        
        # 資料庫上傳防抖計時器
        self.db_upload_timer: Optional[threading.Timer] = None
