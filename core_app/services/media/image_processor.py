import logging
from typing import Optional, Tuple
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt

logger = logging.getLogger(__name__)

class ImageProcessor:
    """
    Handles image processing tasks entirely in memory using PySide6.QtGui.
    Focuses on generating thumbnails and 1080p previews without creating temp files.
    """

    @staticmethod
    def process_image(file_path: str) -> Tuple[Optional[bytes], Optional[bytes]]:
        """
        Processes an image file to generate a thumbnail and a preview.
        
        Args:
            file_path: The local path to the image file.
            
        Returns:
            A tuple containing (thumbnail_bytes, preview_bytes).
            - thumbnail_bytes: JPG/WebP bytes, max 200px.
            - preview_bytes: JPG bytes, scaled to 1080p if original > 1080p, else None.
        """
        try:
            reader = QImageReader(file_path)
            reader.setAutoTransform(True) # Handle EXIF orientation
            
            if not reader.canRead():
                logger.warning(f"Cannot read image: {file_path}")
                return None, None

            original_image = reader.read()
            if original_image.isNull():
                logger.warning(f"Failed to load image: {file_path}")
                return None, None

            # 1. Generate Thumbnail (Max 200px)
            thumb_bytes = ImageProcessor._generate_thumbnail(original_image)

            # 2. Generate Preview (Max 1080p)
            preview_bytes = ImageProcessor._generate_preview(original_image)

            return thumb_bytes, preview_bytes

        except Exception as e:
            logger.error(f"Error processing image {file_path}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def _generate_thumbnail(image: QImage) -> Optional[bytes]:
        try:
            # Scale down to max 200x200, keeping aspect ratio
            if image.width() > 200 or image.height() > 200:
                thumb_img = image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                thumb_img = image # Use original if small enough

            return ImageProcessor._qimage_to_bytes(thumb_img, "JPG", quality=70)
        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}")
            return None

    @staticmethod
    def _generate_preview(image: QImage) -> Optional[bytes]:
        try:
            # Check if image is larger than 1080p (1920x1080)
            # We consider 'larger' if either dimension exceeds the box, 
            # but usually we want to fit within 1920x1080.
            if image.width() <= 1920 and image.height() <= 1080:
                return None # No preview needed, original is small enough to serve as preview

            preview_img = image.scaled(1920, 1080, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            return ImageProcessor._qimage_to_bytes(preview_img, "JPG", quality=85)
        except Exception as e:
            logger.error(f"Preview generation failed: {e}")
            return None

    @staticmethod
    def _qimage_to_bytes(image: QImage, format_str: str, quality: int = -1) -> bytes:
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, format_str, quality)
        return  byte_array.data()
