import sqlite3
import logging
import base64
from typing import Dict, Optional
from collections import OrderedDict

from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)

class LRUCache:
    def __init__(self, capacity_mb: int = 200):
        self.capacity_bytes = capacity_mb * 1024 * 1024
        self.current_size = 0
        self.cache = OrderedDict() # file_id -> bytes

    def get(self, key: int) -> Optional[bytes]:
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: int, value: bytes):
        if key in self.cache:
            self.current_size -= len(self.cache[key])
            self.cache.move_to_end(key)
        
        self.cache[key] = value
        self.current_size += len(value)
        
        # Evict if needed
        while self.current_size > self.capacity_bytes and self.cache:
            _, evicted_val = self.cache.popitem(last=False)
            self.current_size -= len(evicted_val)

class GalleryManager:
    """
    Manages global local disk thumbnail database and preview image caching.
    """
    def __init__(self, db_path: str = "./file/local_thumbs.db"):
        self.db_path = db_path
        self._db_handler = DatabaseConnection()
        self._init_local_db()
        # file_id -> preview bytes
        self._preview_cache = LRUCache(capacity_mb=200) 

    def _init_local_db(self):
        import os
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

    def import_package_bytes(self, db_bytes: bytes):
        """Deserializes a downloaded thumbs package and imports it into the local cache."""
        try:
            temp_conn = sqlite3.connect(":memory:")
            temp_conn.deserialize(db_bytes)
            cursor = temp_conn.cursor()
            cursor.execute("SELECT file_id, thumb_data FROM thumbnails")
            rows = cursor.fetchall()
            
            self.conn.executemany("INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data) VALUES (?, ?)", rows)
            self.conn.commit()
            temp_conn.close()
            logger.info(f"Imported {len(rows)} thumbnails into local cache.")
        except Exception as e:
            logger.error(f"Failed to import package bytes: {e}")

    def build_package_bytes(self, file_ids: list) -> Optional[bytes]:
        """Builds a thumbs.db package containing the specified file_ids for upload."""
        if not file_ids: return None
        try:
            temp_conn = sqlite3.connect(":memory:")
            cursor = temp_conn.cursor()
            cursor.execute('''
                CREATE TABLE thumbnails (
                    file_id INTEGER PRIMARY KEY,
                    thumb_data BLOB
                )
            ''')
            
            placeholders = ",".join("?" for _ in file_ids)
            local_cursor = self.conn.cursor()
            local_cursor.execute(f"SELECT file_id, thumb_data FROM local_thumbs WHERE file_id IN ({placeholders})", file_ids)
            rows = local_cursor.fetchall()
            
            cursor.executemany("INSERT INTO thumbnails (file_id, thumb_data) VALUES (?, ?)", rows)
            temp_conn.commit()
            
            return temp_conn.serialize()
        except Exception as e:
            logger.error(f"Failed to build package bytes: {e}")
            return None

    def save_thumbnails(self, new_thumbnails: Dict[int, bytes]):
        """Saves newly generated thumbnails to local cache."""
        try:
            data_to_insert = [(fid, blob) for fid, blob in new_thumbnails.items()]
            self.conn.executemany("INSERT OR REPLACE INTO local_thumbs (file_id, thumb_data) VALUES (?, ?)", data_to_insert)
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error saving local thumbs: {e}")

    def get_thumbnails(self, file_ids: list) -> Dict[str, str]:
        """Gets base64 thumbnails for the given file_ids from the local cache."""
        if not file_ids: return {}
        try:
            placeholders = ",".join("?" for _ in file_ids)
            cursor = self.conn.cursor()
            cursor.execute(f"SELECT file_id, thumb_data FROM local_thumbs WHERE file_id IN ({placeholders})", file_ids)
            rows = cursor.fetchall()
            
            result = {}
            for fid, blob in rows:
                if blob:
                    result[str(fid)] = base64.b64encode(blob).decode('utf-8')
            return result
        except Exception as e:
            logger.error(f"Error reading local thumbs: {e}")
            return {}

    # --- Backward Compatibility Methods for Phase 1 Migration ---

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

    def close_folder_db(self, folder_id: int):
        pass # No longer needed

    def has_db(self, folder_id: int) -> bool:
        return True # Trigger local reads

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

    # --- Preview Cache Management ---

    def cache_preview(self, file_id: int, image_bytes: bytes):
        self._preview_cache.put(file_id, image_bytes)

    def get_cached_preview(self, file_id: int) -> Optional[str]:
        data = self._preview_cache.get(file_id)
        if data:
            return base64.b64encode(data).decode('utf-8')
        return None
