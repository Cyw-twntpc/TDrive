import asyncio
import ctypes
import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes.wintypes as wt

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QCloseEvent, QColor, QIcon, QPainter, QPalette, QPen
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from core_app.core.settings_manager import SettingsManager, get_localized_text
from core_app.core.utils import get_resource_path
from core_app.presentation.web_bridge.app_service import TDriveService
from core_app.presentation.web_bridge.api_bridge import Bridge

logger = logging.getLogger(__name__)

# Win32 native frameless / DWM constants, structs, and 64-bit-safe bindings
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_SYSMENU = 0x00080000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020

_WM_ERASEBKGND = 0x0014
_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_WM_NCACTIVATE = 0x0086

_HTCAPTION = 2
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_SM_CXSIZEFRAME = 32
_SM_CYSIZEFRAME = 33
_SM_CXPADDEDBORDER = 92
_MONITOR_DEFAULTTONEAREST = 2

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWCP_ROUND = 2

if sys.platform == "win32":
    _LONG_PTR = ctypes.c_ssize_t

    class _MARGINS(ctypes.Structure):
        _fields_ = [
            ("cxLeftWidth", ctypes.c_int),
            ("cxRightWidth", ctypes.c_int),
            ("cyTopHeight", ctypes.c_int),
            ("cyBottomHeight", ctypes.c_int),
        ]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [
            ("rgrc", wt.RECT * 3),
            ("lppos", ctypes.c_void_p),
        ]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("rcMonitor", wt.RECT),
            ("rcWork", wt.RECT),
            ("dwFlags", wt.DWORD),
        ]

    _user32 = ctypes.windll.user32
    _dwmapi = ctypes.windll.dwmapi

    _user32.IsWindow.argtypes = [wt.HWND]
    _user32.IsWindow.restype = wt.BOOL
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsIconic.restype = wt.BOOL
    _user32.IsZoomed.argtypes = [wt.HWND]
    _user32.IsZoomed.restype = wt.BOOL
    _user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = _LONG_PTR
    _user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, _LONG_PTR]
    _user32.SetWindowLongPtrW.restype = _LONG_PTR
    _user32.SetWindowPos.argtypes = [
        wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT
    ]
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _user32.GetWindowRect.restype = wt.BOOL
    _user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    _user32.ScreenToClient.restype = wt.BOOL
    _user32.GetDpiForWindow.argtypes = [wt.HWND]
    _user32.GetDpiForWindow.restype = wt.UINT
    _user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wt.UINT]
    _user32.GetSystemMetricsForDpi.restype = ctypes.c_int
    _user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromWindow.restype = wt.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wt.BOOL

    _dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, ctypes.POINTER(_MARGINS)]
    _dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
    _dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

    _MSG_MESSAGE_OFFSET = wt.MSG.message.offset
    _HANDLED_WIN32_MSGS = frozenset((_WM_ERASEBKGND, _WM_NCCALCSIZE, _WM_NCHITTEST, _WM_NCACTIVATE))
else:
    _user32 = None
    _dwmapi = None
    _MSG_MESSAGE_OFFSET = 0
    _HANDLED_WIN32_MSGS = frozenset()


def _get_win32_frame_insets(hwnd: int) -> tuple[int, int, int]:
    """Return (dpi, frame_x, frame_y) border thickness metrics for a Windows HWND."""
    if _user32 is None:
        return (96, 0, 0)
    dpi = int(_user32.GetDpiForWindow(hwnd) or 96)
    padded = int(_user32.GetSystemMetricsForDpi(_SM_CXPADDEDBORDER, dpi))
    frame_x = int(_user32.GetSystemMetricsForDpi(_SM_CXSIZEFRAME, dpi)) + padded
    frame_y = int(_user32.GetSystemMetricsForDpi(_SM_CYSIZEFRAME, dpi)) + padded
    return (dpi, frame_x, frame_y)


