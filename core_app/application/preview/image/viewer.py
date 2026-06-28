import logging
import base64
from typing import Optional
from collections import OrderedDict
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt

logger = logging.getLogger(__name__)


class LRUCache:
    def __init__(self, capacity_mb: int = 200):
        self.capacity_bytes = capacity_mb * 1024 * 1024
        self.current_size = 0
        self.cache = OrderedDict()

    def get(self, key: int) -> Optional[bytes]:
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: int, value: bytes):
        if key in self.cache:
            self.current_size -= len(self.cache[key])
            self.cache.move_to_end(key)

        self.cache[key] = value
        self.current_size += len(value)

        while self.current_size > self.capacity_bytes and self.cache:
            _, evicted_val = self.cache.popitem(last=False)
            self.current_size -= len(evicted_val)


class ImagePreviewer:
    """
    Generates 1080p JPG previews from image files.
    Maintains an in-memory LRU cache of preview bytes keyed by file_id.
    """

    def __init__(self):
        self._preview_cache = LRUCache(capacity_mb=200)

    def generate(self, file_path: str) -> Optional[bytes]:
        """
        Reads an image file and returns a 1080p JPG preview.
        Returns None if the original image is already <= 1080p.
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

            result = self._generate_preview(image)
            del image
            return result

        except Exception as e:
            logger.error(f"Error generating preview for {file_path}: {e}", exc_info=True)
            return None

    def _generate_preview(self, image: QImage) -> Optional[bytes]:
        try:
            if image.width() <= 1920 and image.height() <= 1080:
                return None

            preview_img = image.scaled(1920, 1080, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            return self._qimage_to_bytes(preview_img, "JPG", quality=85)
        except Exception as e:
            logger.error(f"Preview generation failed: {e}")
            return None

    @staticmethod
    def _qimage_to_bytes(image: QImage, format_str: str, quality: int = -1) -> bytes:
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, format_str, quality)
        return byte_array.data()

    def cache_preview(self, file_id: int, image_bytes: bytes):
        self._preview_cache.put(file_id, image_bytes)

    def get_cached_preview(self, file_id: int) -> Optional[str]:
        data = self._preview_cache.get(file_id)
        if data:
            return base64.b64encode(data).decode('utf-8')
        return None
