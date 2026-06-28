import logging
from typing import Optional
from PySide6.QtGui import QImage

from core_app.application.file_system.thumbnail.image_generator import ImageThumbnailGenerator

logger = logging.getLogger(__name__)


class VideoThumbnailGenerator:
    """
    Extracts a single frame from a video file and generates a 400px JPG thumbnail.
    Uses OpenCV for frame capture, then PySide6 for thumbnail scaling.
    """

    @staticmethod
    def generate(file_path: str) -> Optional[bytes]:
        """
        Captures a frame at 10% position of the video and returns a 400px JPG thumbnail.
        Returns None if the video cannot be read or OpenCV is not installed.
        """
        try:
            import cv2
            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                return None

            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            target_frame = int(frame_count * 0.1) if frame_count > 0 else 30

            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()

            cap.release()

            if not ret or frame is None:
                return None

            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)

            return ImageThumbnailGenerator._generate_thumbnail(qimg)

        except ImportError:
            return None
        except Exception as e:
            logger.error(f"Error generating video thumbnail for {file_path}: {e}", exc_info=True)
            return None