class _CaptionButton(QPushButton):
    """DPI-sharp custom painted window caption button (minimize, maximize/restore, close)."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._maximized = False
        self._hovered = False
        self._pressed = False
        self._fg_color = QColor("#0F172A")
        self._hover_bg = QColor(15, 23, 42, 18)
        self.setFixedSize(46, 36)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = bool(maximized)
        self.update()

    def set_palette_colors(self, fg_color: QColor, hover_bg: QColor) -> None:
        self._fg_color = fg_color
        self._hover_bg = hover_bg
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Draw hover/pressed background
        if self.kind == "close" and (self._hovered or self._pressed):
            bg = QColor("#BE123C") if self._pressed else QColor("#E11D48")
            painter.fillRect(self.rect(), bg)
            icon_color = QColor("#FFFFFF")
        elif self._hovered or self._pressed:
            bg = QColor(self._hover_bg)
            if self._pressed:
                bg.setAlpha(min(255, bg.alpha() + 14))
            painter.fillRect(self.rect(), bg)
            icon_color = self._fg_color
        else:
            icon_color = self._fg_color

        pen = QPen(icon_color, 1.15)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)

        cx = self.width() / 2.0
        cy = self.height() / 2.0

        if self.kind == "minimize":
            painter.drawLine(QPointF(cx - 5.0, cy), QPointF(cx + 5.0, cy))
        elif self.kind == "maximize":
            if self._maximized:
                # Overlapping restore squares
                back = QRectF(cx - 3.0, cy - 5.0, 7.5, 7.5)
                front = QRectF(cx - 5.0, cy - 3.0, 7.5, 7.5)
                painter.drawLine(QPointF(back.left() + 2.0, back.top()), QPointF(back.right(), back.top()))
                painter.drawLine(QPointF(back.right(), back.top()), QPointF(back.right(), back.bottom() - 2.0))
                painter.drawRect(front)
            else:
                painter.drawRect(QRectF(cx - 4.5, cy - 4.5, 9.0, 9.0))
        elif self.kind == "close":
            painter.drawLine(QPointF(cx - 4.5, cy - 4.5), QPointF(cx + 4.5, cy + 4.5))
            painter.drawLine(QPointF(cx + 4.5, cy - 4.5), QPointF(cx - 4.5, cy + 4.5))


class CustomTitleBar(QWidget):
    """Native-style frameless window title bar with drag move, double-click zoom, and theme sync."""

    def __init__(self, parent_window: QMainWindow):
        super().__init__(parent_window)
        self._window = parent_window
        self.setObjectName("CustomTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(36)
        self._bg_color = QColor("#F3F5F9")
        self._border_color = QColor(15, 23, 42, 16)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(8)

        self.icon_label = QLabel()
        icon_pixmap = QIcon(str(get_resource_path("web/icon.ico"))).pixmap(QSize(16, 16))
        self.icon_label.setPixmap(icon_pixmap)
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.title_label = QLabel("TDrive")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.min_btn = _CaptionButton("minimize", self)
        self.min_btn.setToolTip(get_localized_text("window.minimize", "最小化"))
        self.min_btn.clicked.connect(self._window.showMinimized)

        self.max_btn = _CaptionButton("maximize", self)
        self.max_btn.setToolTip(get_localized_text("window.maximize", "最大化"))
        self.max_btn.clicked.connect(self._toggle_maximize)

        self.close_btn = _CaptionButton("close", self)
        self.close_btn.setToolTip(get_localized_text("window.close", "關閉"))
        self.close_btn.clicked.connect(self._window.close)

        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)

        self.apply_theme("light")

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def set_maximized(self, maximized: bool) -> None:
        self.max_btn.set_maximized(maximized)
        if maximized:
            self.max_btn.setToolTip(get_localized_text("window.restore", "還原"))
        else:
            self.max_btn.setToolTip(get_localized_text("window.maximize", "最大化"))

    def apply_theme(self, theme: str) -> None:
        is_dark = theme == "dark"
        bg = "#15181F" if is_dark else "#F3F5F9"
        fg = "#C6CDD8" if is_dark else "#0F172A"
        border = "rgba(210, 220, 238, 0.08)" if is_dark else "rgba(15, 23, 42, 0.06)"
        hover_bg = QColor(210, 220, 238, 20) if is_dark else QColor(15, 23, 42, 18)

        self._bg_color = QColor(bg)
        self._border_color = QColor(210, 220, 238, 22) if is_dark else QColor(15, 23, 42, 16)

        # Scope QSS by objectName so child icon/title labels do not inherit borders
        self.setStyleSheet(
            f"#CustomTitleBar {{ background-color: {bg}; border: none; border-bottom: 1px solid {border}; }}"
        )
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.title_label.setStyleSheet(
            f"color: {fg}; font-size: 12.5px; font-weight: 600; background: transparent; border: none;"
        )
        fg_color = QColor(fg)
        self.min_btn.set_palette_colors(fg_color, hover_bg)
        self.max_btn.set_palette_colors(fg_color, hover_bg)
        self.close_btn.set_palette_colors(fg_color, hover_bg)
        self.update()

    def paintEvent(self, event) -> None:
        """Paint solid title bar surface and hairline divider directly to guarantee non-black chrome."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._bg_color)
        painter.fillRect(0, self.height() - 1, self.width(), 1, self._border_color)

    def mousePressEvent(self, event) -> None:
        if not getattr(self._window, "_win32_frameless_applied", False) and event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if not getattr(self._window, "_win32_frameless_applied", False) and event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class TDriveWebEngineView(QWebEngineView):
    """WebEngine view that intercepts OS local file drops with real host paths."""

    local_paths_dropped = Signal(list)
    _DROP_EVENTS = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.installEventFilter(self)
        for child in self.children():
            child.installEventFilter(self)

    @staticmethod
    def extract_local_paths(mime_data) -> list[str]:
        """Return existing-syntax local filesystem paths carried by a Qt drop mime payload."""
        if not mime_data or not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if url.isLocalFile():
                local_path = url.toLocalFile()
                if local_path:
                    paths.append(local_path)
        return paths

    def eventFilter(self, watched, event) -> bool:
        """Intercept child WebEngine delegate drops for local files while leaving internal drags intact."""
        event_type = event.type()
        if event_type == QEvent.Type.ChildAdded and event.child() is not None:
            event.child().installEventFilter(self)
            return False
        if watched is self or event_type not in self._DROP_EVENTS:
            return False
        mime_data = event.mimeData() if hasattr(event, "mimeData") else None
        paths = self.extract_local_paths(mime_data)
        if paths:
            event.acceptProposedAction()
            if event_type == QEvent.Type.Drop:
                self.local_paths_dropped.emit(paths)
            return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event) -> None:
        if self.extract_local_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self.extract_local_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        paths = self.extract_local_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.local_paths_dropped.emit(paths)
            return
        super().dropEvent(event)


