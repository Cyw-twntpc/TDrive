import math
import logging

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QLinearGradient, QFont, QRadialGradient

from core_app.core.settings_manager import get_localized_text
from core_app.core.utils import get_resource_path

logger = logging.getLogger(__name__)


class SplashScreen(QWidget):
    """Frameless Aurora Slate Glass splash window displayed during startup."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(420, 280)

        # Animation states
        self.light_position = 0.0
        self.gradient_offset = 0.0

        self.light_timer = QTimer(self)
        self.light_timer.timeout.connect(self.update_light)
        self.light_timer.start(20)

        self.gradient_timer = QTimer(self)
        self.gradient_timer.timeout.connect(self.update_gradient)
        self.gradient_timer.start(50)

        # Prefer dark-theme light wordmark on the deep Aurora Slate card
        logo_path = get_resource_path("web/title_dark.png")
        if not logo_path.exists():
            logo_path = get_resource_path("web/title.png")
        self.logo = QPixmap(str(logo_path))
        if not self.logo.isNull():
            self.logo = self.logo.scaled(220, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            logger.warning("Splash screen logo not found or invalid.")

        self._status_text = get_localized_text("native.initializing", "正在初始化...")
        self.fade_in()

    def fade_in(self):
        """Animate the splash screen fade in effect."""
        self.opacity_effect = QGraphicsOpacityEffect()
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)

        self.fade_value = 0.0
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self.update_fade)
        self.fade_timer.start(20)

    def update_fade(self):
        """Update fade in animation."""
        self.fade_value += 0.06
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
        """Update ambient glow drift."""
        self.gradient_offset += 0.006
        if self.gradient_offset > 1.0:
            self.gradient_offset = 0.0
        self.update()

    def paintEvent(self, event):
        """Draw the Aurora Slate Glass splash card with ambient cerulean glows and comet progress."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        bg_rect = QRectF(self.rect().adjusted(20, 20, -20, -20))

        # 1. Soft outer shadow
        painter.setBrush(QColor(2, 6, 23, 110))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bg_rect.adjusted(0, 4, 0, 6), 22, 22)

        # 2. Deep Aurora Slate card background
        card_grad = QLinearGradient(bg_rect.topLeft(), bg_rect.bottomRight())
        card_grad.setColorAt(0.0, QColor("#0F172A"))
        card_grad.setColorAt(0.55, QColor("#131C31"))
        card_grad.setColorAt(1.0, QColor("#0B1120"))
        painter.setBrush(QBrush(card_grad))
        painter.drawRoundedRect(bg_rect, 22, 22)

        # 3. Subtle drifting cerulean & sky ambient glows clipped to card
        painter.save()
        painter.setClipRect(bg_rect)
        drift_x = math.sin(self.gradient_offset * math.pi * 2) * 18.0
        drift_y = math.cos(self.gradient_offset * math.pi * 2) * 12.0

        glow1_center = QPointF(bg_rect.right() - 55 + drift_x, bg_rect.top() + 35 + drift_y)
        glow1 = QRadialGradient(glow1_center, 175)
        glow1.setColorAt(0.0, QColor(59, 130, 246, 62))
        glow1.setColorAt(0.6, QColor(37, 99, 235, 22))
        glow1.setColorAt(1.0, QColor(15, 23, 42, 0))
        painter.setBrush(QBrush(glow1))
        painter.drawEllipse(glow1_center, 175, 175)

        glow2_center = QPointF(bg_rect.left() + 55 - drift_x, bg_rect.bottom() - 30 - drift_y)
        glow2 = QRadialGradient(glow2_center, 160)
        glow2.setColorAt(0.0, QColor(14, 165, 233, 46))
        glow2.setColorAt(1.0, QColor(15, 23, 42, 0))
        painter.setBrush(QBrush(glow2))
        painter.drawEllipse(glow2_center, 160, 160)
        painter.restore()

        # 4. Crisp inner glass highlight border
        border_grad = QLinearGradient(bg_rect.topLeft(), bg_rect.bottomLeft())
        border_grad.setColorAt(0.0, QColor(255, 255, 255, 42))
        border_grad.setColorAt(1.0, QColor(255, 255, 255, 16))
        painter.setPen(QPen(QBrush(border_grad), 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(bg_rect.adjusted(0.6, 0.6, -0.6, -0.6), 22, 22)

        # 5. Logo + subtle backdrop halo
        if not self.logo.isNull():
            logo_x = int((self.width() - self.logo.width()) / 2)
            logo_y = 74

            halo_center = QPointF(self.width() / 2.0, logo_y + self.logo.height() / 2.0)
            halo = QRadialGradient(halo_center, 110)
            halo.setColorAt(0.0, QColor(59, 130, 246, 40))
            halo.setColorAt(1.0, QColor(59, 130, 246, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(halo))
            painter.drawEllipse(halo_center, 110, 110)

            painter.drawPixmap(logo_x, logo_y, self.logo)

        # 6. Sleek 4px progress track with cerulean comet
        progress_width = 190
        progress_height = 4
        progress_x = (self.width() - progress_width) / 2.0
        progress_y = 202.0

        track_rect = QRectF(progress_x, progress_y, progress_width, progress_height)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(track_rect, 2.0, 2.0)

        comet_length = 64.0
        light_center = progress_x + progress_width * self.light_position

        painter.save()
        painter.setClipRect(track_rect)
        tail_grad = QLinearGradient(light_center - comet_length, 0, light_center, 0)
        tail_grad.setColorAt(0.0, QColor(37, 99, 235, 0))
        tail_grad.setColorAt(0.55, QColor(59, 130, 246, 190))
        tail_grad.setColorAt(1.0, QColor(96, 165, 250, 255))
        painter.setBrush(QBrush(tail_grad))
        painter.drawRoundedRect(
            QRectF(light_center - comet_length, progress_y, comet_length, progress_height), 2.0, 2.0
        )

        head_w = 18.0
        head_grad = QLinearGradient(light_center - head_w / 2.0, 0, light_center + head_w / 2.0, 0)
        head_grad.setColorAt(0.0, QColor(96, 165, 250, 140))
        head_grad.setColorAt(0.5, QColor(255, 255, 255, 255))
        head_grad.setColorAt(1.0, QColor(96, 165, 250, 140))
        painter.setBrush(QBrush(head_grad))
        painter.drawRoundedRect(
            QRectF(light_center - head_w / 2.0, progress_y, head_w, progress_height), 2.0, 2.0
        )
        painter.restore()

        # 7. Crisp muted status label
        painter.setPen(QColor("#94A3B8"))
        painter.setFont(QFont("Segoe UI Variable", 9, QFont.Normal))
        text_rect = QRectF(0, 218, self.width(), 24)
        painter.drawText(text_rect, Qt.AlignCenter, self._status_text)

    def closeEvent(self, event):
        """Clean up timers when closing."""
        self.light_timer.stop()
        self.gradient_timer.stop()
        if hasattr(self, 'fade_timer'):
            self.fade_timer.stop()
        super().closeEvent(event)
