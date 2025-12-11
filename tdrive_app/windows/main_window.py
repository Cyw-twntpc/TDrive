import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QMainWindow
from PySide6.QtGui import QIcon, QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from tdrive_app.main_service import TDriveService
from tdrive_app.bridge import Bridge

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    """
    The main application window, which hosts the primary file browsing UI.
    """
    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        self._is_ready_to_close = False

        self.setWindowTitle("TDrive")
        self.setWindowIcon(QIcon(str(Path("web/icon.ico").resolve())))
        
        self.setMinimumSize(800, 600)

        # The bridge connects the Python backend to the main UI's JavaScript.
        self.bridge = Bridge(self.tdrive_service, self._loop)
        
        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        self.web_view = QWebEngineView()
        self.web_view.page().setWebChannel(self.channel)
        # These settings can be useful for development and for enabling
        # certain features in the web UI, but should be reviewed for security.
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        
        self.web_view.setUrl(QUrl.fromLocalFile(str(Path("web/index.html").resolve())))
        self.setCentralWidget(self.web_view)

    async def _graceful_shutdown(self):
        """
        Performs asynchronous shutdown procedures before closing the window.
        
        This ensures that services, like the Telethon client, are properly
        disconnected before the application exits.
        """
        logger.info("Performing graceful shutdown...")
        await self.tdrive_service.close()
        logger.info("Async shutdown procedures complete. Window can now close.")
        self._is_ready_to_close = True
        self.close() # Re-issue the close command now that we are ready.

    def closeEvent(self, event: QCloseEvent):
        """
        Overrides the close event to ensure a graceful shutdown.

        The first time the user tries to close the window, the event is ignored,
        and `_graceful_shutdown` is triggered. Once the async shutdown is complete,
        it calls `self.close()` again. This time, `_is_ready_to_close` is true,
        and the event is accepted, closing the window.
        """
        if self._is_ready_to_close:
            event.accept()
        else:
            event.ignore()
            asyncio.create_task(self._graceful_shutdown())
