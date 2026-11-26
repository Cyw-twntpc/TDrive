import sqlite3
import logging
import base64
import os
import shutil
import threading
from typing import Dict, Optional

from core_app.core.settings_manager import SettingsManager
from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)

MAX_THUMBS_CACHE_SIZE = 200 * 1024 * 1024  # 200 MB
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024    # 512 MB disk reserve watermark


class ThumbnailManager:
    """
    Manages the local on-disk thumbnail database (local_thumbs.db).
    Handles thread-safe CRUD, LRU eviction, cloud package import/export, and folder-level queries.
    Preview image caching (in-memory LRU) lives in preview.image.viewer.ImagePreviewer.
    """

    def __init__(
        self,
        db_path: str = "./file/user_data/local_thumbs.db",
        max_cache_bytes: int = MAX_THUMBS_CACHE_SIZE,
    ):
        if db_path in (
            "./file/user_data/local_thumbs.db",
            "file/user_data/local_thumbs.db",
            os.path.abspath("./file/user_data/local_thumbs.db"),
            os.path.abspath("file/user_data/local_thumbs.db"),
        ):
            db_path = os.path.join(SettingsManager().settings_dir, "local_thumbs.db")
        self.db_path = db_path
        self._max_cache_bytes = max_cache_bytes
        self._lock = threading.RLock()
        self._db_handler = DatabaseConnection()
        self.conn = None
        self._init_local_db()

    def _init_local_db(self):
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS local_thumbs (
                    file_id INTEGER PRIMARY KEY,
                    thumb_data BLOB,
                    last_accessed_at REAL DEFAULT (julianday('now'))
                )
            ''')
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(local_thumbs)").fetchall()}
            if "last_accessed_at" not in cols:
                self.conn.execute("ALTER TABLE local_thumbs ADD COLUMN last_accessed_at REAL")
                self.conn.execute("UPDATE local_thumbs SET last_accessed_at = julianday('now') WHERE last_accessed_at IS NULL")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_local_thumbs_last_accessed ON local_thumbs(last_accessed_at)"
            )
            self.conn.commit()

    def _evict_if_needed_locked(self) -> None:
        """Evict least-recently-used thumbnails when total BLOB size exceeds max_cache_bytes."""
        if not self.conn or self._max_cache_bytes <= 0:
            return
        total = self.conn.execute(
            "SELECT COALESCE(SUM(LENGTH(thumb_data)), 0) FROM local_thumbs"
        ).fetchone()[0]
        evicted = 0
        while total > self._max_cache_bytes:
            row = self.conn.execute(
                "SELECT file_id, COALESCE(LENGTH(thumb_data), 0) FROM local_thumbs ORDER BY last_accessed_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                break
            self.conn.execute("DELETE FROM local_thumbs WHERE file_id = ?", (row[0],))
            total -= row[1]
            evicted += 1
        if evicted > 0:
            self.conn.execute("PRAGMA incremental_vacuum(32)")

    def _ensure_disk_space_locked(self, incoming_bytes: int) -> bool:
        """Ensure sufficient free disk space remains after caching thumbnails; fail-open if low."""
        if not self.conn:
            return False
        cache_dir = os.path.dirname(os.path.abspath(self.db_path))
        evicted = 0
        while True:
            try:
                free_bytes = shutil.disk_usage(cache_dir).free
            except OSError:
                return True
            if free_bytes - incoming_bytes >= MIN_FREE_DISK_BYTES:
                if evicted > 0:
                    self.conn.execute("PRAGMA incremental_vacuum(32)")
                return True
            row = self.conn.execute(
                "SELECT file_id FROM local_thumbs ORDER BY last_accessed_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                logger.warning(
                    "Low disk space (%d free bytes < %d reserve); skipping thumbnail cache write",
                    free_bytes,
                    MIN_FREE_DISK_BYTES,
                )
                return False
            self.conn.execute("DELETE FROM local_thumbs WHERE file_id = ?", (row[0],))
            evicted += 1

    # --- Cloud Package Import / Export ---

    def import_package_bytes(self, db_bytes: bytes):
        """Deserializes a downloaded thumbs package and imports it into the local cache."""
        with self._lock:
            if not self.conn:
                return
            temp_conn = None
            try:
                temp_conn = sqlite3.connect(":memory:")
                temp_conn.deserialize(db_bytes)
                cursor = temp_conn.cursor()
                cursor.execute("SELECT file_id, thumb_data FROM thumbnails")
                rows = cursor.fetchall()

                incoming_bytes = sum(len(r[1]) for r in rows if r[1])
                if not self._ensure_disk_space_locked(incoming_bytes):
                    self.conn.commit()
                    return

                self.conn.executemany(
                    "INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data, last_accessed_at) VALUES (?, ?, julianday('now'))",
                    rows,
                )
                self._evict_if_needed_locked()
                self.conn.commit()
                logger.info(f"Imported {len(rows)} thumbnails into local cache.")
            except Exception as e:
                logger.error(f"Failed to import package bytes: {e}")
            finally:
                if temp_conn is not None:
                    temp_conn.close()

    def build_package_bytes(self, file_ids: list) -> Optional[bytes]:
        """Builds a thumbs.db package containing the specified file_ids for upload."""
        if not file_ids:
            return None
        with self._lock:
            if not self.conn:
                return None
            temp_conn = None
            try:
                temp_conn = sqlite3.connect(":memory:")
                cursor = temp_conn.cursor()
                cursor.execute('''
                    CREATE TABLE thumbnails (
                        file_id INTEGER PRIMARY KEY,
                        thumb_data BLOB
                    )
                ''')

                local_cursor = self.conn.cursor()
                rows = []
                for i in range(0, len(file_ids), 900):
                    batch = file_ids[i:i + 900]
                    placeholders = ",".join("?" for _ in batch)
                    local_cursor.execute(
                        f"SELECT file_id, thumb_data FROM local_thumbs WHERE file_id IN ({placeholders})", batch
                    )
                    rows.extend(local_cursor.fetchall())

                cursor.executemany("INSERT INTO thumbnails (file_id, thumb_data) VALUES (?, ?)", rows)
                temp_conn.commit()

                return temp_conn.serialize()
            except Exception as e:
                logger.error(f"Failed to build package bytes: {e}")
                return None
            finally:
                if temp_conn is not None:
                    temp_conn.close()

    # --- Local CRUD ---

    def save_thumbnails(self, new_thumbnails: Dict[int, bytes]):
        """Saves newly generated thumbnails to local cache."""
        if not new_thumbnails:
            return
        with self._lock:
            if not self.conn:
                return
            try:
                incoming_bytes = sum(len(blob) for blob in new_thumbnails.values() if blob)
                if not self._ensure_disk_space_locked(incoming_bytes):
                    self.conn.commit()
                    return
                data_to_insert = [(fid, blob) for fid, blob in new_thumbnails.items()]
                self.conn.executemany(
                    "INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data, last_accessed_at) VALUES (?, ?, julianday('now'))",
                    data_to_insert,
                )
                self._evict_if_needed_locked()
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error saving local thumbs: {e}")

    def get_thumbnails(self, file_ids: list) -> Dict[str, str]:
        """Gets base64 thumbnails for the given file_ids from the local cache."""
        if not file_ids:
            return {}
        with self._lock:
            if not self.conn:
                return {}
            try:
                result = {}
                hit_ids = []
                cursor = self.conn.cursor()
                for i in range(0, len(file_ids), 900):
                    batch = file_ids[i:i + 900]
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"SELECT file_id, thumb_data FROM local_thumbs WHERE file_id IN ({placeholders})", batch
                    )
                    for fid, blob in cursor.fetchall():
                        if blob:
                            result[str(fid)] = base64.b64encode(blob).decode('utf-8')
                            hit_ids.append(fid)
                if hit_ids:
                    for i in range(0, len(hit_ids), 900):
                        batch = hit_ids[i:i + 900]
                        placeholders = ",".join("?" for _ in batch)
                        self.conn.execute(
                            f"UPDATE local_thumbs SET last_accessed_at = julianday('now') WHERE file_id IN ({placeholders})",
                            batch,
                        )
                    self.conn.commit()
                return result
            except Exception as e:
                logger.error(f"Error reading local thumbs: {e}")
                return {}

    def delete_thumbnails(self, file_ids: list[int]):
        """Delete thumbnails for permanently deleted files from local cache."""
        if not file_ids:
            return
        with self._lock:
            if not self.conn:
                return
            try:
                deleted = 0
                for i in range(0, len(file_ids), 900):
                    batch = file_ids[i:i + 900]
                    placeholders = ",".join("?" for _ in batch)
                    cur = self.conn.execute(
                        f"DELETE FROM local_thumbs WHERE file_id IN ({placeholders})", batch
                    )
                    deleted += cur.rowcount or 0
                if deleted > 0:
                    self.conn.execute("PRAGMA incremental_vacuum(32)")
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error deleting local thumbs: {e}")

    # --- Backward Compatibility Methods ---

    def get_folder_thumbnails(self, folder_id: int) -> Dict[str, str]:
        try:
            conn = self._db_handler._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT file_id FROM file_folder_map WHERE folder_id = ?", (folder_id,))
            file_ids = [row['file_id'] for row in cur.fetchall()]
            return self.get_thumbnails(file_ids)
        except Exception as e:
            logger.error(f"Fallback get_folder_thumbnails failed: {e}")
            return {}

    def has_db(self, folder_id: int) -> bool:
        return True

    def load_thumbs_db_from_bytes(self, folder_id: int, db_bytes: bytes):
        self.import_package_bytes(db_bytes)

    def update_thumbs_db(self, folder_id: int, new_thumbnails: Dict[int, bytes]) -> Optional[bytes]:
        self.save_thumbnails(new_thumbnails)
        return self.get_serialized_db(folder_id)

    def get_serialized_db(self, folder_id: int) -> Optional[bytes]:
        try:
            conn = self._db_handler._get_conn()
            cur = conn.cursor()
            cur.execute('''
                SELECT file_id FROM file_folder_map WHERE folder_id = ?
                UNION
                SELECT id as file_id FROM files WHERE thumb_src_folder_id = ?
            ''', (folder_id, folder_id))
            file_ids = [row['file_id'] for row in cur.fetchall()]
            return self.build_package_bytes(file_ids)
        except Exception as e:
            logger.error(f"Fallback get_serialized_db failed: {e}")
            return None

    def close(self):
        """Closes the local thumbnail database connection safely under lock."""
        with self._lock:
            try:
                if hasattr(self, 'conn') and self.conn:
                    self.conn.close()
                    self.conn = None
            except Exception as e:
                logger.error(f"Error closing thumbnail DB: {e}")

    def reopen(self) -> None:
        """Reopen the local thumbnail database if closed (e.g. same-process re-login)."""
        with self._lock:
            if getattr(self, 'conn', None) is None:
                self._init_local_db()
