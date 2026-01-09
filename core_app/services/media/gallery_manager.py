import sqlite3
import logging
import base64
from typing import Dict, Optional
from collections import OrderedDict

from core_app.data.db_handler import DatabaseHandler

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
    Manages in-memory thumbnail databases (SQLite deserialized) and preview image caching.
    """
    def __init__(self):
        # folder_id -> sqlite3.Connection (In-memory DB)
        self._thumbs_dbs: Dict[int, sqlite3.Connection] = {}
        # file_id -> preview bytes
        self._preview_cache = LRUCache(capacity_mb=200) 
        self._db_handler = DatabaseHandler()

    def _get_thumbs_db_connection(self, folder_id: int) -> Optional[sqlite3.Connection]:
        """
        Retrieves the in-memory thumbnails DB connection for a folder.
        """
        return self._thumbs_dbs.get(folder_id)

    def has_db(self, folder_id: int) -> bool:
        """Checks if the thumbnails DB for the folder is loaded in memory."""
        return folder_id in self._thumbs_dbs

    def load_thumbs_db_from_bytes(self, folder_id: int, db_bytes: bytes):
        """Deserializes a bytes object into an in-memory SQLite connection."""
        try:
            # Create a new in-memory connection
            conn = sqlite3.connect(":memory:")
            # Deserialize the bytes into it
            conn.deserialize(db_bytes)
            self._thumbs_dbs[folder_id] = conn
            logger.info(f"Loaded thumbnails DB for folder {folder_id} into memory.")
        except Exception as e:
            logger.error(f"Failed to deserialize thumbs DB for folder {folder_id}: {e}")

    def create_new_thumbs_db(self, folder_id: int) -> sqlite3.Connection:
        """Creates a fresh, empty in-memory thumbnails DB schema."""
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS thumbnails (
                file_id INTEGER PRIMARY KEY,
                thumb_data BLOB
            )
        ''')
        conn.commit()
        self._thumbs_dbs[folder_id] = conn
        return conn

    def get_serialized_db(self, folder_id: int) -> Optional[bytes]:
        """Serializes the current in-memory DB to bytes for upload."""
        conn = self._thumbs_dbs.get(folder_id)
        if not conn:
            return None
        try:
            return conn.serialize()
        except Exception as e:
            logger.error(f"Failed to serialize thumbs DB for folder {folder_id}: {e}")
            return None

    def update_thumbs_db(self, folder_id: int, new_thumbnails: Dict[int, bytes]) -> Optional[bytes]:
        """
        Updates the in-memory DB with new thumbnails and returns the serialized bytes.
        If DB doesn't exist, creates a new one.
        """
        conn = self._thumbs_dbs.get(folder_id)
        if not conn:
            conn = self.create_new_thumbs_db(folder_id)
        
        try:
            cursor = conn.cursor()
            data_to_insert = [(fid, blob) for fid, blob in new_thumbnails.items()]
            cursor.executemany("INSERT OR REPLACE INTO thumbnails (file_id, thumb_data) VALUES (?, ?)", data_to_insert)
            conn.commit()
            return conn.serialize()
        except Exception as e:
            logger.error(f"Error updating thumbs DB for folder {folder_id}: {e}")
            return None

    def get_folder_thumbnails(self, folder_id: int) -> Dict[str, str]:
        """
        Returns a dict of {file_id: base64_string} for all thumbnails in the folder.
        """
        logger.info(f"Getting thumbnails for folder {folder_id} from GalleryManager")
        conn = self._thumbs_dbs.get(folder_id)
        if not conn:
            logger.warning(f"No in-memory DB found for folder {folder_id}")
            return {}
        
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT file_id, thumb_data FROM thumbnails")
            rows = cursor.fetchall()
            logger.info(f"Found {len(rows)} thumbnails in DB for folder {folder_id}")
            
            result = {}
            for fid, blob in rows:
                if blob:
                    # Convert key to string for JSON compatibility
                    result[str(fid)] = base64.b64encode(blob).decode('utf-8')
            
            logger.info(f"Returning {len(result)} thumbnails for folder {folder_id}")
            return result
        except Exception as e:
            logger.error(f"Error reading thumbs from memory DB for folder {folder_id}: {e}")
            return {}

    def close_folder_db(self, folder_id: int):
        conn = self._thumbs_dbs.pop(folder_id, None)
        if conn:
            conn.close()

    # --- Preview Cache Management ---

    def cache_preview(self, file_id: int, image_bytes: bytes):
        self._preview_cache.put(file_id, image_bytes)

    def get_cached_preview(self, file_id: int) -> Optional[str]:
        data = self._preview_cache.get(file_id)
        if data:
            return base64.b64encode(data).decode('utf-8')
        return None
