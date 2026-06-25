import os
import re
import zipfile
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def extract_metadata(file_path: str) -> Dict[str, Any]:
    """
    Extracts advanced metadata from a local file.
    Currently extracts basic stats, and conditionally uses PIL/cv2 for images/videos.
    """
    meta = {}
    ext = os.path.splitext(file_path)[1].lower()

    # Images
    if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif']:
        try:
            from PIL import Image, ExifTags
            with Image.open(file_path) as img:
                meta['res'] = f"{img.width}x{img.height}"
                if img.format:
                    meta['fmt'] = img.format
                if img.mode:
                    meta['mode'] = img.mode
                
                try:
                    exif = img.getexif()
                    if exif:
                        for k, v in exif.items():
                            tag = ExifTags.TAGS.get(k, k)
                            if tag == 'Make':
                                meta['cam_mk'] = str(v).strip().replace('\x00', '')
                            elif tag == 'Model':
                                meta['cam_mod'] = str(v).strip().replace('\x00', '')
                            elif tag == 'ISOSpeedRatings':
                                meta['iso'] = str(v)
                except Exception as e:
                    logger.debug(f"Failed to extract EXIF metadata: {e}")
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Failed to extract image meta for {file_path}: {e}")

    # Videos
    elif ext in ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm', '.flv']:
        try:
            import cv2
            cap = cv2.VideoCapture(file_path)
            if cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                
                if width > 0 and height > 0:
                    meta['res'] = f"{width}x{height}"
                if fps > 0:
                    meta['fps'] = round(fps, 2)
                    if frame_count > 0:
                        duration_sec = frame_count / fps
                        # Format duration to mm:ss or hh:mm:ss
                        m, s = divmod(int(duration_sec), 60)
                        h, m = divmod(m, 60)
                        if h > 0:
                            meta['dur'] = f"{h}:{m:02d}:{s:02d}"
                        else:
                            meta['dur'] = f"{m}:{s:02d}"
            cap.release()
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Failed to extract video meta for {file_path}: {e}")

    # Audio
    elif ext in ['.mp3', '.wav', '.flac', '.ogg', '.m4a', '.wma', '.aac']:
        try:
            from pydub.utils import mediainfo
            info = mediainfo(file_path)
            if info:
                duration_sec = float(info.get('duration', 0))
                if duration_sec > 0:
                    m, s = divmod(int(duration_sec), 60)
                    h, m = divmod(m, 60)
                    if h > 0:
                        meta['dur'] = f"{h}:{m:02d}:{s:02d}"
                    else:
                        meta['dur'] = f"{m}:{s:02d}"
                
                bitrate = info.get('bit_rate')
                if bitrate:
                    meta['bit'] = f"{int(bitrate)//1000} kbps"
                
                tags = info.get('TAG', {})
                artist = tags.get('artist') or tags.get('ARTIST')
                if artist:
                    meta['auth'] = artist
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Failed to extract audio meta for {file_path}: {e}")

    # PDF Documents (.pdf)
    elif ext == '.pdf':
        try:
            import pypdf
            with open(file_path, 'rb') as f:
                reader = pypdf.PdfReader(f)
                meta['pg_cnt'] = str(len(reader.pages))
                info = reader.metadata
                if info and info.author:
                    meta['auth'] = info.author
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Failed to extract pdf meta for {file_path}: {e}")

    # Legacy Office (.doc, .xls, .ppt)
    elif ext in ['.doc', '.xls', '.ppt']:
        try:
            import olefile
            if olefile.isOleFile(file_path):
                with olefile.OleFileIO(file_path) as ole:
                    meta_data = ole.get_metadata()
                    if meta_data.author:
                        author_val = meta_data.author
                        meta['auth'] = author_val.decode('utf-8', errors='ignore') if isinstance(author_val, bytes) else str(author_val)
                    if meta_data.num_pages:
                        meta['pg_cnt'] = str(meta_data.num_pages)
                    if meta_data.num_words:
                        meta['w_cnt'] = str(meta_data.num_words)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Failed to extract ole meta for {file_path}: {e}")

    # Modern Office (.docx, .xlsx, .pptx)
    elif ext in ['.docx', '.xlsx', '.pptx']:
        try:
            with zipfile.ZipFile(file_path, 'r') as z:
                if 'docProps/app.xml' in z.namelist():
                    app_xml = z.read('docProps/app.xml').decode('utf-8', errors='ignore')
                    w_match = re.search(r'<Words>(\d+)</Words>', app_xml)
                    if w_match:
                        meta['w_cnt'] = w_match.group(1)
                    p_match = re.search(r'<(?:Pages|Slides)>(\d+)</(?:Pages|Slides)>', app_xml)
                    if p_match:
                        meta['pg_cnt'] = p_match.group(1)
                
                if 'docProps/core.xml' in z.namelist():
                    core_xml = z.read('docProps/core.xml').decode('utf-8', errors='ignore')
                    a_match = re.search(r'<dc:creator>(.*?)</dc:creator>', core_xml)
                    if a_match and a_match.group(1):
                        meta['auth'] = a_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to extract office meta for {file_path}: {e}")

    # OpenDocument (.odt, .ods, .odp)
    elif ext in ['.odt', '.ods', '.odp']:
        try:
            with zipfile.ZipFile(file_path, 'r') as z:
                if 'meta.xml' in z.namelist():
                    meta_xml = z.read('meta.xml').decode('utf-8', errors='ignore')
                    a_match = re.search(r'<dc:creator>(.*?)</dc:creator>', meta_xml)
                    if a_match and a_match.group(1):
                        meta['auth'] = a_match.group(1)
                    p_match = re.search(r'meta:page-count="(\d+)"', meta_xml)
                    if p_match:
                        meta['pg_cnt'] = p_match.group(1)
                    w_match = re.search(r'meta:word-count="(\d+)"', meta_xml)
                    if w_match:
                        meta['w_cnt'] = w_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to extract opendocument meta for {file_path}: {e}")

    # Pure Text Documents (.txt, .md)
    elif ext in ['.txt', '.md']:
        try:
            char_count = 0
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                while True:
                    chunk = f.read(1024 * 1024) # 1MB chunks
                    if not chunk:
                        break
                    char_count += len(chunk.replace(" ", "").replace("\n", "").replace("\r", ""))
            if char_count > 0:
                meta['w_cnt'] = str(char_count)
        except Exception as e:
            logger.warning(f"Failed to extract document meta for {file_path}: {e}")

    return meta
