import asyncio
import logging
import io
from collections import OrderedDict
from typing import Dict, Tuple

from core_app.infrastructure.telegram import telegram_comms
from core_app.infrastructure.local_fs import file_processor as fp
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
from core_app.infrastructure.database.main_db.repositories.map_repository import MapRepository

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
        self._db = DatabaseConnection()
        FileRepository(self._db)
        FolderRepository(self._db)
        TrashRepository(self._db)
        MapRepository(self._db)
        
        # Locks for concurrent access to same chunk
        self._locks: Dict[str, asyncio.Lock] = {}
        
        # Tracking for dynamic readahead
        self._active_readaheads: Dict[Tuple[int, int], asyncio.Task] = {}
        self._last_chunk_requested: Dict[int, int] = {}

    def _cancel_readaheads(self, file_id: int):
        """Cancels all pending readahead tasks for a specific file to free up bandwidth."""
        keys_to_cancel = [k for k in self._active_readaheads.keys() if k[0] == file_id]
        for k in keys_to_cancel:
            task = self._active_readaheads[k]
            if not task.done():
                task.cancel()
                logger.debug(f"Seek detected: Cancelled readahead task for chunk {k[1]}")
            del self._active_readaheads[k]

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

        # Seek Detection: Cancel old readaheads if we jumped more than 1 chunk
        last_requested = self._last_chunk_requested.get(file_id, -1)
        if last_requested != -1 and abs(start_chunk_idx - last_requested) > 1:
            self._cancel_readaheads(file_id)
        
        self._last_chunk_requested[file_id] = end_chunk_idx

        for chunk_idx in range(start_chunk_idx, end_chunk_idx + 1):
            chunk_data = await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)
            
            # Calculate intersection of requested range and current chunk
            chunk_start = chunk_idx * self.chunk_size
            chunk_start + len(chunk_data)
            
            # Intersection relative to chunk
            slice_start = max(0, current_read_pos - chunk_start)
            slice_end = min(len(chunk_data), end_offset - chunk_start)
            
            if slice_start < slice_end:
                buffer.write(chunk_data[slice_start:slice_end])
                current_read_pos += (slice_end - slice_start)

            # Trigger dynamic readahead (depth = 2)
            for offset in range(1, 3):
                target_chunk = chunk_idx + offset
                cache_key = (file_id, target_chunk)
                
                # Only schedule if it's not already cached and not currently downloading
                if cache_key not in self._cache and cache_key not in self._active_readaheads:
                    task = asyncio.create_task(self._readahead(file_id, target_chunk, file_hash, chunk_map))
                    self._active_readaheads[cache_key] = task

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
                logger.warning(f"Chunk {chunk_idx+1} not found in map for file {file_id}. Map keys: {list(chunk_map.keys())}")
                return b""

            client = self.shared_state.client
            if not client:
                raise ConnectionError("Telegram client not connected")

            logger.debug(f"Downloading chunk {chunk_idx+1} for file {file_id} (Msg: {msg_id})")
            
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
            self._active_readaheads.pop(cache_key, None)
            return # Already cached

        try:
            # Check if this chunk actually exists
            if (chunk_idx + 1) not in chunk_map:
                return

            await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)
        except asyncio.CancelledError:
            pass # Task intentionally cancelled by seek detection
        except Exception as e:
            logger.debug(f"Readahead failed for chunk {chunk_idx}: {e}")
        finally:
            self._active_readaheads.pop(cache_key, None)

    async def _get_chunk_map(self, file_id: int) -> Dict[int, int]:
        """
        Returns { part_num: message_id } for the given file_id via MetadataManager.
        """
        if not self.shared_state.metadata_manager:
            logger.error("MetadataManager not initialized in SharedState.")
            return {}

        client = self.shared_state.client
        if not client:
            return {}

        try:
            chunks = await self.shared_state.metadata_manager.get_file_chunks(
                client, self.shared_state.group_id, self.shared_state.api_id, file_id
            )
            if not chunks:
                logger.warning(f"_get_chunk_map: No chunks returned for file_id {file_id} from MetadataManager.")

            # chunks is list of [part_num, message_id, part_hash]
            return {c[0]: c[1] for c in chunks}
        except Exception as e:
            logger.error(f"Failed to get chunk map for file {file_id}: {e}")
            return {}
