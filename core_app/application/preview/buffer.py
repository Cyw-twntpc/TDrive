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

        # Track active downloads to prevent duplicate requests and memory leaks
        self._active_downloads: Dict[str, asyncio.Future] = {}

        # Tracking for dynamic readahead
        self._active_readaheads: Dict[Tuple[int, int], asyncio.Task] = {}
        self._last_chunk_requested: Dict[int, int] = {}

        # Lock to protect shared mutable state across coroutines
        self._state_lock = asyncio.Lock()

    async def read(self, file_id: int, offset: int, length: int, file_size: int, file_hash: str) -> bytes:
        """
        Reads a range of bytes from the virtual file.
        Automatically downloads and decrypts necessary chunks.
        """
        # Guard: DB stores size as REAL, ensure int arithmetic
        file_size = int(file_size)
        offset = int(offset)
        length = int(length)

        if length <= 0:
            return b""

        if offset >= file_size:
            return b""

        # Calculate start and end chunk indices
        start_chunk_idx = offset // self.chunk_size
        end_offset = min(offset + length, file_size)
        end_chunk_idx = (end_offset - 1) // self.chunk_size

        buffer = io.BytesIO()
        current_read_pos = offset

        # Retrieve chunks information from DB
        chunk_map = await self._get_chunk_map(file_id)
        if not chunk_map:
            logger.warning(f"StreamBuffer.read: chunk_map is empty for file_id={file_id}")

        # Seek Detection: Cancel old readaheads if we jumped more than 1 chunk
        async with self._state_lock:
            last_requested = self._last_chunk_requested.get(file_id, -1)
            if last_requested != -1 and abs(start_chunk_idx - last_requested) > 1:
                keys_to_cancel = [k for k in self._active_readaheads.keys() if k[0] == file_id]
                for k in keys_to_cancel:
                    task = self._active_readaheads[k]
                    if not task.done():
                        task.cancel()
                    del self._active_readaheads[k]

        async with self._state_lock:
            self._last_chunk_requested[file_id] = end_chunk_idx

        for chunk_idx in range(start_chunk_idx, end_chunk_idx + 1):
            chunk_data = await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)

            # Calculate intersection of requested range and current chunk
            chunk_start = chunk_idx * self.chunk_size

            # Intersection relative to chunk
            slice_start = max(0, current_read_pos - chunk_start)
            slice_end = min(len(chunk_data), end_offset - chunk_start)

            if slice_start < slice_end:
                buffer.write(chunk_data[slice_start:slice_end])
                current_read_pos += (slice_end - slice_start)

            # Trigger dynamic readahead (depth = 2)
            for ra_offset in range(1, 3):
                target_chunk = chunk_idx + ra_offset
                cache_key = (file_id, target_chunk)

                # Only schedule if it's not already cached and not currently downloading
                if cache_key not in self._cache and cache_key not in self._active_readaheads:
                    task = asyncio.create_task(self._readahead(file_id, target_chunk, file_hash, chunk_map))
                    async with self._state_lock:
                        self._active_readaheads[cache_key] = task

        result = buffer.getvalue()
        return result

    async def _get_chunk(self, file_id: int, chunk_idx: int, file_hash: str, chunk_map: Dict[int, int]) -> bytes:
        cache_key = (file_id, chunk_idx)

        # 1. Check Memory Cache
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        # Use a Future to prevent multiple downloads of the same chunk without leaking memory
        lock_key = f"{file_id}_{chunk_idx}"
        if lock_key in self._active_downloads:
            # Wait for the already-in-progress download to finish
            try:
                data = await self._active_downloads[lock_key]
                if data:
                    return data
            except Exception:
                pass
            # Download failed — fall through to retry ourselves

        # Create a new Future for this download
        loop = asyncio.get_running_loop()
        download_future = loop.create_future()
        self._active_downloads[lock_key] = download_future

        success = False
        try:
            # 2. Download and Decrypt
            msg_id = chunk_map.get(chunk_idx + 1) # Part nums are 1-based in DB
            if not msg_id:
                raise KeyError(f"Chunk {chunk_idx+1} not found in map for file {file_id}")

            client = self.shared_state.client
            if not client:
                raise ConnectionError("Telegram client not connected")

            decrypted_data = await telegram_comms.download_data_as_bytes(
                client, self.shared_state.group_id, [msg_id], file_hash
            )

            if not decrypted_data:
                raise IOError(f"Failed to download chunk {chunk_idx+1} for file {file_id}")

            # 3. Update Cache
            await self._add_to_cache(cache_key, decrypted_data)
            success = True
            download_future.set_result(decrypted_data)
            return decrypted_data

        except Exception as e:
            if not download_future.done():
                download_future.set_exception(e)
            raise

        finally:
            self._active_downloads.pop(lock_key, None)

    async def _add_to_cache(self, key, data):
        async with self._state_lock:
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
        async with self._state_lock:
            if cache_key in self._cache:
                self._active_readaheads.pop(cache_key, None)
                return # Already cached

        try:
            # Check if this chunk actually exists
            if (chunk_idx + 1) not in chunk_map:
                async with self._state_lock:
                    self._active_readaheads.pop(cache_key, None)
                return

            await self._get_chunk(file_id, chunk_idx, file_hash, chunk_map)
        except asyncio.CancelledError:
            pass # Task intentionally cancelled by seek detection
        except Exception:
            pass
        finally:
            async with self._state_lock:
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
            logger.warning(f"_get_chunk_map: client not connected for file_id={file_id}")
            return {}

        try:
            chunks = await self.shared_state.metadata_manager.get_file_chunks(
                client, self.shared_state.group_id, self.shared_state.api_id, file_id
            )
            if not chunks:
                logger.warning(f"_get_chunk_map: get_file_chunks returned empty for file_id={file_id}")

            # chunks is list of [part_num, message_id, part_hash]
            result = {c[0]: c[1] for c in chunks}
            return result
        except Exception as e:
            logger.error(f"Failed to get chunk map for file {file_id}: {e}")
            return {}
