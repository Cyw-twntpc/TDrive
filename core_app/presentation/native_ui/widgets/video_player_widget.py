import os
import sys
import logging
import threading

from core_app.core.utils import ensure_extracted

vlc_path = ensure_extracted('vlc')
os.environ['PYTHON_VLC_MODULE_PATH'] = vlc_path
if hasattr(os, 'add_dll_directory'):
    try:
        os.add_dll_directory(vlc_path)
    except Exception as e:
        pass

import vlc
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QPushButton, QSlider, QFrame, QLabel, QToolTip, QApplication,
                               QStyle, QStyleOptionSlider)
from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QRectF, QPropertyAnimation, QVariantAnimation, QParallelAnimationGroup, QPauseAnimation, QSequentialAnimationGroup
from PySide6.QtGui import QCursor, QPainter, QColor, QPen, QPalette

logger = logging.getLogger(__name__)

class LoadingSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.rotate)
        self.timer.setInterval(16)
        self.setFixedSize(60, 60)
        
    def rotate(self):
        self.angle = (self.angle + 6) % 360
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw background ring
        pen_bg = QPen(QColor(255, 255, 255, 40), 6)
        painter.setPen(pen_bg)
        painter.drawEllipse(5, 5, 50, 50)
        
        # Draw spinning arc
        pen_fg = QPen(QColor(59, 130, 246, 255), 6)
        pen_fg.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        
        rect = QRectF(5, 5, 50, 50)
        painter.drawArc(rect, -self.angle * 16, 120 * 16)

class LoadingOverlay(QWidget):
    def __init__(self, target_widget):
        super().__init__(None)
        self.target_widget = target_widget
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(160, 140)
        self.setStyleSheet("background: transparent;")
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(15)
        
        self.spinner = LoadingSpinner()
        layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        
        self.label = QLabel("加載中...")
        self.label.setStyleSheet("color: white; font-weight: bold; font-size: 16px; background: transparent;")
        layout.addWidget(self.label, 0, Qt.AlignmentFlag.AlignHCenter)

    def update_position(self):
        if self.target_widget.isVisible() and self.target_widget.window().isVisible():
            center = self.target_widget.mapToGlobal(self.target_widget.rect().center())
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
        
    def showEvent(self, event):
        self.spinner.timer.start()
        self.update_position()
        super().showEvent(event)
        
    def hideEvent(self, event):
        self.spinner.timer.stop()
        super().hideEvent(event)

class StateOverlay(QWidget):
    def __init__(self, target_widget):
        super().__init__(None)
        self.target_widget = target_widget
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(250, 250)
        self.setStyleSheet("background: transparent;")
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_style(140)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        
        self.opacity_group = QSequentialAnimationGroup(self)
        self.pause_anim = QPauseAnimation(400)
        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(300)
        self.fade_anim.setStartValue(1.0)
        self.fade_anim.setEndValue(0.0)
        self.opacity_group.addAnimation(self.pause_anim)
        self.opacity_group.addAnimation(self.fade_anim)

        self.font_anim = QVariantAnimation(self)
        self.font_anim.setDuration(150)
        self.font_anim.setStartValue(110)
        self.font_anim.setEndValue(140)
        self.font_anim.valueChanged.connect(self._update_style)

        self.animation_group = QParallelAnimationGroup(self)
        self.animation_group.addAnimation(self.opacity_group)
        self.animation_group.addAnimation(self.font_anim)
        self.animation_group.finished.connect(self.hide)

    def _update_style(self, size):
        self.icon_label.setStyleSheet(f"color: rgba(220, 220, 220, 180); font-size: {size}px; font-weight: bold; background: transparent;")

    def update_position(self):
        if self.target_widget.isVisible() and self.target_widget.window().isVisible():
            center = self.target_widget.mapToGlobal(self.target_widget.rect().center())
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def show_state(self, is_playing):
        if is_playing:
            self.icon_label.setText("►")
        else:
            self.icon_label.setText("❚❚")
            
        self.update_position()
        self.setWindowOpacity(1.0)
        self.show()
        
        self.animation_group.stop()
        self.animation_group.start()

