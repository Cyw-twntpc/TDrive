import asyncio
import logging
import io
from collections import OrderedDict
from typing import Dict

from core_app.api import telegram_comms
from core_app.api import file_processor as fp
from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class StreamBuffer:
    """
    Manages buffering and on-demand downloading of encrypted file chunks for streaming.
    Implements LRU caching and thread-safe decryption.
    """
    def __init__(self, shared_state, cache_size_mb: int = 256):
        self.shared_state = shared_state
        self.chunk_size = fp.CHUNK_SIZE # 8MB
        self.cache_capacity = cache_size_mb * 1024 * 1024
        self.current_cache_size = 0
        
        # Cache: (file_id, chunk_index) -> decrypted_bytes
        self._cache = OrderedDict()
        self._db = DatabaseHandler()
        
        # Locks for concurrent access to same chunk
        self._locks: Dict[str, asyncio.Lock] = {}

    async def read(self, file_id: int, offset: int, length: int, file_size: int, file_hash: str) -> bytes:
        """
        Reads a range of bytes from the virtual file.
        Automatically downloads and decrypts necessary chunks.
        """
        if offset >= file_size:
            return b""

        # Calculate start and end chunk indices
        start_chunk_idx = offset // self.chunk_size
        end_offset = min(offset + length, file_size)
        end_chunk_idx = (end_offset - 1) // self.chunk_size

        buffer = io.BytesIO()
        current_read_pos = offset

        # Retrieve chunks information from DB
        # Optimization: We could cache this info map, but for now query is fast enough
        chunk_map = await self._get_chunk_map(file_id)

        for chunk_idx in range(start_chunk_idx, end_chunk_idx + 1):
            chunk_data = await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)
            
            # Calculate intersection of requested range and current chunk
            chunk_start = chunk_idx * self.chunk_size
            chunk_end = chunk_start + len(chunk_data)
            
            # Intersection relative to chunk
            slice_start = max(0, current_read_pos - chunk_start)
            slice_end = min(len(chunk_data), end_offset - chunk_start)
            
            if slice_start < slice_end:
                buffer.write(chunk_data[slice_start:slice_end])
                current_read_pos += (slice_end - slice_start)

            # Trigger readahead for next chunk
            asyncio.create_task(self._readahead(file_id, chunk_idx + 1, file_hash, chunk_map))

        return buffer.getvalue()

    async def _get_chunk(self, file_id: int, chunk_idx: int, file_hash: str, chunk_map: Dict[int, int]) -> bytes:
        cache_key = (file_id, chunk_idx)
        
        # 1. Check Memory Cache
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        # Use a lock to prevent multiple downloads of the same chunk
        lock_key = f"{file_id}_{chunk_idx}"
        if lock_key not in self._locks:
            self._locks[lock_key] = asyncio.Lock()
        
        async with self._locks[lock_key]:
            # Double check after acquiring lock
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key]

            # 2. Download and Decrypt
            msg_id = chunk_map.get(chunk_idx + 1) # Part nums are 1-based in DB
            if not msg_id:
                # If chunk not found in DB (e.g. single part file without chunks entry?), fallback?
                # Actually, our DB structure guarantees chunks table populated.
                # Special case: Small file < 8MB might not be in chunks table if logic differs?
                # But transfer_service always writes to chunks table or we rely on 'files' table preview?
                # Wait, 'files' table is for deduplication. 'chunks' table links file_id to message_ids.
                # We need to query 'chunks' table using file_id (content ID).
                logger.warning(f"Chunk {chunk_idx+1} not found for file {file_id}")
                return b""

            client = self.shared_state.client
            if not client:
                raise ConnectionError("Telegram client not connected")

            logger.debug(f"Downloading chunk {chunk_idx+1} for file {file_id} (Msg: {msg_id})")
            
            # Reuse download_data_as_bytes but here we need Raw Encrypted Bytes first?
            # No, download_data_as_bytes does download + decrypt in memory.
            # We can use it, but we need to handle the specific Chunk Key generation.
            
            # Key Generation Logic:
            # The key is derived from the *original file hash*.
            # cr.generate_key uses file_hash[:32] and file_hash[-32:].
            # This is consistent for all chunks.
            
            decrypted_data = await telegram_comms.download_data_as_bytes(
                client, self.shared_state.group_id, [msg_id], file_hash
            )
            
            if not decrypted_data:
                raise IOError(f"Failed to download chunk {chunk_idx+1}")

            # 3. Update Cache
            self._add_to_cache(cache_key, decrypted_data)
            return decrypted_data

    def _add_to_cache(self, key, data):
        self._cache[key] = data
        self.current_cache_size += len(data)
        self._cache.move_to_end(key)

        # Evict
        while self.current_cache_size > self.cache_capacity and self._cache:
            k, v = self._cache.popitem(last=False)
            self.current_cache_size -= len(v)

    async def _readahead(self, file_id: int, chunk_idx: int, file_hash: str, chunk_map: Dict[int, int]):
        """Preloads the next chunk in background."""
        cache_key = (file_id, chunk_idx)
        if cache_key in self._cache:
            return # Already cached

        try:
            # Check if this chunk actually exists
            if (chunk_idx + 1) not in chunk_map:
                return

            await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)
        except Exception as e:
            logger.debug(f"Readahead failed for chunk {chunk_idx}: {e}")

    async def _get_chunk_map(self, file_id: int) -> Dict[int, int]:
        """
        Returns { part_num: message_id } for the given file_id.
        """
        loop = asyncio.get_running_loop()
        def query():
            conn = self._db._get_conn()
            try:
                cur = conn.cursor()
                # Assuming file_id passed here is the 'files.id' (content ID).
                cur.execute("SELECT part_num, message_id FROM chunks WHERE file_id = ? ORDER BY part_num", (file_id,))
                return {row['part_num']: row['message_id'] for row in cur.fetchall()}
            finally:
                conn.close()
        
        return await loop.run_in_executor(None, query)
