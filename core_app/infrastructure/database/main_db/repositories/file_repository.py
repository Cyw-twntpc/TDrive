import sqlite3
import logging
import time
from core_app.core import errors
from .base_repository import BaseRepository


logger = logging.getLogger(__name__)

class FileRepository(BaseRepository):
    def find_file_by_hash(self, file_hash: str) -> int | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM files WHERE hash = ?", (file_hash,))
        row = cursor.fetchone()
        return row['id'] if row else None

    def add_file(self, folder_id: int, name: str, modif_date_ts: float, 
                 file_id: int | None = None, file_hash: str | None = None, size: float | None = None,
                 preview_msg_id: int | None = None, preview_hash: str | None = None,
                 map_msg_id: int | None = None, has_thumb: bool = False):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"檔案名稱 '{name}' 包含無效字元。")

        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
                target_info = cursor.fetchone()
                if target_info and target_info['name'] == 'Recycle Bin':
                        raise errors.InvalidOperationError("無法在回收桶中新增檔案。")

                self._check_name_collision(cursor, folder_id, name, 'file')
                
                target_file_id = file_id
                target_size = size

                if target_file_id is None:
                    if file_hash is None or size is None:
                        raise ValueError("file_hash and size are required when creating new file content.")
                    
                    self._execute_write(cursor,
                        "INSERT INTO files (hash, size, preview_msg_id, preview_hash, map_msg_id, has_thumb) VALUES (?, ?, ?, ?, ?, ?)",
                        (file_hash, size, preview_msg_id, preview_hash, map_msg_id, 1 if has_thumb else 0), score=1
                    )
                    target_file_id = cursor.lastrowid
                else:
                    cursor.execute("SELECT size FROM files WHERE id = ?", (target_file_id,))
                    row = cursor.fetchone()
                    target_size = row['size']

                self._execute_write(cursor,
                    "INSERT INTO file_folder_map (folder_id, file_id, name, modif_date) VALUES (?, ?, ?, ?)",
                    (folder_id, target_file_id, name, modif_date_ts), score=1
                )
                
                ui_file_id = cursor.lastrowid

                self._update_folder_size_recursively(cursor, folder_id, target_size)
                
                return target_file_id, ui_file_id

    def get_file_details(self, map_id: int) -> dict | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.id as map_id, m.name, f.id as file_id, f.size, f.hash, f.preview_msg_id, f.preview_hash,
                   f.map_msg_id as map_msg_id
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            WHERE m.id = ?
        """, (map_id,))
        row = cursor.fetchone()
        if not row: return None
        
        return {
            "id": row['map_id'],
            "file_id": row['file_id'],
            "name": row['name'],
            "size": row['size'],
            "hash": row['hash'],
            "preview_msg_id": row['preview_msg_id'],
            "preview_hash": row['preview_hash'],
            "map_msg_id": row['map_msg_id']
        }

    def update_file_map_msg_id(self, file_id: int, msg_id: int | None):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                self._execute_write(conn, "UPDATE files SET map_msg_id = ? WHERE id = ?", (msg_id, file_id), score=1)

    def rename_file(self, map_id: int, new_name: str):
        if not self._is_valid_item_name(new_name): raise errors.InvalidNameError(f"Invalid name '{new_name}'")
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT folder_id FROM file_folder_map WHERE id = ?", (map_id,))
                info = cursor.fetchone()
                if not info: raise errors.PathNotFoundError(f"File {map_id} not found")
                self._check_name_collision(cursor, info['folder_id'], new_name, 'file', exclude_id=map_id)
                self._execute_write(cursor, "UPDATE file_folder_map SET name = ?, modif_date = ? WHERE id = ?", (new_name, time.time(), map_id), score=1)

    def move_file(self, map_id: int, new_parent_id: int):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT m.folder_id, m.name, m.file_id, f.size 
                    FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.id = ?
                """, (map_id,))
                info = cursor.fetchone()
                if not info: raise errors.PathNotFoundError(f"File {map_id} not found")
                
                old_parent_id = info['folder_id']
                if old_parent_id == new_parent_id: return

                if new_parent_id:
                    cursor.execute("SELECT name FROM folders WHERE id = ?", (new_parent_id,))
                    dest = cursor.fetchone()
                    if not dest: raise errors.PathNotFoundError(f"Dest folder {new_parent_id} not found")
                    if dest['name'] == 'Recycle Bin': raise errors.InvalidOperationError("Use delete to move to Recycle Bin")

                self._check_name_collision(cursor, new_parent_id, info['name'], 'file')
                self._execute_write(cursor, "UPDATE file_folder_map SET folder_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), map_id), score=5)
                
                # Mark source folder so thumbnails don't break during move gap
                # ONLY update if it's NULL (meaning this is the first move since the package was created)
                cursor.execute("SELECT thumb_src_folder_id FROM files WHERE id = ?", (info['file_id'],))
                curr_src = cursor.fetchone()['thumb_src_folder_id']
                if curr_src is None:
                    self._execute_write(cursor, "UPDATE files SET thumb_src_folder_id = ? WHERE id = ?", (old_parent_id, info['file_id']), score=1)
                
                self._update_folder_size_recursively(cursor, old_parent_id, -info['size'])
                self._update_folder_size_recursively(cursor, new_parent_id, info['size'])

    def update_file_preview_info(self, file_id: int, msg_id: int, hash_val: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                self._execute_write(conn, "UPDATE files SET preview_msg_id = ?, preview_hash = ? WHERE id = ?", (msg_id, hash_val, file_id), score=1)

    def search_db_items(self, search_term: str, base_folder_id: int, progress_callback: callable):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Sanitize for FTS5: Escape double quotes and enclose in double quotes for phrase search
        clean_term = search_term.replace('"', '""')
        # Prefix search: "term*"
        fts_query = f'"{clean_term}" *'

        # Check if base_folder_id is Root (TDrive), if so, no need to filter by parent
        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (base_folder_id,))
        root_check = cursor.fetchone()
        is_global_search = (root_check and root_check['parent_id'] is None)

        # Batching variables
        folders_batch, files_batch = [], []
        
        def yield_batch():
            nonlocal folders_batch, files_batch
            if folders_batch or files_batch:
                progress_callback({"folders": folders_batch, "files": files_batch})
                folders_batch, files_batch = [], []

        # Query structure with optional ancestry check
        ancestry_cte = ""
        ancestry_filter_folder = ""
        ancestry_filter_file = ""
        
        if not is_global_search:
            ancestry_cte = """
            ancestors(id) AS (
                SELECT id FROM folders WHERE id = ?
                UNION ALL
                SELECT f.id FROM folders f JOIN ancestors a ON f.parent_id = a.id
            )
            """
            ancestry_filter_folder = "AND f.parent_id IN (SELECT id FROM ancestors)"
            ancestry_filter_file = "AND m.folder_id IN (SELECT id FROM ancestors)"
            params = (base_folder_id, fts_query, fts_query)
        else:
            params = (fts_query, fts_query)

        with_clause = f"WITH RECURSIVE {ancestry_cte}" if not is_global_search else ""
        
        full_sql = f"""
        {with_clause}
        SELECT 
            'folder' as type, f.id, f.parent_id, f.name, f.total_size as size, f.modif_date
        FROM search_index s
        JOIN folders f ON s.item_id = f.id
        WHERE s.item_type = 'folder' AND search_index MATCH ? {ancestry_filter_folder}
        
        UNION ALL
        
        SELECT 
            'file' as type, m.id, m.folder_id as parent_id, m.name, fl.size, m.modif_date
        FROM search_index s
        JOIN file_folder_map m ON s.item_id = m.id
        JOIN files fl ON m.file_id = fl.id
        WHERE s.item_type = 'file' AND search_index MATCH ? {ancestry_filter_file}
        """

        # Handle params carefully
        # If not global: [base_folder_id, term, term]
        # If global: [term, term]
        
        sql_params = list(params)

        try:
            cursor.execute(full_sql, sql_params)
            
            count = 0
            for row in cursor.fetchall():
                if row['type'] == 'folder':
                    folders_batch.append({
                        "id": row['id'], "parent_id": row['parent_id'], "name": row['name'],
                        "raw_size": row['size'], "size": self._format_size(row['size']),
                        "modif_date": self._format_timestamp(row['modif_date'])
                    })
                else:
                    files_batch.append({
                        "id": row['id'], "parent_id": row['parent_id'], "name": row['name'],
                        "raw_size": row['size'], "size": self._format_size(row['size']),
                        "modif_date": self._format_timestamp(row['modif_date'])
                    })
                
                count += 1
                if count >= 50:
                    yield_batch()
                    count = 0
            
            yield_batch()
            
        except sqlite3.OperationalError as e:
            logger.error(f"FTS5 Search failed: {e}")
            # Fallback or empty results

