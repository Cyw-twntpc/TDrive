import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, QPoint, Signal
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon, QCloseEvent, QGuiApplication, QColor, QDesktopServices
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

from core_app.main_service import TDriveService
from core_app.bridge import Bridge

logger = logging.getLogger(__name__)

class ExternalLinkPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, _type, isMainFrame):
        if _type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, _type, isMainFrame)

class LoginWindow(QMainWindow):
    login_successful = Signal(TDriveService)

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop, api_id=None, api_hash=None):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        self._api_id = api_id
        self._api_hash = api_hash
        self._drag_offset = None
        self._is_ready_to_close = False

        self.setWindowTitle("TDrive - 登入")
        self.setWindowIcon(QIcon(str(Path("web/icon.ico").resolve())))

        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        screen = QGuiApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        window_width = 440
        window_height = 700
        x = (screen_geometry.width() - window_width) // 2
        y = (screen_geometry.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        self.setFixedSize(window_width, window_height)

        self.bridge = Bridge(self.tdrive_service, self._loop)
        self.bridge.login_and_initialization_complete.connect(self.on_login_complete)
        self.bridge.window_action.connect(self.handle_window_action)
        self.bridge.drag_window.connect(self.handle_drag_window)
        self.bridge.drag_start.connect(self.handle_drag_start)
        self.bridge.drag_end.connect(self.handle_drag_end)

        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        self.web_view = QWebEngineView()
        self.web_view.setPage(ExternalLinkPage(self.web_view))
        self.web_view.page().setWebChannel(self.channel)
        self.web_view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.web_view.loadFinished.connect(self.on_load_finished)
        self.web_view.setUrl(QUrl.fromLocalFile(str(Path("web/login.html").resolve())))
        self.setCentralWidget(self.web_view)

    def on_load_finished(self, success: bool):
        if success and self._api_id and self._api_hash:
            logger.info("Web page loaded. Pre-filling API credentials from expired session.")
            js_code = f"window.prefill_api_credentials('{self._api_id}', '{self._api_hash}');"
            self.web_view.page().runJavaScript(js_code)

    def on_login_complete(self):
        logger.info("Login process complete. Emitting login_successful signal.")
        self._is_ready_to_close = True
        self.login_successful.emit(self.tdrive_service)
        
    def handle_window_action(self, action: str):
        if action == "minimize":
            self.showMinimized()
        elif action == "close":
            self.close()
    
    def handle_drag_start(self, global_x: int, global_y: int):
        self._drag_offset = QPoint(global_x - self.pos().x(), global_y - self.pos().y())
    
    def handle_drag_window(self, global_x: int, global_y: int):
        if self._drag_offset:
            self.move(global_x - self._drag_offset.x(), global_y - self._drag_offset.y())

    def handle_drag_end(self):
        self._drag_offset = None

    def closeEvent(self, event: QCloseEvent):
        if not self._is_ready_to_close:
            logger.info("Login not complete. Closing the application.")
            QApplication.instance().quit()
        event.accept()
