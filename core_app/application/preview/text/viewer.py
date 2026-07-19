import logging
import gzip
import asyncio
from collections import OrderedDict

logger = logging.getLogger(__name__)

LANGUAGE_MAP = {
    'py': 'python', 'js': 'javascript', 'ts': 'typescript',
    'html': 'html', 'css': 'css', 'json': 'json', 'xml': 'xml',
    'yaml': 'yaml', 'yml': 'yaml', 'ini': 'ini', 'md': 'markdown',
    'sh': 'bash', 'bat': 'dos', 'ps1': 'powershell', 'sql': 'sql',
    'c': 'c', 'cpp': 'cpp', 'h': 'c', 'java': 'java', 'go': 'go',
    'rs': 'rust', 'rb': 'ruby', 'php': 'php', 'r': 'r', 'swift': 'swift',
    'kt': 'kotlin', 'scala': 'scala', 'lua': 'lua', 'pl': 'perl',
    'perl': 'perl', 'tex': 'latex', 'txt': 'plaintext', 'log': 'plaintext',
    'cfg': 'ini', 'conf': 'ini', 'env': 'plaintext', 'gitignore': 'plaintext',
    'dockerfile': 'dockerfile',
}

TEXT_EXTENSIONS = set(LANGUAGE_MAP.keys()) | {'dockerfile'}

WINDOW_SIZE = 256 * 1024       # 256 KB per window


def _detect_language(file_name: str) -> str:
    ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
    return LANGUAGE_MAP.get(ext, 'plaintext')


def _decode_window(data: bytes) -> str:
    """Detect encoding per-window via chardet, fallback to utf-8."""
    try:
        import chardet
        enc = chardet.detect(data)['encoding']
        if enc:
            return data.decode(enc, errors='replace')
    except Exception:
        pass
    return data.decode('utf-8', errors='replace')


