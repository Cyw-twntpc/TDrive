import logging
from typing import Optional
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt

logger = logging.getLogger(__name__)


class ImageThumbnailGenerator:
    """
    Generates 400px JPG thumbnails from image files entirely in memory.
    Uses PySide6.QtGui for image processing.
    """

    @staticmethod
    def generate(file_path: str) -> Optional[bytes]:
        """
        Reads an image file and returns a 400px JPG thumbnail.
        Returns None if the image cannot be read.
        """
        try:
            reader = QImageReader(file_path)
            reader.setAutoTransform(True)

            if not reader.canRead():
                logger.warning(f"Cannot read image: {file_path}")
                del reader
                return None

            image = reader.read()
            del reader

            if image.isNull():
                logger.warning(f"Failed to load image: {file_path}")
                del image
                return None

            result = ImageThumbnailGenerator._generate_thumbnail(image)
            del image
            return result

        except Exception as e:
            logger.error(f"Error generating thumbnail for {file_path}: {e}", exc_info=True)
            return None

    @staticmethod
    def _generate_thumbnail(image: QImage) -> Optional[bytes]:
        try:
            if image.width() > 400 or image.height() > 400:
                thumb_img = image.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                thumb_img = image

            return ImageThumbnailGenerator._qimage_to_bytes(thumb_img, "JPG", quality=80)
        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}")
            return None

    @staticmethod
    def _qimage_to_bytes(image: QImage, format_str: str, quality: int = -1) -> bytes:
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, format_str, quality)
        return byte_array.data()
