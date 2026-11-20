import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QMainWindow, QApplication, QStackedWidget, QMessageBox
from PySide6.QtGui import QIcon, QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from core_app.core.settings_manager import get_localized_text
from core_app.core.utils import get_resource_path
from core_app.presentation.web_bridge.app_service import TDriveService
from core_app.presentation.web_bridge.api_bridge import Bridge

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    logged_out = Signal()

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        self._is_logging_out = False
        # Removed self._is_ready_to_close as we now quit app directly

        self.setWindowTitle("TDrive")
        self.setWindowIcon(QIcon(str(get_resource_path("web/icon.ico"))))
        
        self.setMinimumSize(800, 600)

        self.bridge = Bridge(self.tdrive_service, self._loop)
        
        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)
        
        self.web_view = QWebEngineView()
        self.web_view.page().profile().clearHttpCache()
        self.web_view.page().setWebChannel(self.channel)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.web_view.page().settings().setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        
        self.web_view.setUrl(QUrl.fromLocalFile(str(get_resource_path("web/index.html"))))
        
        self.stacked_widget.addWidget(self.web_view)
        
        # Connect bridge signals
        self.bridge.play_video_requested.connect(self.handle_play_video)
        self.bridge.logout_completed.connect(self.on_logout_complete)
        
        self.player_widget = None

    def on_logout_complete(self):
        """Handle logout completion by shutting down active player and notifying AppController."""
        logger.info("Logout complete signal received. Returning to login window.")
        self._is_logging_out = True
        if self.player_widget:
            self.player_widget.close_player()
        self.logged_out.emit()

    def handle_play_video(self, stream_url: str, file_name: str):
        from core_app.presentation.native_ui.widgets.video_player_widget import VideoPlayerWidget
        logger.info(f"Embedding video player for URL: {stream_url}")
        
        if self.player_widget:
            self.player_widget.close_player()
            
        self.player_widget = VideoPlayerWidget(stream_url, file_name)
        self.player_widget.closed.connect(self.close_player)
        
        self.stacked_widget.addWidget(self.player_widget)
        self.stacked_widget.setCurrentWidget(self.player_widget)

    def close_player(self):
        logger.info("Closing embedded video player")
        self.stacked_widget.setCurrentWidget(self.web_view)
        if self.player_widget:
            self.stacked_widget.removeWidget(self.player_widget)
            self.player_widget.deleteLater()
            self.player_widget = None

    async def _graceful_shutdown(self):
        logger.info("Performing graceful shutdown (Window hidden)...")
        try:
            await self.tdrive_service.close()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            logger.info("Shutdown complete. Quitting application.")
            QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent):
        if self._is_logging_out:
            event.accept()
            return

        transfer_service = getattr(self.tdrive_service, "_transfer_service", None)
        if transfer_service and hasattr(transfer_service, "has_active_transfers") and transfer_service.has_active_transfers():
            reply = QMessageBox.question(
                self,
                get_localized_text("native.exit_confirm_title", "確認離開 TDrive"),
                get_localized_text(
                    "native.exit_confirm_msg",
                    "目前尚有進行中的傳輸任務，確定要中斷並離開應用程式嗎？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        # Hide window immediately for better UX
        self.hide()
        event.ignore() # Prevent Qt from killing the app immediately
        
        # Stop video playback immediately so it releases connections to the streaming proxy
        if self.player_widget:
            self.player_widget.close_player()
            
        # Start background shutdown task
        self._shutdown_task = asyncio.create_task(self._graceful_shutdown())
        self.tdrive_service._shared_state.background_tasks.add(self._shutdown_task)
        self._shutdown_task.add_done_callback(self.tdrive_service._shared_state.background_tasks.discard)
