import sqlite3
import logging
import base64
import os
from typing import Dict, Optional

from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)


class ThumbnailManager:
    """
    Manages the local on-disk thumbnail database (local_thumbs.db).
    Handles CRUD, cloud package import/export, and backward-compatible folder-level queries.
    Preview image caching (in-memory LRU) lives in preview.image.viewer.ImagePreviewer.
    """

    def __init__(self, db_path: str = "./file/user_data/local_thumbs.db"):
        self.db_path = db_path
        self._db_handler = DatabaseConnection()
        self._init_local_db()

    def _init_local_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS local_thumbs (
                file_id INTEGER PRIMARY KEY,
                thumb_data BLOB
            )
        ''')
        self.conn.commit()

    # --- Cloud Package Import / Export ---

    def import_package_bytes(self, db_bytes: bytes):
        """Deserializes a downloaded thumbs package and imports it into the local cache."""
        try:
            temp_conn = sqlite3.connect(":memory:")
            temp_conn.deserialize(db_bytes)
            cursor = temp_conn.cursor()
            cursor.execute("SELECT file_id, thumb_data FROM thumbnails")
            rows = cursor.fetchall()

            self.conn.executemany(
                "INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data) VALUES (?, ?)", rows
            )
            self.conn.commit()
            temp_conn.close()
            logger.info(f"Imported {len(rows)} thumbnails into local cache.")
        except Exception as e:
            logger.error(f"Failed to import package bytes: {e}")

    def build_package_bytes(self, file_ids: list) -> Optional[bytes]:
        """Builds a thumbs.db package containing the specified file_ids for upload."""
        if not file_ids:
            return None
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

    # --- Local CRUD ---

    def save_thumbnails(self, new_thumbnails: Dict[int, bytes]):
        """Saves newly generated thumbnails to local cache."""
        try:
            data_to_insert = [(fid, blob) for fid, blob in new_thumbnails.items()]
            self.conn.executemany(
                "INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data) VALUES (?, ?)", data_to_insert
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error saving local thumbs: {e}")

    def get_thumbnails(self, file_ids: list) -> Dict[str, str]:
        """Gets base64 thumbnails for the given file_ids from the local cache."""
        if not file_ids:
            return {}
        try:
            result = {}
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
            return result
        except Exception as e:
            logger.error(f"Error reading local thumbs: {e}")
            return {}

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
        """Closes the local thumbnail database connection."""
        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.close()
                self.conn = None
        except Exception as e:
            logger.error(f"Error closing thumbnail DB: {e}")