class VideoPlayerWidget(QWidget):
    closed = Signal()

    def __init__(self, stream_url: str, file_name: str = "Video"):
        super().__init__()
        
        self.setStyleSheet("""
            VideoPlayerWidget { background-color: #000000; }
            QToolTip { 
                color: #ffffff; 
                background-color: rgba(40, 44, 52, 230); 
                border: 1px solid rgba(255, 255, 255, 40); 
                padding: 4px 8px; 
                border-radius: 4px;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        # 1. Video Frame (Bottom Layer)
        self.video_frame = QFrame()
        
        # Force a pure black palette so it doesn't flash white before VLC renders
        palette = self.video_frame.palette()
        palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.black)
        self.video_frame.setAutoFillBackground(True)
        self.video_frame.setPalette(palette)
        
        if sys.platform == "win32":
            self.video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
            self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        else:
            self.video_frame.setStyleSheet("background-color: black;")
            
        self.layout.addWidget(self.video_frame, 0, 0)
        
        # --- Loading Overlay ---
        self.loading_overlay = LoadingOverlay(self.video_frame)
        self.state_overlay = StateOverlay(self.video_frame)
        self.is_seeking = False
        self.stuck_ticks = 0
        self.last_time = -1
        
        self.video_frame.installEventFilter(self)
        
        # Install global application event filter for zero-latency focus tracking
        if QApplication.instance():
            QApplication.instance().installEventFilter(self)
            
        # --- Top Bar ---
        self.top_bar = QWidget()
        self.top_bar.setStyleSheet("background-color: rgba(40, 44, 52, 220);")
        self.top_layout = QHBoxLayout(self.top_bar)
        self.top_layout.setContentsMargins(20, 10, 20, 10)
        
        self.title_label = QLabel(file_name)
        self.title_label.setStyleSheet("color: #E0E0E0; font-weight: bold; font-size: 15px; background: transparent;")
        
        self.close_button = QPushButton("✕")
        self.close_button.setToolTip("返回")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFixedSize(32, 32)
        self.close_button.setStyleSheet("""
            QPushButton { 
                background: rgba(255, 255, 255, 0.1); 
                color: #E0E0E0; 
                font-size: 16px; 
                border-radius: 6px; 
            }
            QPushButton:hover { 
                background: rgba(255, 68, 68, 0.8); 
                color: white; 
            }
        """)
        self.close_button.clicked.connect(self.close_player)
        self.top_layout.addWidget(self.title_label)
        self.top_layout.addStretch(1)
        self.top_layout.addWidget(self.close_button)
        
        # --- Time Preview Label ---
        self.time_preview_label = QLabel(self)
        self.time_preview_label.setStyleSheet("background-color: rgba(0, 0, 0, 200); color: white; padding: 3px 6px; border-radius: 4px; font-size: 12px;")
        self.time_preview_label.hide()
        
        # --- Bottom Bar ---
        self.bottom_bar = QWidget()
        self.bottom_bar.setStyleSheet("background-color: rgba(40, 44, 52, 220);")
        self.bottom_layout = QHBoxLayout(self.bottom_bar)
        self.bottom_layout.setContentsMargins(20, 8, 20, 8)
        self.bottom_layout.setSpacing(12)
        
        self.play_icon = "►"
        self.pause_icon = "❚❚"
        
        self.play_button = QPushButton(self.pause_icon)
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setFixedSize(36, 36)
        self.play_button.setStyleSheet("""
            QPushButton { 
                background: rgba(255, 255, 255, 0.1); 
                color: #E0E0E0; 
                font-size: 14px; 
                border-radius: 6px; 
            }
            QPushButton:hover { 
                background: rgba(59, 130, 246, 0.8); 
                color: white; 
            }
        """)
        self.play_button.clicked.connect(self.toggle_play)
        self.bottom_layout.addWidget(self.play_button)
        
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setMouseTracking(True)
        self.position_slider.installEventFilter(self)
        self.position_slider.setMaximum(1000)
        self.position_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.position_slider.setStyleSheet("""
            QSlider { background: transparent; height: 24px; }
            QSlider::groove:horizontal {
                border: none; height: 3px; background: rgba(255, 255, 255, 0.2); border-radius: 1px;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6; border-radius: 1px;
            }
            QSlider::handle:horizontal {
                background: #ffffff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
            }
            QSlider::handle:horizontal:hover {
                background: #60a5fa; transform: scale(1.2);
            }
        """)
        self.position_slider.sliderMoved.connect(self.handle_time_drag)
        self.bottom_layout.addWidget(self.position_slider)
        
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setFixedWidth(110)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet("color: #E0E0E0; font-family: 'Segoe UI', monospace; font-size: 13px; background: transparent;")
        self.bottom_layout.addWidget(self.time_label)

        # Volume Icon
        self.volume_button = QPushButton("🔊")
        self.volume_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_button.setFixedSize(30, 30)
        self.volume_button.setStyleSheet("""
            QPushButton { 
                background: transparent; 
                color: #E0E0E0; 
                font-size: 16px; 
                border-radius: 6px; 
            }
            QPushButton:hover { 
                background: rgba(255, 255, 255, 0.1); 
            }
        """)
        self.volume_button.clicked.connect(self.toggle_mute)
        self.bottom_layout.addWidget(self.volume_button)

        # Volume Slider
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.setStyleSheet("""
            QSlider { background: transparent; height: 20px; }
            QSlider::groove:horizontal {
                border: none; height: 3px; background: rgba(255, 255, 255, 0.2); border-radius: 1px;
            }
            QSlider::sub-page:horizontal {
                background: #10b981; border-radius: 1px;
            }
            QSlider::handle:horizontal {
                background: #ffffff; width: 10px; height: 10px; margin: -3px 0; border-radius: 5px;
            }
            QSlider::handle:horizontal:hover {
                background: #34d399;
            }
        """)
        self.volume_slider.sliderMoved.connect(self.handle_volume_drag)
        self.bottom_layout.addWidget(self.volume_slider)

        self.layout.addWidget(self.top_bar, 0, 0, Qt.AlignmentFlag.AlignTop)
        self.layout.addWidget(self.bottom_bar, 0, 0, Qt.AlignmentFlag.AlignBottom)

        self.setMouseTracking(True)
        self.video_frame.setMouseTracking(True)
        self.top_bar.setMouseTracking(True)
        self.bottom_bar.setMouseTracking(True)
        self.loading_overlay.setMouseTracking(True)
        
        self.installEventFilter(self)
        self.video_frame.installEventFilter(self)
        self.position_slider.installEventFilter(self)
        self.volume_slider.installEventFilter(self)
        
        self.hide_timer = QTimer(self)
        self.hide_timer.setInterval(3000)
        self.hide_timer.timeout.connect(self.hide_controls)
        self.hide_timer.start()

        if not hasattr(self, 'instance') or self.instance is None:
            self.instance = vlc.Instance('--no-xlib', '--network-caching=5000', '--quiet')
            self.player = self.instance.media_player_new()
        
        self.player.video_set_mouse_input(False)
        self.player.video_set_key_input(False)
        
        if sys.platform.startswith('linux'):
            self.player.set_xwindow(self.video_frame.winId())
        elif sys.platform == "win32":
            self.player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            self.player.set_nsobject(int(self.video_frame.winId()))

        self.media = self.instance.media_new(stream_url)
        self.player.set_media(self.media)
        
        self.player.audio_set_volume(100)
        self.is_muted = False
        self.previous_volume = 100
        
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(200)
        self.ui_timer.timeout.connect(self.update_ui)
        
        self.is_playing = False
        self.play()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.loading_overlay.update_position()
        self.state_overlay.update_position()
        
    def moveEvent(self, event):
        super().moveEvent(event)
        self.loading_overlay.update_position()
        self.state_overlay.update_position()
        
    def showEvent(self, event):
        super().showEvent(event)
        self.loading_overlay.update_position()
        self.state_overlay.update_position()

    def toggle_mute(self):
        if self.is_muted:
            self.is_muted = False
            self.player.audio_set_volume(self.previous_volume)
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(self.previous_volume)
            self.volume_slider.blockSignals(False)
            self.volume_button.setText("🔊")
        else:
            self.is_muted = True
            self.previous_volume = self.volume_slider.value()
            self.player.audio_set_volume(0)
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(0)
            self.volume_slider.blockSignals(False)
            self.volume_button.setText("🔇")

    def handle_volume_drag(self, value):
        self.player.audio_set_volume(value)
        if value > 0 and self.is_muted:
            self.is_muted = False
            self.volume_button.setText("🔊")
        elif value == 0 and not self.is_muted:
            self.is_muted = True
            self.volume_button.setText("🔇")
        QToolTip.showText(QCursor.pos(), f"音量: {value}%", self.volume_slider)
        self.show_controls()

    def handle_time_drag(self, position):
        self.set_position(position)
        length_ms = self.player.get_length()
        if length_ms > 0:
            target_time_ms = int(length_ms * (position / 1000.0))
            time_str = self.format_time(target_time_ms)
            QToolTip.showText(QCursor.pos(), time_str, self.position_slider)

    def get_slider_value_from_pos(self, x):
        opt = QStyleOptionSlider()
        self.position_slider.initStyleOption(opt)
        style = self.position_slider.style()
        grc = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self.position_slider)
        hrc = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self.position_slider)
        
        length = grc.width() - hrc.width()
        pos = x - grc.x() - hrc.width() // 2
        
        if length > 0:
            val = (pos / length) * self.position_slider.maximum()
            return max(0, min(self.position_slider.maximum(), int(val)))
        return 0

    def eventFilter(self, obj, event):
        if not hasattr(self, 'position_slider') or not hasattr(self, 'volume_slider'):
            return super().eventFilter(obj, event)
            
        if obj == self.position_slider:
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    val = self.get_slider_value_from_pos(event.position().x())
                    self.position_slider.setValue(val)
                    self.handle_time_drag(val)
            elif event.type() == QEvent.Type.MouseMove:
                if self.player and self.player.get_length() > 0:
                    val = self.get_slider_value_from_pos(event.position().x())
                    hover_time = int((val / self.position_slider.maximum()) * self.player.get_length())
                    self.time_preview_label.setText(self.format_time(hover_time))
                    self.time_preview_label.adjustSize()
                    global_pos = self.position_slider.mapToGlobal(event.position().toPoint())
                    local_pos = self.mapFromGlobal(global_pos)
                    self.time_preview_label.move(local_pos.x() - self.time_preview_label.width() // 2, local_pos.y() - 35)
                    self.time_preview_label.raise_()
                    self.time_preview_label.show()
            elif event.type() == QEvent.Type.Leave:
                self.time_preview_label.hide()
                
        if obj == self.volume_slider and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                val = int((event.position().x() / self.volume_slider.width()) * self.volume_slider.maximum())
                self.volume_slider.setValue(val)
                self.handle_volume_drag(val)

        if obj == self.video_frame and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self.toggle_play()
                return True

        if event.type() == QEvent.Type.MouseMove:
            self.show_controls()
            
        if event.type() == QEvent.Type.ApplicationDeactivate:
            if not self.loading_overlay.isHidden():
                self.loading_overlay.hide()
            if not self.state_overlay.isHidden():
                self.state_overlay.hide()
                
        if event.type() == QEvent.Type.KeyPress and self.window().isActiveWindow() and self.isVisible():
            if event.key() == Qt.Key.Key_Space:
                self.toggle_play()
                return True
            elif event.key() == Qt.Key.Key_Right:
                self.seek_relative(5000)
                return True
            elif event.key() == Qt.Key.Key_Left:
                self.seek_relative(-5000)
                return True
            
        return super().eventFilter(obj, event)

    def seek_relative(self, ms_offset):
        if not self.player:
            return
        current_time = self.player.get_time()
        length = self.player.get_length()
        if current_time >= 0 and length > 0:
            new_time = max(0, min(current_time + ms_offset, length))
            self.set_position(new_time)
            self.position_slider.setValue(int((new_time / length) * self.position_slider.maximum()))
            self.time_label.setText(f"{self.format_time(new_time)} / {self.format_time(length)}")

    def show_controls(self):
        self.top_bar.show()
        self.bottom_bar.show()
        self.hide_timer.start()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def hide_controls(self):
        if self.is_playing:
            self.top_bar.hide()
            self.bottom_bar.hide()
            self.setCursor(Qt.CursorShape.BlankCursor)

    def play(self):
        self.player.play()
        self.ui_timer.start()
        self.play_button.setText(self.pause_icon)
        self.is_playing = True
        self.hide_timer.start()

    def pause(self):
        self.player.pause()
        self.play_button.setText(self.play_icon)
        self.is_playing = False
        self.show_controls()

    def toggle_play(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()
        self.state_overlay.show_state(self.is_playing)

    def set_position(self, position):
        self.player.set_position(position / 1000.0)
        self.show_controls()
        
        self.is_seeking = True
        self.stuck_ticks = 0
        self.seek_wait_ticks = 0

    def update_ui(self):
        state = self.player.get_state()
        current_time = self.player.get_time()
        
        time_changed = (current_time != getattr(self, 'last_time', -1))
        
        # Check if video is stuck (time is not changing while it should be playing)
        if self.is_playing and not time_changed:
            self.stuck_ticks = getattr(self, 'stuck_ticks', 0) + 1
        else:
            self.stuck_ticks = 0
            
        self.last_time = current_time

        # If stuck for > 2 ticks (400ms) and playing, it's buffering!
        is_stuck = self.is_playing and self.stuck_ticks > 2
        
        if getattr(self, 'is_seeking', False):
            self.seek_wait_ticks = getattr(self, 'seek_wait_ticks', 0) + 1
            # Must wait at least 400ms (2 ticks), must not be stuck, must be Playing, and time must actually advance
            if self.seek_wait_ticks > 2 and not is_stuck and state == vlc.State.Playing and time_changed:
                self.is_seeking = False

        is_active = self.window().isActiveWindow()
        
        if not is_active and not self.state_overlay.isHidden():
            self.state_overlay.hide()

        if state in [vlc.State.Opening, vlc.State.Buffering] or getattr(self, 'is_seeking', False) or is_stuck:
            if self.loading_overlay.isHidden() and self.isVisible() and is_active:
                self.loading_overlay.update_position()
                self.loading_overlay.show()
            elif not is_active and not self.loading_overlay.isHidden():
                self.loading_overlay.hide()
                
            self.show_controls()
            self.hide_timer.stop()
        else:
            if not self.loading_overlay.isHidden():
                self.loading_overlay.hide()
                self.hide_timer.start()
        
        if not self.is_playing:
            return
            
        pos = self.player.get_position()
        if pos >= 0 and not self.position_slider.isSliderDown():
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(int(pos * 1000))
            self.position_slider.blockSignals(False)
        
        time_ms = self.player.get_time()
        length_ms = self.player.get_length()
        if time_ms >= 0 and length_ms > 0:
            self.time_label.setText(f"{self.format_time(time_ms)} / {self.format_time(length_ms)}")

    def format_time(self, ms):
        s = round(ms / 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def close_player(self):
        from PySide6.QtWidgets import QApplication
        if QApplication.instance():
            QApplication.instance().removeEventFilter(self)
            
        self.ui_timer.stop()
        self.hide_timer.stop()
        self.loading_overlay.close()
        self.loading_overlay.deleteLater()
        self.state_overlay.close()
        self.state_overlay.deleteLater()
        
        player = self.player
        instance = self.instance
        media = self.media
        
        self.player = None
        self.instance = None
        self.media = None
        
        def _cleanup_vlc(p, i, m):
            try:
                if sys.platform.startswith('linux'):
                    p.set_xwindow(0)
                elif sys.platform == "win32":
                    p.set_hwnd(0)
                elif sys.platform == "darwin":
                    p.set_nsobject(0)
                    
                p.stop()
                
                del p
                del m
                del i
            except Exception as e:
                logger.error(f"Error releasing VLC resources: {e}")

        cleanup_thread = threading.Thread(target=_cleanup_vlc, args=(player, instance, media))
        cleanup_thread.daemon = True
        cleanup_thread.start()
        
        self.closed.emit()
