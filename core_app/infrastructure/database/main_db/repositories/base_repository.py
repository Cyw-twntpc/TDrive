import sqlite3
import math
from datetime import datetime
import logging

from core_app.core import errors

logger = logging.getLogger(__name__)

class BaseRepository:
    def __init__(self, db_connection=None):
        if db_connection is None:
            from core_app.infrastructure.database.main_db.database import DatabaseConnection
            db_connection = DatabaseConnection()
        self.db = db_connection
        self.transaction_logger = self.db.transaction_logger

    @property
    def _db_lock(self):
        return self.db._db_lock

    def _get_conn(self):
        return self.db._get_conn()

    def _execute_write(self, *args, **kwargs):
        return self.db._execute_write(*args, **kwargs)

    def _format_size(self, bytes_num: float | int | None) -> str:
        if not isinstance(bytes_num, (int, float)) or bytes_num is None: return "0 B"
        if bytes_num == 0: return "0 B"
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(bytes_num, k))) if bytes_num > 0 else 0
        if i == 0: return f"{bytes_num:.0f} {sizes[i]}"
        return f"{bytes_num / (k ** i):.1f} {sizes[i]}"

    def _format_timestamp(self, ts: float | None) -> str:
        if ts is None: return "-"
        try:
            dt_obj = datetime.fromtimestamp(ts)
            return dt_obj.strftime("%Y/%m/%d %p %I:%M").replace("AM", "上午").replace("PM", "下午")
        except: return "-"

    def _is_valid_item_name(self, name: str) -> bool:
        if not name or name in (".", ".."): return False
        if any(c in name for c in r'\/<>:"|?*'): return False
        return True

    def _check_name_collision(self, cursor: sqlite3.Cursor, folder_id: int, name: str, item_type: str, exclude_id: int | None = None):
        if item_type == 'folder':
            query = "SELECT id FROM folders WHERE parent_id = ? AND name = ?"
        else:
            query = "SELECT id FROM file_folder_map WHERE folder_id = ? AND name = ?"
        
        params = [folder_id, name]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)

        cursor.execute(query, params)
        if cursor.fetchone():
             raise errors.ItemAlreadyExistsError(f"此位置已存在名為 '{name}' 的{'資料夾' if item_type == 'folder' else '檔案'}。")

    def _update_folder_size_recursively(self, cursor: sqlite3.Cursor, folder_id: int, size_delta: float):
        current_id = folder_id
        while current_id is not None:
            self._execute_write(cursor, "UPDATE folders SET total_size = total_size + ? WHERE id = ?", (size_delta, current_id), score=0)
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_id,))
            result = cursor.fetchone()
            current_id = result['parent_id'] if result else None

    def _get_recycle_bin_id(self, cursor: sqlite3.Cursor) -> int:
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
        return cursor.fetchone()['id']

