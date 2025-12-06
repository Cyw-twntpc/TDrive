import logging
import asyncio
import threading
from telethon import TelegramClient
from typing import TYPE_CHECKING, Optional

from .. import telegram_comms

# 為了避免循環匯入問題，只在類型檢查時匯入 SharedState
if TYPE_CHECKING:
    from ..shared_state import SharedState

logger = logging.getLogger(__name__)

async def ensure_client_connected(shared_state: 'SharedState') -> Optional[TelegramClient]:
    """
    確保 Telegram 客戶端已連線。如果未連線，則鎖定 UI 並嘗試重新連線。
    返回一個已連線的 client 物件，如果失敗則返回 None。
    """
    if shared_state.client and shared_state.client.is_connected():
        return shared_state.client

    logger.warning("偵測到連線中斷，鎖定 UI 並開始重試 Telegram 連線...")
    
    if shared_state.eel_instance:
        try:
            # 呼叫前端函式以顯示連線中斷的遮罩
            shared_state.eel_instance.show_connection_lost()()
        except Exception as e:
            logger.error(f"呼叫 show_connection_lost 失敗: {e}")

    api_id = shared_state.api_id
    api_hash = shared_state.api_hash
    session_file = f'./file/user_{api_id}.session'

    if not (api_id and api_hash):
        logger.error("錯誤：在 SharedState 中找不到 API 憑證，無法重新連線。")
        if shared_state.eel_instance:
            shared_state.eel_instance.hide_connection_lost()()
        return None

    while True:
        try:
            if shared_state.client:
                try:
                    await shared_state.client.disconnect()
                except Exception as e:
                    logger.debug(f"舊客戶端斷開連線時發生錯誤: {e}")

            new_client = TelegramClient(session_file, api_id, api_hash)
            await new_client.connect()
            
            if await new_client.is_user_authorized():
                logger.info("Telegram 重新連線成功！")
                shared_state.client = new_client
                
                if shared_state.eel_instance:
                    shared_state.eel_instance.hide_connection_lost()()
                return new_client
            else:
                logger.warning("重新連線失敗：使用者授權無效。可能需要重新登入。")
                break

        except Exception as e:
            logger.error(f"Telegram 重新連線嘗試失敗: {e}")
            await asyncio.sleep(5)
    
    if shared_state.eel_instance:
        shared_state.eel_instance.hide_connection_lost()()
    return None

async def trigger_db_upload_in_background(shared_state: 'SharedState'):
    """
    使用防抖 (debounce) 機制，在背景觸發資料庫上傳。
    這可以將短時間內的多個修改合併為一次上傳。
    """
    
    # 建立要在 asyncio 事件迴圈中執行的任務
    async def upload_task():
        try:
            logger.info("執行延遲的資料庫上傳任務...")
            client = await ensure_client_connected(shared_state)
            if not client or not shared_state.api_id:
                logger.error("背景資料庫上傳任務中止，因為無法確保客戶端連線或 api_id 不存在。")
                return

            group_id = await telegram_comms.get_group(client, shared_state.api_id)
            await telegram_comms.sync_database_file(client, group_id, mode='upload')
            logger.info("背景資料庫上傳任務已完成。")
        except Exception as e:
            logger.error(f"背景資料庫上傳任務失敗: {e}", exc_info=True)

    # 這個函式將被 Timer 呼叫
    def schedule_async_task():
        if shared_state.loop.is_running():
            shared_state.loop.call_soon_threadsafe(lambda: asyncio.create_task(upload_task()))
        else:
            logger.warning("事件迴圈未執行，無法排程資料庫上傳。")

    # 如果存在計時器，取消它
    if shared_state.db_upload_timer:
        shared_state.db_upload_timer.cancel()
        logger.debug("取消了先前的資料庫上傳計時器。")

    # 建立並啟動一個新的計時器，延遲 2 秒執行
    shared_state.db_upload_timer = threading.Timer(2.0, schedule_async_task)
    shared_state.db_upload_timer.start()
    logger.debug("已排程一個新的資料庫上傳任務在 2 秒後執行。")

