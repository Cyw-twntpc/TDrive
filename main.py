import os
# Ensure that the PySide6 backend is used for Qt
os.environ['QT_API'] = 'pyside6'

import sys
import ctypes
import asyncio
import logging

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication

from qasync import QEventLoop

from core_app.common import logger_config
from core_app.main_service import TDriveService
from core_app.ui.windows.login_window import LoginWindow
from core_app.ui.windows.main_window import MainWindow

logger = logging.getLogger(__name__)

class AppController(QObject):
    """
    Manages the application's main lifecycle and window switching between the
    login window and the main application window.
    """
    def __init__(self, app: QApplication, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.app = app
        self.loop = loop
        self.login_window = None
        self.main_window = None
        self.tdrive_service = TDriveService(loop=self.loop)

    def start(self):
        """
        Checks the initial login status and displays the appropriate window.
        """
        login_status = self.loop.run_until_complete(self.tdrive_service.check_startup_login())
        
        if login_status.get("logged_in"):
            self.show_main_window(self.tdrive_service)
        else:
            # If the session has expired, pass the existing API credentials
            # to the login window for a smoother re-login experience.
            api_id = login_status.get("api_id")
            api_hash = login_status.get("api_hash")
            self.show_login_window(api_id=api_id, api_hash=api_hash)

    def show_login_window(self, api_id=None, api_hash=None):
        """
        Initializes and displays the login window.
        """
        self.login_window = LoginWindow(self.tdrive_service, self.loop, api_id=api_id, api_hash=api_hash)
        self.login_window.login_successful.connect(self.on_login_successful)
        self.login_window.show()

    def show_main_window(self, service_instance: TDriveService):
        """
        Closes the login window (if it exists) and shows the main application window.
        """
        if self.login_window:
            self.login_window.close()
        
        self.main_window = MainWindow(service_instance, self.loop)
        
        # Re-wire the connection status emitter to the new main window's bridge
        # to ensure UI updates for connection status are handled correctly.
        if self.main_window.bridge and self.main_window.bridge.connection_status_changed:
            service_instance._shared_state.connection_emitter = self.main_window.bridge.connection_status_changed.emit
        
        self.main_window.showMaximized()

    def on_login_successful(self, service_instance: TDriveService):
        """
        Slot triggered by `login_successful` signal from the LoginWindow.
        """
        self.show_main_window(service_instance)

def main():
    """
    Main entry point for the TDrive application.
    sets up the application environment, and starts the main event loop.
    """
    logger_config.setup_logging()

    # Set a custom AppUserModelID for Windows. This is necessary for the
    # application icon to be displayed correctly in the taskbar.
    myappid = 'tdrive.client.v1'
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception as e:
        logger.warning(f"Could not set AppUserModelID. This may affect the taskbar icon on Windows. Error: {e}")

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    QApplication.setAttribute(Qt.AA_UseDesktopOpenGL) 
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)

    app = QApplication(sys.argv)
    
    # Use qasync to integrate asyncio with the Qt event loop
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    controller = AppController(app, loop)
    controller.start()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
