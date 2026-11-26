import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core_app.application.file_system.preview_cache_manager import PreviewCacheManager
from core_app.application.preview.image.viewer import ImagePreviewer
from core_app.application.preview.text.viewer import TextPreviewer
from core_app.core import utils
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
from core_app.infrastructure.telegram import telegram_comms

if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState
    from ..thumbnail.manager import ThumbnailManager

logger = logging.getLogger(__name__)


class PreviewService:
    """Coordinates thumbnails, image/text/file previews, full downloads, and cache eviction."""

    def __init__(
        self,
        shared_state: 'SharedState',
        thumb_manager: 'ThumbnailManager',
        buffer=None,
        preview_cache=None,
    ):
        self.shared_state = shared_state
        self.thumb_manager = thumb_manager
        self.buffer = buffer
        self.preview_cache = preview_cache or PreviewCacheManager()
        self.previewer = ImagePreviewer()
        self._active_preview_downloads: Dict[int, asyncio.Future] = {}
        self._active_doc_downloads: Dict[tuple, asyncio.Future] = {}
        self.text_previewer = TextPreviewer(self.preview_cache, self.download_full_file)

    async def get_thumbnails(self, folder_id: int, return_file_id_keys: bool = False) -> Dict[str, Any]:
        """Return base64 thumbnails for a folder, pulling remote thumb packages when missing."""
        try:
            def _get_folder_files_info():
                db = DatabaseConnection()
                conn = db._get_conn()
                cur = conn.cursor()
                cur.execute('''
                    SELECT m.id as map_id, m.file_id, f.thumb_src_folder_id, f.has_thumb
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id 
                    WHERE m.folder_id = ?
                ''', (folder_id,))
                return cur.fetchall()

            files_info = await asyncio.to_thread(_get_folder_files_info)
            if not files_info:
                return {"success": True, "thumbnails": {}}

            file_ids = [row['file_id'] for row in files_info]
            id_map = {row['file_id']: row['map_id'] for row in files_info}
            thumbs = await asyncio.to_thread(self.thumb_manager.get_thumbnails, file_ids)

            expecting_thumb_file_ids = {row['file_id'] for row in files_info if row['has_thumb'] == 1}
            missing_file_ids = expecting_thumb_file_ids - {int(k) for k in thumbs.keys()}
            if missing_file_ids:
                await self._download_missing_thumb_packages(folder_id, files_info, missing_file_ids)
                thumbs = await asyncio.to_thread(self.thumb_manager.get_thumbnails, file_ids)

            mapped = thumbs if return_file_id_keys else self._map_thumbnails_to_map_ids(thumbs, id_map)
            logger.debug(f"PreviewService returning {len(mapped)} thumbnails for {folder_id} (Mapped IDs)")
            return {"success": True, "thumbnails": mapped}
        except Exception as e:
            logger.error(f"Error fetching thumbnails: {e}", exc_info=True)
            return {"success": False, "thumbnails": {}}

    async def _download_missing_thumb_packages(
        self, folder_id: int, files_info, missing_file_ids: set[int]
    ) -> None:
        """Fetch remote folder thumbnail packages for file_ids absent from local SQLite cache."""
        needed_folders = {
            row['thumb_src_folder_id'] if row['thumb_src_folder_id'] else folder_id
            for row in files_info if row['file_id'] in missing_file_ids
        }
        if not needed_folders:
            return

        def _get_package_cloud_info(f_ids):
            db = DatabaseConnection()
            conn = db._get_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in f_ids)
            cur.execute(f"SELECT id, thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id IN ({placeholders})", list(f_ids))
            return cur.fetchall()

        cloud_packages = await asyncio.to_thread(_get_package_cloud_info, needed_folders)
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            return

        for pkg in cloud_packages:
            if pkg['thumbs_db_msg_id'] and pkg['thumbs_db_msg_id'] != 0:
                logger.debug(f"Downloading thumbs package for folder {pkg['id']} (msg_id: {pkg['thumbs_db_msg_id']})...")
                g_idx, raw_id = utils.decode_global_msg_id(pkg['thumbs_db_msg_id'])
                target_gid = self.shared_state.get_telegram_group_id(g_idx)
                db_bytes = await telegram_comms.download_data_as_bytes(
                    client, target_gid, [raw_id], pkg['thumbs_db_hash']
                )
                if db_bytes:
                    await asyncio.to_thread(self.thumb_manager.import_package_bytes, db_bytes)

    @staticmethod
    def _map_thumbnails_to_map_ids(thumbs: Dict[str, str], id_map: Dict[int, int]) -> Dict[str, str]:
        """Translate content file_id thumbnail map into frontend file_folder_map.id keys."""
        mapped_thumbs: Dict[str, str] = {}
        for content_id_str, b64_data in thumbs.items():
            try:
                content_id = int(content_id_str)
                if content_id in id_map:
                    mapped_thumbs[str(id_map[content_id])] = b64_data
            except ValueError:
                logger.debug("Skipping invalid content_id: %s", content_id_str)
        return mapped_thumbs

    def _get_file_preview_info_sync(self, map_id: int):
        """Read file preview message coordinates and content hash synchronously from main_db."""
        db = DatabaseConnection()
        FileRepository(db)
        FolderRepository(db)
        TrashRepository(db)
        conn = db._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT f.preview_msg_id, f.preview_hash, f.id as content_id, f.hash as file_hash, f.size
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            WHERE m.id = ?
        """, (map_id,))
        return cur.fetchone()

    async def _download_preview_payload(self, client, map_id: int, info) -> Optional[bytes]:
        """Download image preview blob or fall back to small/single-chunk original download."""
        content_id = info['content_id']
        if info['preview_msg_id']:
            logger.debug(f"Downloading preview for file map {map_id} (content {content_id})...")
            g_idx, raw_id = utils.decode_global_msg_id(info['preview_msg_id'])
            target_gid = self.shared_state.get_telegram_group_id(g_idx)
            return await telegram_comms.download_data_as_bytes(
                client, target_gid, [raw_id], info['preview_hash']
            )

        logger.debug(f"No preview found for {map_id}, attempting fallback to original file...")
        try:
            chunks = await self.shared_state.metadata_manager.get_file_chunks(
                client, self.shared_state.api_id, content_id
            )
            if chunks and len(chunks) == 1:
                g_idx, raw_id = utils.decode_global_msg_id(chunks[0][1])
                target_gid = self.shared_state.get_telegram_group_id(g_idx)
                return await telegram_comms.download_data_as_bytes(
                    client, target_gid, [raw_id], info['file_hash']
                )
            if chunks and len(chunks) > 1 and (info['size'] or 0) <= 32 * 1024 * 1024:
                return await self.download_full_file(content_id, info['size'], info['file_hash'])
        except Exception as fb_e:
            logger.warning(f"Fallback preview download failed: {fb_e}")
        return None

    async def _cache_and_encode_preview(self, content_id: int, file_hash: str, preview_bytes: bytes) -> str:
        """Populate Tier-1 memory LRU and Tier-2 disk SQLite caches, then return base64 string."""
        self.previewer.cache_preview(content_id, preview_bytes)
        if self.preview_cache:
            await asyncio.to_thread(
                self.preview_cache.put,
                content_id,
                preview_bytes,
                'image_preview',
                file_hash,
                len(preview_bytes),
                None,
                False,
            )
        return await asyncio.to_thread(lambda d: base64.b64encode(d).decode('utf-8'), preview_bytes)

    async def get_preview(self, map_id: int) -> Dict[str, Any]:
        """Resolve an image preview from memory LRU, SQLite disk cache, or Telegram with dedup."""
        try:
            info = await asyncio.to_thread(self._get_file_preview_info_sync, map_id)
            if not info:
                logger.error("File not found")
                return {"success": False}

            content_id = info['content_id']
            preview_b64 = self.previewer.get_cached_preview(content_id)
            if preview_b64:
                return {"success": True, "preview": preview_b64}

            if self.preview_cache:
                cached = await asyncio.to_thread(
                    self.preview_cache.get, content_id, info['file_hash'], ('image_preview',)
                )
                if cached and cached.get('data'):
                    self.previewer.cache_preview(content_id, cached['data'])
                    b64_str = await asyncio.to_thread(
                        lambda d: base64.b64encode(d).decode('utf-8'), cached['data']
                    )
                    return {"success": True, "preview": b64_str}

            if content_id in self._active_preview_downloads:
                try:
                    shared_b64 = await self._active_preview_downloads[content_id]
                    return {"success": True, "preview": shared_b64} if shared_b64 else {"success": False}
                except Exception as e:
                    logger.debug(f"In-flight preview download wait failed for content_id={content_id}: {e}")

            download_future = asyncio.get_running_loop().create_future()
            self._active_preview_downloads[content_id] = download_future

            try:
                client = await utils.ensure_client_connected(self.shared_state)
                if not client:
                    logger.error("Client not connected")
                    if not download_future.done():
                        download_future.set_result(None)
                    return {"success": False}

                preview_bytes = await self._download_preview_payload(client, map_id, info)
                if preview_bytes:
                    b64_str = await self._cache_and_encode_preview(content_id, info['file_hash'], preview_bytes)
                    if not download_future.done():
                        download_future.set_result(b64_str)
                    return {"success": True, "preview": b64_str}

                logger.error("Download failed")
                if not download_future.done():
                    download_future.set_result(None)
                return {"success": False}
            except Exception as exc:
                if not download_future.done():
                    download_future.set_exception(exc)
                raise
            finally:
                self._active_preview_downloads.pop(content_id, None)
        except Exception as e:
            logger.error(f"Error fetching preview: {e}", exc_info=True)
            return {"success": False}

    async def download_full_file(self, content_id: int, file_size: int, file_hash: str) -> bytes:
        """Download and decrypt all chunks belonging to content_id in storage-group order."""
        client = await utils.ensure_client_connected(self.shared_state)
        if not client:
            raise ConnectionError("Client not connected")
        chunks = await self.shared_state.metadata_manager.get_file_chunks(
            client, self.shared_state.api_id, content_id
        )
        if not chunks:
            raise FileNotFoundError(f"No chunks found for content_id={content_id}")

        groups_batches = []
        current_target = None
        current_ids = []
        for chunk in chunks:
            g_idx, raw_id = utils.decode_global_msg_id(chunk[1])
            target_gid = self.shared_state.get_telegram_group_id(g_idx)
            if target_gid != current_target:
                if current_ids:
                    groups_batches.append((current_target, current_ids))
                current_target = target_gid
                current_ids = [raw_id]
            else:
                current_ids.append(raw_id)
        if current_ids:
            groups_batches.append((current_target, current_ids))

        logger.debug(f"download_full_file: grouped {len(chunks)} chunks into {len(groups_batches)} batches for content_id={content_id}")
        final_bytes = bytearray()
        for target_gid, raw_ids in groups_batches:
            data_part = await telegram_comms.download_data_as_bytes(
                client, target_gid, raw_ids, file_hash
            )
            if data_part is None:
                logger.error(f"download_full_file: download failed for group={target_gid}, content_id={content_id}")
                raise RuntimeError(f"Download failed for content_id={content_id}")
            final_bytes.extend(data_part)

        return bytes(final_bytes)

    async def get_file_info(self, map_id: int) -> dict:
        """Resolve content_id, size, hash, and filename for a file_folder_map row."""
        def _sync_query():
            db = DatabaseConnection()
            conn = db._get_conn()
            cur = conn.cursor()
            cur.execute('''
                SELECT f.id as content_id, f.size, f.hash, m.name
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                WHERE m.id = ?
            ''', (map_id,))
            row = cur.fetchone()
            if not row:
                raise FileNotFoundError(f"File map_id={map_id} not found")
            return {
                'content_id': row[0],
                'size': row[1],
                'file_hash': row[2],
                'name': row[3],
            }
        return await asyncio.to_thread(_sync_query)

    async def get_file_extended_details(self, map_id: int) -> Dict[str, Any]:
        """Fetch extra media/document metadata stored inside the Telegram map JSON."""
        try:
            def _get_db_info():
                db = DatabaseConnection()
                conn = db._get_conn()
                cur = conn.cursor()
                cur.execute("""
                    SELECT f.map_msg_id, f.id as real_file_id 
                    FROM file_folder_map m 
                    JOIN files f ON m.file_id = f.id 
                    WHERE m.id = ?
                """, (map_id,))
                return cur.fetchone()

            row = await asyncio.to_thread(_get_db_info)
            if not row or not row['map_msg_id']:
                return {"success": False}

            client = await utils.ensure_client_connected(self.shared_state)
            if not client:
                return {"success": False}

            map_data = await self.shared_state.metadata_manager.fetch_map_file(
                client, self.shared_state.api_id, row['map_msg_id']
            )
            real_key = str(row['real_file_id'])
            if real_key in map_data and isinstance(map_data[real_key], dict) and 'm' in map_data[real_key]:
                return {"success": True, "metadata": map_data[real_key]['m']}
            return {"success": False}
        except Exception as e:
            logger.error(f"Error fetching extended details for map_id {map_id}: {e}", exc_info=True)
            return {"success": False}

    async def invalidate_deleted_caches(self, deletion_results: List[Dict[str, Any]]) -> None:
        """Evict memory and disk preview/thumbnail/stream caches for orphaned deleted files."""
        orphan_ids = list({
            res['file_id'] for res in (deletion_results or [])
            if isinstance(res, dict) and res.get('orphan') and res.get('file_id')
        })
        if not orphan_ids:
            return
        for fid in orphan_ids:
            if getattr(self, 'previewer', None):
                self.previewer.invalidate(fid)
            if getattr(self, 'text_previewer', None):
                self.text_previewer.invalidate(fid)
            if self.buffer and hasattr(self.buffer, 'invalidate_file'):
                self.buffer.invalidate_file(fid)
        if self.preview_cache:
            await asyncio.to_thread(self.preview_cache.delete_many, orphan_ids)
        if self.thumb_manager and hasattr(self.thumb_manager, 'delete_thumbnails'):
            await asyncio.to_thread(self.thumb_manager.delete_thumbnails, orphan_ids)

    def evict_idle_memory_caches(self, max_idle_seconds: float = 300.0) -> int:
        """Evict idle image and text entries from in-memory caches after inactivity."""
        evicted = 0
        if getattr(self, 'previewer', None) and hasattr(self.previewer, 'evict_idle'):
            evicted += self.previewer.evict_idle(max_idle_seconds)
        if getattr(self, 'text_previewer', None) and hasattr(self.text_previewer, 'evict_idle'):
            evicted += self.text_previewer.evict_idle(max_idle_seconds)
        return evicted

    def clear_memory_caches(self) -> None:
        """Release all in-memory preview caches on logout or shutdown."""
        if getattr(self, 'previewer', None):
            self.previewer.clear_cache()
        if getattr(self, 'text_previewer', None):
            self.text_previewer.clear_cache()
