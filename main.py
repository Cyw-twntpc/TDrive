import os
import sys

os.environ['QT_API'] = 'pyside6'

if sys.platform == 'win32':
    # Mitigate Windows 11 DWM/MPO drag stutter while keeping VBlank sync intact
    existing_flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '').split()
    if '--disable-direct-composition' not in existing_flags:
        existing_flags.append('--disable-direct-composition')
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = ' '.join(existing_flags)

import ctypes
import asyncio
import logging

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

from qasync import QEventLoop

from core_app.core import logger_config
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.presentation.web_bridge.app_service import TDriveService
from core_app.presentation.native_ui.windows.login_window import LoginWindow
from core_app.presentation.native_ui.windows.main_window import MainWindow
from core_app.presentation.native_ui.windows.splash_screen import SplashScreen

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
            # Queue background services to start safely AFTER the event loop officially runs forever
            self.tdrive_service._shared_state.create_background_task(self.tdrive_service.start_background_services())
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
            self.login_window = None
        
        self.main_window = MainWindow(service_instance, self.loop)
        self.main_window.logged_out.connect(self.on_logout)
        
        # Re-wire the connection status emitter to the new main window's bridge
        # to ensure UI updates for connection status are handled correctly.
        if self.main_window.bridge and self.main_window.bridge.connection_status_changed:
            service_instance._shared_state.connection_emitter = self.main_window.bridge.connection_status_changed.emit
        
        self.main_window.showMaximized()

    def on_login_successful(self, service_instance: TDriveService):
        self.show_main_window(service_instance)

    def on_logout(self):
        """Close main window, reset DB singleton and service, and show LoginWindow."""
        if self.main_window:
            self.main_window.close()
            self.main_window.deleteLater()
            self.main_window = None

        DatabaseConnection._instance = None
        self.tdrive_service = TDriveService(loop=self.loop)
        self.show_login_window()

def _has_live_clients(service: TDriveService) -> bool:
    """Return True if the main Telegram client or any worker bot client is still connected."""
    shared_state = getattr(service, "_shared_state", None)
    if not shared_state:
        return False
    if shared_state.client and shared_state.client.is_connected():
        return True
    return any(bot and bot.is_connected() for bot in (getattr(shared_state, "clients_pool", None) or []))


async def _shutdown_remaining_resources(controller: AppController, loop: asyncio.AbstractEventLoop) -> None:
    """Close any remaining active service connections and cancel leftover asyncio tasks before loop exit."""
    service = getattr(controller, "tdrive_service", None)
    if service is not None and _has_live_clients(service):
        try:
            await service.close()
        except Exception as exc:
            logger.error(f"Error closing TDriveService during final cleanup: {exc}", exc_info=True)

    current = asyncio.current_task(loop)
    pending = [task for task in asyncio.all_tasks(loop) if task is not current and not task.done()]
    if pending:
        logger.debug(f"Cancelling {len(pending)} pending asyncio task(s) before event loop close.")
        for task in pending:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for pending asyncio tasks to finish during exit.")


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

    with loop:
        try:
            controller.start()
            loop.run_forever()
        finally:
            service = getattr(controller, "tdrive_service", None)
            has_pending = any(not task.done() for task in asyncio.all_tasks(loop))
            if (service is not None and _has_live_clients(service)) or has_pending:
                try:
                    loop.run_until_complete(_shutdown_remaining_resources(controller, loop))
                except Exception as exc:
                    logger.error(f"Failed to drain pending tasks on shutdown: {exc}", exc_info=True)

if __name__ == "__main__":
    main()