class MainWindow(QMainWindow):
    logged_out = Signal()
    _RESIZE_BORDER_PX = 4

    def __init__(self, tdrive_service: TDriveService, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.tdrive_service = tdrive_service
        self._loop = loop
        self._is_logging_out = False
        self._win32_frameless_applied = False
        self._current_theme = SettingsManager().get("theme", "light")
        if self._current_theme not in ("light", "dark"):
            self._current_theme = "light"

        self.setWindowTitle("TDrive")
        self.setWindowIcon(QIcon(str(get_resource_path("web/icon.ico"))))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setMinimumSize(800, 600)
        self.resize(1280, 800)
        if sys.platform != "win32":
            self.setMouseTracking(True)

        self.bridge = Bridge(self.tdrive_service, self._loop)
        self.bridge.theme_changed.connect(self.apply_theme)

        self.channel = QWebChannel()
        self.channel.registerObject("tdrive_bridge", self.bridge)

        # Frameless shell container with custom title bar and stacked content
        self._root_container = QWidget(self)
        self._root_container.setObjectName("MainWindowFrame")
        if sys.platform != "win32":
            self._root_container.setMouseTracking(True)
        self._root_layout = QVBoxLayout(self._root_container)
        margin = 0 if sys.platform == "win32" else self._RESIZE_BORDER_PX
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        self._root_layout.setSpacing(0)

        self.title_bar = CustomTitleBar(self)
        self.stacked_widget = QStackedWidget()

        self._root_layout.addWidget(self.title_bar)
        self._root_layout.addWidget(self.stacked_widget, 1)
        self.setCentralWidget(self._root_container)

        self.web_view = TDriveWebEngineView()
        self.web_view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.web_view.local_paths_dropped.connect(self.bridge.emit_external_paths_dropped)
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
        self._setup_win32_frameless()
        self.apply_theme(self._current_theme)

    def _setup_win32_frameless(self) -> None:
        """Enable native Win32 frame styles and DWM attributes without triggering D3D11 black flicker."""
        if sys.platform != "win32" or self._win32_frameless_applied or _user32 is None or _dwmapi is None:
            return
        if QApplication.platformName() != "windows":
            return
        try:
            hwnd = int(self.winId())
            if not _user32.IsWindow(hwnd):
                return
            style = int(_user32.GetWindowLongPtrW(hwnd, _GWL_STYLE))
            native_flags = _WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX | _WS_SYSMENU
            _user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style | native_flags)

            is_win11 = sys.getwindowsversion().build >= 22000
            if is_win11:
                # Avoid DwmExtendFrameIntoClientArea on Win11: glass alpha margins clash with Chromium GPU surface
                corner_pref = ctypes.c_int(_DWMWCP_ROUND)
                _dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    _DWMWA_WINDOW_CORNER_PREFERENCE,
                    ctypes.byref(corner_pref),
                    ctypes.sizeof(corner_pref),
                )
            else:
                margins = _MARGINS(0, 0, 1, 0)
                _dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))

            _user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
            )
            self._win32_frameless_applied = True
            self._root_layout.setContentsMargins(0, 0, 0, 0)
        except Exception as exc:
            logger.warning("Failed to enable native Win32 DWM frameless mode; using Qt fallback: %s", exc)

    def _apply_dwm_theme(self, is_dark: bool) -> None:
        """Sync Windows DWM dark mode and Win11 border color attributes with active theme."""
        if not self._win32_frameless_applied or _dwmapi is None:
            return
        try:
            hwnd = int(self.winId())
            dark_val = ctypes.c_int(1 if is_dark else 0)
            _dwmapi.DwmSetWindowAttribute(
                hwnd,
                _DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(dark_val),
                ctypes.sizeof(dark_val),
            )
            if sys.getwindowsversion().build >= 22000:
                # COLORREF uses 0x00BBGGRR: dark #2B3140 -> 0x0040312B, light #CBD5E1 -> 0x00E1D5CB
                border_color = ctypes.c_uint32(0x0040312B if is_dark else 0x00E1D5CB)
                _dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    _DWMWA_BORDER_COLOR,
                    ctypes.byref(border_color),
                    ctypes.sizeof(border_color),
                )
        except Exception as exc:
            logger.debug("Skipped DWM theme attribute sync: %s", exc)

    def apply_theme(self, theme: str) -> None:
        """Synchronize native window frame, custom title bar, and WebEngine canvas color with active theme."""
        normalized = "dark" if theme == "dark" else "light"
        self._current_theme = normalized
        if self.player_widget is None:
            self._apply_chrome_theme(normalized)

    def _apply_chrome_theme(self, theme: str) -> None:
        is_dark = theme == "dark"
        shell_hex = "#15181F" if is_dark else "#F3F5F9"
        border_hex = "#2B3140" if is_dark else "#CBD5E1"
        shell_qcolor = QColor(shell_hex)
        frame_border = "none" if (self._win32_frameless_applied or self.isMaximized()) else f"1px solid {border_hex}"
        self.setStyleSheet(f"QMainWindow {{ background-color: {shell_hex}; }}")
        self._root_container.setStyleSheet(
            f"#MainWindowFrame {{ background-color: {shell_hex}; border: {frame_border}; }}"
        )
        for widget in (self, self._root_container, self.stacked_widget, self.web_view):
            pal = widget.palette()
            pal.setColor(QPalette.ColorRole.Window, shell_qcolor)
            widget.setPalette(pal)
        self.stacked_widget.setStyleSheet(f"QStackedWidget {{ background-color: {shell_hex}; border: none; }}")
        self.title_bar.apply_theme(theme)
        self.web_view.page().setBackgroundColor(shell_qcolor)
        self._apply_dwm_theme(is_dark)

    def _handle_win32_nccalcsize(self, hwnd: int, lparam: int) -> tuple[bool, int]:
        """Strip OS titlebar chrome while clipping maximized frame overhang inside work area."""
        params = _NCCALCSIZE_PARAMS.from_address(lparam)
        rect = params.rgrc[0]
        if _user32 is not None and _user32.IsZoomed(hwnd):
            _, frame_x, frame_y = _get_win32_frame_insets(hwnd)
            rect.left += frame_x
            rect.right -= frame_x
            rect.top += frame_y
            rect.bottom -= frame_y

            monitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
            if monitor:
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    # Preserve 1px bottom trigger when auto-hide taskbar matches full monitor bounds
                    if (
                        info.rcMonitor.top == info.rcWork.top
                        and info.rcMonitor.bottom == info.rcWork.bottom
                        and info.rcMonitor.left == info.rcWork.left
                        and info.rcMonitor.right == info.rcWork.right
                    ):
                        rect.bottom -= 1
        return (True, 0)

    def _handle_win32_nchittest(self, hwnd: int, lparam: int) -> tuple[bool, int] | None:
        """Delegate titlebar drag (HTCAPTION) and window edge resize hit testing directly to Windows."""
        if _user32 is None:
            return None

        # Decode signed 16-bit screen coords (negative values occur on left/top secondary monitors)
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value

        win_rect = wt.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            return None

        dpi, _, _ = _get_win32_frame_insets(hwnd)
        if not _user32.IsZoomed(hwnd):
            border = max(5, round(6 * dpi / 96))
            left = x < win_rect.left + border
            right = x >= win_rect.right - border
            top = y < win_rect.top + border
            bottom = y >= win_rect.bottom - border

            if top and left:
                return (True, _HTTOPLEFT)
            if top and right:
                return (True, _HTTOPRIGHT)
            if bottom and left:
                return (True, _HTBOTTOMLEFT)
            if bottom and right:
                return (True, _HTBOTTOMRIGHT)
            if left:
                return (True, _HTLEFT)
            if right:
                return (True, _HTRIGHT)
            if top:
                return (True, _HTTOP)
            if bottom:
                return (True, _HTBOTTOM)

        pt = wt.POINT(x, y)
        if not _user32.ScreenToClient(hwnd, ctypes.byref(pt)):
            return None
        dpr = self.devicePixelRatioF() or (dpi / 96.0)
        rel_x = pt.x / dpr
        rel_y = pt.y / dpr

        if 0 <= rel_y < self.title_bar.height() and 0 <= rel_x < self.title_bar.min_btn.geometry().left():
            return (True, _HTCAPTION)
        return None

    def nativeEvent(self, eventType, message):
        """Intercept Windows non-client messages for flicker-free DWM dragging and snapping."""
        if self._win32_frameless_applied and eventType in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg_addr = int(message)
            # Fast-path message ID check avoids constructing a full MSG struct on every drag tick
            msg_id = ctypes.c_uint.from_address(msg_addr + _MSG_MESSAGE_OFFSET).value
            if msg_id not in _HANDLED_WIN32_MSGS:
                return super().nativeEvent(eventType, message)
            msg = wt.MSG.from_address(msg_addr)
            if msg_id == _WM_ERASEBKGND:
                return (True, 1)
            if msg_id == _WM_NCACTIVATE and _user32 is not None and not _user32.IsIconic(int(msg.hWnd)):
                return (True, 1)
            if msg_id == _WM_NCCALCSIZE:
                if msg.wParam == 0:
                    return (True, 0)
                return self._handle_win32_nccalcsize(int(msg.hWnd), int(msg.lParam))
            if msg_id == _WM_NCHITTEST:
                hit = self._handle_win32_nchittest(int(msg.hWnd), int(msg.lParam))
                if hit is not None:
                    return hit
        return super().nativeEvent(eventType, message)

    def _hit_test_edges(self, pos) -> Qt.Edge:
        """Return Qt.Edge flags if cursor lies inside the outer frameless resize border (non-Win32 fallback)."""
        if self.isMaximized():
            return Qt.Edge(0)
        m = self._RESIZE_BORDER_PX + 2
        edges = Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _cursor_for_edges(self, edges: Qt.Edge) -> Qt.CursorShape:
        if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        if edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mouseMoveEvent(self, event) -> None:
        if not self._win32_frameless_applied:
            edges = self._hit_test_edges(event.position().toPoint())
            self.setCursor(self._cursor_for_edges(edges))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if not self._win32_frameless_applied and event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edges = self._hit_test_edges(event.position().toPoint())
            if edges != Qt.Edge(0):
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._setup_win32_frameless()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            maximized = self.isMaximized()
            self.title_bar.set_maximized(maximized)
            if not self._win32_frameless_applied:
                m = 0 if maximized else self._RESIZE_BORDER_PX
                self._root_layout.setContentsMargins(m, m, m, m)
            self._apply_chrome_theme("dark" if self.player_widget is not None else self._current_theme)
        super().changeEvent(event)

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

        self._apply_chrome_theme("dark")
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
        self._apply_chrome_theme(self._current_theme)

    async def _graceful_shutdown(self):
        logger.info("Performing graceful shutdown (Window hidden)...")
        try:
            await self.tdrive_service.close()
        except asyncio.CancelledError:
            logger.warning("Graceful shutdown task was cancelled unexpectedly.")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)
        finally:
            logger.info("Shutdown complete. Quitting application.")
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def closeEvent(self, event: QCloseEvent):
        if self._is_logging_out:
            event.accept()
            return

        existing_task = getattr(self, "_shutdown_task", None)
        if isinstance(existing_task, asyncio.Future) and not existing_task.done():
            event.ignore()
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
