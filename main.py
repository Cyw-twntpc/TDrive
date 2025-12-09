import os
os.environ['QT_API'] = 'pyside6'

import sys
import ctypes

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon, QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from qasync import QEventLoop

from tdrive_app import logger_config
from tdrive_app.services.utils import cleanup_temp_folders
from tdrive_app.main_service import TDriveService
from tdrive_app.bridge import Bridge

logger = logging.getLogger(__name__)

class TDriveMainWindow(QMainWindow):
    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop, start_url: QUrl):
        super().__init__()
        self.setWindowTitle("TDrive - 您的安全雲端儲存")
        self.setWindowIcon(QIcon(str(Path("web/icon.ico").resolve())))

        # 使用傳入的後端服務實例和事件迴圈
        self.tdrive_service = tdrive_service
        self.bridge = Bridge(self.tdrive_service, loop)

        # 設定 WebChannel
        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        # 設定 WebEngineView
        self.web_view = QWebEngineView()
        self.web_view.page().setWebChannel(self.channel)
        self.web_view.setUrl(start_url)
        self.setCentralWidget(self.web_view)

        # 啟用腳本開啟新視窗的功能
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        
        self.showMaximized()
        logger.info(f"主視窗已初始化並載入 URL: {start_url.toString()}")

        self._is_ready_to_close = False 
        self._loop = loop

    async def _graceful_shutdown(self):
        """執行非同步清理，然後手動關閉視窗"""
        logger.info("正在執行非同步關閉程序...")
        
        # 取消計時器
        if self.tdrive_service._shared_state.db_upload_timer:
            self.tdrive_service._shared_state.db_upload_timer.cancel()
        
        # 關閉服務
        await self.tdrive_service.close()
        
        logger.info("非同步關閉程序完成。")
        self._is_ready_to_close = True
        self.close()

    def closeEvent(self, event: QCloseEvent):
        if self._is_ready_to_close:
            logger.info("TDrive 應用程式正在關閉。")
            event.accept()
        else:
            logger.info("攔截關閉信號，開始清理...")
            event.ignore() # 暫停關閉
            asyncio.create_task(self._graceful_shutdown())


def main():
    logger_config.setup_logging()
    cleanup_temp_folders()

    myappid = 'tdrive.client.v1' 
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception as e:
        logger.warning(f"設定 AppUserModelID 失敗: {e}")

    app = QApplication(sys.argv)
    
    # 使用 qasync 的 Event Loop
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    tdrive_service = TDriveService(loop=loop)

    # 這裡可以直接用 loop.run_until_complete
    is_logged_in = loop.run_until_complete(tdrive_service.check_startup_login())
    start_page_name = "index.html" if is_logged_in else "login.html"
    start_page_path = Path("web").joinpath(start_page_name).resolve()
    start_url = QUrl.fromLocalFile(str(start_page_path))

    main_window = TDriveMainWindow(tdrive_service, loop, start_url)
    
    tdrive_service._shared_state.connection_emitter = main_window.bridge.connection_status_changed.emit
    
    main_window.show()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()