class TextPreviewer:
    """Two-tier text preview provider.

    Tier 1: In-memory LRU (256 MB) — full file bytes, uncompressed.
    Tier 2: PreviewCacheManager (SQLite) — full file bytes, gzip compressed.

    Each window request reads from the full file bytes and decodes independently.
    """

    def __init__(self, preview_cache, download_full_file):
        self._memory_cache: OrderedDict[int, bytes] = OrderedDict()
        self._memory_cache_max_bytes = 256 * 1024 * 1024
        self._memory_cache_bytes = 0
        self._preview_cache = preview_cache
        self._download_full_file = download_full_file
        self._page_indexes: dict[int, list[int]] = {}
        self._encodings: dict[int, str] = {}

    def _add_to_memory_cache(self, content_id: int, data: bytes):
        if content_id in self._memory_cache:
            self._memory_cache_bytes -= len(self._memory_cache[content_id])
            
        size = len(data)
        while self._memory_cache_bytes + size > self._memory_cache_max_bytes and self._memory_cache:
            oldest_id, oldest_data = self._memory_cache.popitem(last=False)
            self._memory_cache_bytes -= len(oldest_data)
            self._page_indexes.pop(oldest_id, None)
            self._encodings.pop(oldest_id, None)
            logger.debug(f"Evicted content_id={oldest_id} from memory cache ({len(oldest_data)} bytes)")
        self._memory_cache[content_id] = data
        self._memory_cache.move_to_end(content_id)
        self._memory_cache_bytes += size

    async def _get_file_bytes(self, content_id: int, file_size: int, file_hash: str) -> bytes:
        # Tier 1: Memory cache
        if content_id in self._memory_cache:
            self._memory_cache.move_to_end(content_id)
            return self._memory_cache[content_id]

        # Tier 2: SQLite cache (gzip compressed)
        row = self._preview_cache.get(content_id)
        if row:
            data = gzip.decompress(row['data'])
            logger.debug(f"SQLite cache hit for content_id={content_id} ({len(data)} bytes)")
            self._add_to_memory_cache(content_id, data)
            return data

        # Download full file from Telegram
        logger.info(f"Downloading full file content_id={content_id} from Telegram")
        data = await self._download_full_file(content_id, file_size, file_hash)
        self._preview_cache.put(content_id, data, 'text', file_hash, file_size)
        self._add_to_memory_cache(content_id, data)
        return data

    async def _build_page_index(self, content_id: int, data: bytes):
        if content_id in self._page_indexes and content_id in self._encodings:
            return

        def _compute():
            enc = 'utf-8'
            try:
                import chardet
                # Use a larger sample for better detection
                sample_size = min(len(data), 5 * 1024 * 1024)
                res = chardet.detect(data[:sample_size])
                if res and res['encoding']:
                    enc = res['encoding']
                    
                # Fix encoding issues for pagination where BOM is lost in subsequent pages
                enc_lower = enc.lower()
                if enc_lower == 'ascii':
                    enc = 'utf-8'
                elif enc_lower == 'utf-16':
                    if data.startswith(b'\xff\xfe'):
                        enc = 'utf-16le'
                    elif data.startswith(b'\xfe\xff'):
                        enc = 'utf-16be'
                    else:
                        enc = 'utf-16le'
                elif enc_lower == 'utf-32':
                    if data.startswith(b'\xff\xfe\x00\x00'):
                        enc = 'utf-32le'
                    elif data.startswith(b'\x00\x00\xfe\xff'):
                        enc = 'utf-32be'
                    else:
                        enc = 'utf-32le'
            except:
                pass
            
            enc_lower = enc.lower()
            if 'utf-16le' in enc_lower:
                nl_bytes = b'\x0a\x00'
            elif 'utf-16be' in enc_lower:
                nl_bytes = b'\x00\x0a'
            elif 'utf-32le' in enc_lower:
                nl_bytes = b'\x0a\x00\x00\x00'
            else:
                nl_bytes = b'\n'

            offsets = [0]
            pos = 0
            lines = 0
            LINES_PER_PAGE = 100
            
            MAX_PAGE_BYTES = 512 * 1024
            
            while True:
                next_pos = data.find(nl_bytes, pos)
                if next_pos == -1:
                    next_pos = len(data)
                else:
                    next_pos += len(nl_bytes)
                
                while next_pos - offsets[-1] > MAX_PAGE_BYTES:
                    offsets.append(offsets[-1] + MAX_PAGE_BYTES)
                    
                if next_pos == len(data):
                    break
                    
                lines += 1
                if lines % LINES_PER_PAGE == 0 and offsets[-1] != next_pos:
                    offsets.append(next_pos)
                
                pos = next_pos
                
            return enc, offsets

        enc, offsets = await asyncio.to_thread(_compute)
        self._encodings[content_id] = enc
        self._page_indexes[content_id] = offsets

    async def get_preview(self, file_info: dict) -> dict:
        content_id = file_info['content_id']
        file_size = int(file_info['size'])
        file_hash = file_info['file_hash']
        lang = _detect_language(file_info['name'])

        full_data = await self._get_file_bytes(content_id, file_size, file_hash)
        
        is_code = lang != 'plaintext'
        MB = 1024 * 1024
        
        if is_code and file_size > MB:
            pos = full_data.rfind(b'\n', 0, MB)
            if pos == -1: pos = MB
            else: pos += 1
            window_data = full_data[:pos]
            text = _decode_window(window_data)
            return {
                'url': None,
                'content': text,
                'total_pages': 1,
                'language': lang,
                'file_name': file_info['name'],
                'is_code': True
            }
        elif is_code and file_size <= MB:
            window_data = full_data
            text = _decode_window(window_data)
            return {
                'url': None,
                'content': text,
                'total_pages': 1,
                'language': lang,
                'file_name': file_info['name'],
                'is_code': True
            }
            
        await self._build_page_index(content_id, full_data)
        
        total_pages = max(1, len(self._page_indexes[content_id]) - 1)
        start_byte = self._page_indexes[content_id][0]
        end_byte = self._page_indexes[content_id][1] if len(self._page_indexes[content_id]) > 1 else len(full_data)
        
        window_data = full_data[start_byte:end_byte]
        enc = self._encodings.get(content_id, 'utf-8')
        text = window_data.decode(enc, errors='replace')
        
        return {
            'url': None,
            'content': text,
            'total_pages': total_pages,
            'language': lang,
            'file_name': file_info['name'],
            'is_code': False
        }

    async def get_text_page(self, page_index: int, file_info: dict) -> dict:
        content_id = file_info['content_id']
        file_size = int(file_info['size'])
        file_hash = file_info['file_hash']

        full_data = await self._get_file_bytes(content_id, file_size, file_hash)
        
        if content_id not in self._page_indexes or content_id not in self._encodings:
            await self._build_page_index(content_id, full_data)
            
        offsets = self._page_indexes[content_id]
        if page_index < 0 or page_index >= len(offsets) - 1:
            return {'content': '', 'page_index': page_index, 'is_last': True}
            
        start_byte = offsets[page_index]
        end_byte = offsets[page_index + 1]
        window_data = full_data[start_byte:end_byte]
        enc = self._encodings.get(content_id, 'utf-8')
        text = window_data.decode(enc, errors='replace')
        
        is_last = (page_index >= len(offsets) - 2)
        
        return {
            'content': text,
            'page_index': page_index,
            'is_last': is_last
        }
