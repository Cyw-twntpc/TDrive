import logging
import base64
import time
from typing import Optional
from collections import OrderedDict
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt

logger = logging.getLogger(__name__)


class LRUCache:
    def __init__(self, capacity_mb: int = 100):
        self.capacity_bytes = capacity_mb * 1024 * 1024
        self.current_size = 0
        self.cache = OrderedDict()
        self._timestamps: dict[int, float] = {}

    def get(self, key: int) -> Optional[bytes]:
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        self._timestamps[key] = time.monotonic()
        return self.cache[key]

    def put(self, key: int, value: bytes):
        if len(value) > self.capacity_bytes:
            return
        if key in self.cache:
            self.current_size -= len(self.cache[key])
            self.cache.move_to_end(key)

        self.cache[key] = value
        self._timestamps[key] = time.monotonic()
        self.current_size += len(value)

        while self.current_size > self.capacity_bytes and self.cache:
            evicted_key, evicted_val = self.cache.popitem(last=False)
            self._timestamps.pop(evicted_key, None)
            self.current_size -= len(evicted_val)

    def delete(self, key: int):
        """Remove a single cached item if present and update current byte count."""
        val = self.cache.pop(key, None)
        self._timestamps.pop(key, None)
        if val is not None:
            self.current_size = max(0, self.current_size - len(val))

    def evict_idle(self, max_idle_seconds: float) -> int:
        """Evict cached items that have not been read within max_idle_seconds."""
        now = time.monotonic()
        stale_keys = [
            k for k, ts in self._timestamps.items()
            if (now - ts) >= max_idle_seconds
        ]
        for k in stale_keys:
            self.delete(k)
        return len(stale_keys)

    def clear(self):
        """Clear all cached items from memory."""
        self.cache.clear()
        self._timestamps.clear()
        self.current_size = 0


class ImagePreviewer:
    """
    Generates 1080p JPG previews from image files.
    Maintains an in-memory LRU cache of preview bytes keyed by content_id.
    """

    def __init__(self):
        self._preview_cache = LRUCache(capacity_mb=100)

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

    def cache_preview(self, content_id: int, image_bytes: bytes):
        self._preview_cache.put(content_id, image_bytes)

    def get_cached_preview(self, content_id: int) -> Optional[str]:
        data = self._preview_cache.get(content_id)
        if data:
            return base64.b64encode(data).decode('utf-8')
        return None

    def invalidate(self, content_id: int):
        """Invalidate cached image preview for a deleted file."""
        self._preview_cache.delete(content_id)

    def evict_idle(self, max_idle_seconds: float = 300.0) -> int:
        """Evict idle image previews older than max_idle_seconds from memory."""
        return self._preview_cache.evict_idle(max_idle_seconds)

    def clear_cache(self):
        """Release all in-memory image previews."""
        self._preview_cache.clear()
