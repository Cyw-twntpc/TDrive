import logging
from .base_repository import BaseRepository


logger = logging.getLogger(__name__)

class MapRepository(BaseRepository):
    def create_map_file_record(self, msg_id: int, folder_id: int | None = None) -> int:
        """Creates a new record in map_files and returns its ID."""
        conn = self._get_conn()
        with conn:
            cursor = conn.cursor()
            self._execute_write(cursor, 
                "INSERT INTO map_files (msg_id, folder_id, ref_count) VALUES (?, ?, 0)", 
                (msg_id, folder_id), score=1)
            return cursor.lastrowid

    def update_map_file_msg_id(self, map_id: int, new_msg_id: int | None):
        """Updates the cloud message ID for a given map record (O(1) update)."""
        conn = self._get_conn()
        with conn:
            # Metadata update only, low priority sync
            self._execute_write(conn, "UPDATE map_files SET msg_id = ? WHERE id = ?", (new_msg_id, map_id), score=1)

    def get_map_file_info(self, map_id: int) -> dict | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, msg_id, folder_id, ref_count FROM map_files WHERE id = ?", (map_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def increment_map_ref(self, map_id: int):
        conn = self._get_conn()
        with conn:
            self._execute_write(conn, "UPDATE map_files SET ref_count = ref_count + 1 WHERE id = ?", (map_id,), score=0)

    def decrement_map_ref(self, map_id: int):
        conn = self._get_conn()
        with conn:
            self._execute_write(conn, "UPDATE map_files SET ref_count = ref_count - 1 WHERE id = ?", (map_id,), score=0)

    def set_folder_active_map(self, folder_id: int, map_id: int | None):
        conn = self._get_conn()
        with conn:
            self._execute_write(conn, "UPDATE folders SET active_map_id = ? WHERE id = ?", (map_id, folder_id), score=1)

    def get_folder_active_map(self, folder_id: int) -> int | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT active_map_id FROM folders WHERE id = ?", (folder_id,))
        res = cursor.fetchone()
        return res['active_map_id'] if res else None

