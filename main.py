import os
os.environ['QT_API'] = 'pyside6'
os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = '9222'

import sys
import ctypes
import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

from qasync import QEventLoop

from core_app.common import logger_config
from core_app.main_service import TDriveService
from core_app.ui.windows.login_window import LoginWindow
from core_app.ui.windows.main_window import MainWindow
from core_app.ui.windows.splash_screen import SplashScreen

logger = logging.getLogger(__name__)

class AppController(QObject):
    def __init__(self, app: QApplication, loop: asyncio.AbstractEventLoop, splash: QWidget = None):
        super().__init__()
        self.app = app
        self.loop = loop
        self.splash = splash
        self.login_window = None
        self.main_window = None
        self.tdrive_service = TDriveService(loop=self.loop)

    def start(self):
        login_status = self.loop.run_until_complete(self.tdrive_service.check_startup_login())
        
        if login_status.get("logged_in"):
            self.show_main_window(self.tdrive_service)
        else:
            # If the session has expired, pass the existing API credentials
            # to the login window for a smoother re-login experience.
            api_id = login_status.get("api_id")
            api_hash = login_status.get("api_hash")
            self.show_login_window(api_id=api_id, api_hash=api_hash)
            
        if self.splash:
            self.splash.close()

    def show_login_window(self, api_id=None, api_hash=None):
        self.login_window = LoginWindow(self.tdrive_service, self.loop, api_id=api_id, api_hash=api_hash)
        self.login_window.login_successful.connect(self.on_login_successful)
        self.login_window.show()

    def show_main_window(self, service_instance: TDriveService):
        if self.login_window:
            self.login_window.close()
        
        self.main_window = MainWindow(service_instance, self.loop)
        
        # Re-wire the connection status emitter to the new main window's bridge
        # to ensure UI updates for connection status are handled correctly.
        if self.main_window.bridge and self.main_window.bridge.connection_status_changed:
            service_instance._shared_state.connection_emitter = self.main_window.bridge.connection_status_changed.emit
        
        self.main_window.showMaximized()

    def on_login_successful(self, service_instance: TDriveService):
        self.show_main_window(service_instance)

def main():
    logger_config.setup_logging()

    # Set AppUserModelID for Windows taskbar icon
    myappid = 'tdrive.client.v1'
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception as e:
        logger.warning(f"Could not set AppUserModelID: {e}")

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    
    # Show the custom animated splash screen
    splash = SplashScreen()
    splash.show()
    
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    controller = AppController(app, loop, splash)
    controller.start()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
