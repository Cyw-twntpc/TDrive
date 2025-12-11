import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, QPoint, Signal
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon, QCloseEvent, QGuiApplication, QColor
from PySide6.QtWebEngineWidgets import QWebEngineView

from tdrive_app.main_service import TDriveService
from tdrive_app.bridge import Bridge

logger = logging.getLogger(__name__)

class LoginWindow(QMainWindow):
    """
    A frameless window dedicated to handling the login process.
    
    It hosts a QWebEngineView that loads the `login.html` page and sets up
    a QWebChannel to facilitate communication between Python and JavaScript.
    """
    # Signal emitted upon successful login and initialization.
    login_successful = Signal(TDriveService)

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop, api_id=None, api_hash=None):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        # Optional credentials for pre-filling if a session has expired.
        self._api_id = api_id
        self._api_hash = api_hash
        self._drag_offset = None
        self._is_ready_to_close = False

        self.setWindowTitle("TDrive - Login")
        self.setWindowIcon(QIcon(str(Path("web/icon.ico").resolve())))

        # Set flags for a frameless, transparent window, allowing the HTML/CSS to define the window's appearance.
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Center the window on the primary screen.
        screen = QGuiApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        window_width = 440
        window_height = 700
        x = (screen_geometry.width() - window_width) // 2
        y = (screen_geometry.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        self.setFixedSize(window_width, window_height)

        # Set up the Python-JavaScript bridge.
        self.bridge = Bridge(self.tdrive_service, self._loop)
        # This connection is the final step in the login flow, triggering the window switch.
        self.bridge.login_and_initialization_complete.connect(self.on_login_complete)
        self.bridge.window_action.connect(self.handle_window_action)
        self.bridge.drag_window.connect(self.handle_drag_window)
        self.bridge.drag_start.connect(self.handle_drag_start)
        self.bridge.drag_end.connect(self.handle_drag_end)

        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        # Configure the web view.
        self.web_view = QWebEngineView()
        self.web_view.page().setWebChannel(self.channel)
        self.web_view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.web_view.loadFinished.connect(self.on_load_finished)
        self.web_view.setUrl(QUrl.fromLocalFile(str(Path("web/login.html").resolve())))
        self.setCentralWidget(self.web_view)

    def on_load_finished(self, success: bool):
        """
        Slot called after the web page has finished loading.
        If pre-fill credentials exist, it injects them into the web page.
        """
        if success and self._api_id and self._api_hash:
            logger.info("Web page loaded. Pre-filling API credentials from expired session.")
            js_code = f"window.prefill_api_credentials('{self._api_id}', '{self._api_hash}');"
            self.web_view.page().runJavaScript(js_code)

    def on_login_complete(self):
        """
        Slot triggered by the bridge when the backend confirms successful login.
        It emits the `login_successful` signal to notify the AppController.
        """
        logger.info("Login process complete. Emitting login_successful signal.")
        # Set this flag to True to allow the window to be closed gracefully by the AppController.
        self._is_ready_to_close = True
        self.login_successful.emit(self.tdrive_service)
        
    def handle_window_action(self, action: str):
        """Handles window actions like minimize and close."""
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
        """
        Overrides the default close event.

        If the window is closed before the login is complete (e.g., by the user
        closing it manually), it quits the entire application. Otherwise, it
        accepts the event, allowing the window to be closed by the AppController.
        """
        if not self._is_ready_to_close:
            logger.info("Login not complete. Closing the application.")
            QApplication.instance().quit()
        event.accept()
