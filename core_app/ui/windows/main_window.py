import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QMainWindow
from PySide6.QtGui import QIcon, QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from core_app.main_service import TDriveService
from core_app.bridge import Bridge

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        self._is_ready_to_close = False

        self.setWindowTitle("TDrive")
        self.setWindowIcon(QIcon(str(Path("web/icon.ico").resolve())))
        
        self.setMinimumSize(800, 600)

        self.bridge = Bridge(self.tdrive_service, self._loop)
        
        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        self.web_view = QWebEngineView()
        self.web_view.page().setWebChannel(self.channel)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        
        self.web_view.setUrl(QUrl.fromLocalFile(str(Path("web/index.html").resolve())))
        self.setCentralWidget(self.web_view)

    async def _graceful_shutdown(self):
        logger.info("Performing graceful shutdown...")
        await self.tdrive_service.close()
        logger.info("Async shutdown procedures complete. Window can now close.")
        self._is_ready_to_close = True
        self.close() 

    def closeEvent(self, event: QCloseEvent):
        if self._is_ready_to_close:
            event.accept()
        else:
            event.ignore()
            asyncio.create_task(self._graceful_shutdown())
