import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QLinearGradient, QFont, QRadialGradient

logger = logging.getLogger(__name__)

class SplashScreen(QWidget):
    def __init__(self):
        super().__init__()
        # Set window properties: frameless, stay on top, and translucent background
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(420, 280)

        # Animation states
        self.light_position = 0.0
        self.gradient_offset = 0.0
        
        # Timer for light animation
        self.light_timer = QTimer(self)
        self.light_timer.timeout.connect(self.update_light)
        self.light_timer.start(20)
        
        # Timer for background gradient animation
        self.gradient_timer = QTimer(self)
        self.gradient_timer.timeout.connect(self.update_gradient)
        self.gradient_timer.start(50)

        # Load logo from the web folder
        self.logo = QPixmap(str(Path("web/title.png").resolve()))
        if not self.logo.isNull():
            # Scale logo to a suitable size
            self.logo = self.logo.scaled(260, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            logger.warning("Splash screen logo (web/title.png) not found or invalid.")
        
        # Fade in animation
        self.fade_in()

    def fade_in(self):
        """Animate the splash screen fade in effect."""
        self.opacity_effect = QGraphicsOpacityEffect()
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        
        # Manual fade in
        self.fade_value = 0.0
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self.update_fade)
        self.fade_timer.start(20)
    
    def update_fade(self):
        """Update fade in animation."""
        self.fade_value += 0.05
        if self.fade_value >= 1.0:
            self.fade_value = 1.0
            self.fade_timer.stop()
        self.opacity_effect.setOpacity(self.fade_value)

    def update_light(self):
        """Update light beam position."""
        self.light_position += 0.015
        if self.light_position > 1.2:
            self.light_position = -0.2
        self.update()
    
    def update_gradient(self):
        """Update background gradient animation."""
        self.gradient_offset += 0.005
        if self.gradient_offset > 1.0:
            self.gradient_offset = 0.0
        self.update()

    def paintEvent(self, event):
        """Custom drawing of the splash screen interface."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # 1. Draw Soft Shadow
        shadow_rect = self.rect().adjusted(20, 20, -20, -20)
        painter.setBrush(QColor(0, 0, 0, 40))  # Slightly darker shadow for contrast
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(shadow_rect.adjusted(4, 4, 4, 4), 24, 24)

        # 2. Draw Background with Animated Gradient (Opaque & Vibrant)
        bg_rect = self.rect().adjusted(20, 20, -20, -20)
        
        # Create flowing gradient
        import math
        offset1 = math.sin(self.gradient_offset * math.pi * 2) * 0.15
        offset2 = math.cos(self.gradient_offset * math.pi * 2) * 0.15
        offset3 = math.sin(self.gradient_offset * math.pi * 2 + math.pi) * 0.15
        
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        
        # Ensure all positions stay within 0.0-1.0 range
        pos1 = max(0.0, min(1.0, 0.0 + offset1))
        pos2 = max(0.0, min(1.0, 0.25 + offset2))
        pos3 = max(0.0, min(1.0, 0.5 + offset3))
        pos4 = max(0.0, min(1.0, 0.75 + offset1))
        pos5 = max(0.0, min(1.0, 1.0 + offset2))
        
        # Vibrant & Opaque colors (Alpha 255)
        gradient.setColorAt(pos1, QColor(255, 210, 240, 255))      # Vibrant pink
        gradient.setColorAt(pos2, QColor(210, 230, 255, 255))      # Vibrant blue
        gradient.setColorAt(pos3, QColor(240, 220, 255, 255))      # Vibrant purple
        gradient.setColorAt(pos4, QColor(220, 240, 255, 255))      # Cyan-blue
        gradient.setColorAt(pos5, QColor(255, 230, 240, 255))      # Pink-white

        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bg_rect, 24, 24)
        
        # 3. Draw Glass Border
        border_gradient = QLinearGradient(0, bg_rect.top(), 0, bg_rect.bottom())
        border_gradient.setColorAt(0, QColor(255, 255, 255, 200))
        border_gradient.setColorAt(0.5, QColor(255, 255, 255, 120))
        border_gradient.setColorAt(1, QColor(200, 230, 255, 150))
        
        border_pen = QPen(QBrush(border_gradient), 1.5)
        painter.setPen(border_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(bg_rect.adjusted(1, 1, -1, -1), 24, 24)

        # 4. Draw Subtle Light Reflection
        reflection_gradient = QLinearGradient(
            bg_rect.left(), bg_rect.top(),
            bg_rect.left() + 200, bg_rect.top() + 200
        )
        reflection_gradient.setColorAt(0, QColor(255, 255, 255, 120))
        reflection_gradient.setColorAt(1, QColor(255, 255, 255, 0))
        
        painter.setBrush(reflection_gradient)
        painter.setPen(Qt.NoPen)
        reflection_rect = QRectF(bg_rect.left(), bg_rect.top(), 160, 120)
        painter.setClipRect(bg_rect)
        painter.drawRoundedRect(reflection_rect, 24, 24)
        painter.setClipping(False)

        # 5. Draw Glow Behind Logo
        if not self.logo.isNull():
            logo_x = (self.width() - self.logo.width()) // 2
            logo_y = 80  # Moved up slightly
            
            glow_center = QPointF(self.width() / 2, logo_y + self.logo.height() / 2)
            glow_gradient = QRadialGradient(glow_center, 120)
            glow_gradient.setColorAt(0, QColor(255, 255, 255, 180))
            glow_gradient.setColorAt(0.6, QColor(200, 220, 255, 80))
            glow_gradient.setColorAt(1, QColor(255, 255, 255, 0))
            
            painter.setBrush(glow_gradient)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(glow_center, 120, 120)

            # 6. Draw Logo
            painter.drawPixmap(logo_x, logo_y, self.logo)

        # 7. Draw Linear Progress Bar with Brighter Comet Effect
        progress_width = 180
        progress_height = 6
        progress_x = (self.width() - progress_width) // 2
        progress_y = 220  # Moved up
        
        # Progress bar background
        track_rect = QRectF(progress_x, progress_y, progress_width, progress_height)
        painter.setBrush(QColor(255, 255, 255, 100))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(track_rect, progress_height/2, progress_height/2)
        
        # Moving comet effect (Deep Blue)
        comet_length = 60
        light_center = progress_x + progress_width * self.light_position
        
        # Draw comet tail (Deep Blue Gradient)
        tail_gradient = QLinearGradient(light_center - comet_length, 0, light_center, 0)
        tail_gradient.setColorAt(0, QColor(0, 50, 150, 0))      # Transparent deep blue
        tail_gradient.setColorAt(0.5, QColor(20, 80, 200, 150)) # Mid deep blue
        tail_gradient.setColorAt(1, QColor(40, 100, 240, 220))  # Bright deep blue
        
        painter.setBrush(QBrush(tail_gradient))
        tail_rect = QRectF(light_center - comet_length, progress_y, comet_length, progress_height)
        painter.setClipRect(track_rect)
        painter.drawRoundedRect(tail_rect, progress_height/2, progress_height/2)
        
        # Draw bright comet head (Deep Blue with White Core)
        head_width = 20
        head_gradient = QLinearGradient(light_center - head_width/2, 0, light_center + head_width/2, 0)
        head_gradient.setColorAt(0, QColor(20, 80, 240, 150))   # Deep blue edge
        head_gradient.setColorAt(0.5, QColor(255, 255, 255, 255)) # Pure white core
        head_gradient.setColorAt(1, QColor(20, 80, 240, 150))   # Deep blue edge
        
        painter.setBrush(QBrush(head_gradient))
        head_rect = QRectF(light_center - head_width/2, progress_y - 1, head_width, progress_height + 2)
        painter.drawRoundedRect(head_rect, (progress_height + 2)/2, (progress_height + 2)/2)
        
        # Glow around the head (Deep Blue Glow)
        glow_radius = 10
        glow_center_point = QPointF(light_center, progress_y + progress_height/2)
        glow_gradient = QRadialGradient(glow_center_point, glow_radius)
        glow_gradient.setColorAt(0, QColor(40, 100, 255, 200))  # Intense blue glow
        glow_gradient.setColorAt(0.5, QColor(20, 60, 200, 100))
        glow_gradient.setColorAt(1, QColor(0, 40, 150, 0))
        
        painter.setBrush(QBrush(glow_gradient))
        painter.drawEllipse(glow_center_point, glow_radius, glow_radius)
        
        painter.setClipping(False)

        # 8. Draw Status Text
        painter.setPen(QColor(60, 80, 110))
        painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Normal))
        text_rect = QRectF(0, 230, self.width(), 25) # Moved up
        painter.drawText(text_rect, Qt.AlignCenter, "正在初始化...")

    def closeEvent(self, event):
        """Clean up timers when closing."""
        self.light_timer.stop()
        self.gradient_timer.stop()
        if hasattr(self, 'fade_timer'):
            self.fade_timer.stop()
        super().closeEvent(event)
