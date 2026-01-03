import sqlite3
import os
import time
import datetime
import logging
import math

from ..common import errors

logger = logging.getLogger(__name__)

class DatabaseHandler:
    TRASH_RETENTION_DAYS = 30
    _is_initialized = False

    def __init__(self, db_path='./file/tdrive.db'):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        if not DatabaseHandler._is_initialized:
            self._init_db()
            DatabaseHandler._is_initialized = True

    def _get_conn(self, db_path=None):
        path_to_connect = db_path if db_path else self.db_path
        conn = sqlite3.connect(path_to_connect)
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        logger.debug(f"Initializing database schema at {self.db_path}...")
        conn = self._get_conn()
        conn.execute('PRAGMA journal_mode=WAL')
        cursor = conn.cursor()

        # Folder hierarchy table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            total_size REAL DEFAULT 0,
            modif_date REAL,
            FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE,
            UNIQUE (parent_id, name)
        )
        ''')

        # Files table (Content Entity)
        # Stores the unique file content and its properties (hash, size).
        # De-duplicated by hash.
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE NOT NULL,
            size REAL NOT NULL
        )
        ''')

        # File-Folder Mapping table (File Structure/Metadata)
        # Maps a logical file (name) in a specific folder to its content (files table).
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_folder_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            modif_date REAL,
            FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE,
            FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE RESTRICT, -- RESTRICT prevents deleting files content if still referenced by map
            UNIQUE (folder_id, name)
        )
        ''')

        # Table to store information about each file chunk
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            part_num INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            part_hash TEXT NOT NULL,
            FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE,
            UNIQUE (file_id, part_num)
        )
        ''')

        # Key-value store for application metadata, like database version
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')

        # Trash Metadata table
        # Stores original location and name for soft-deleted items
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trash_metadata (
            item_id INTEGER NOT NULL,
            item_type TEXT NOT NULL, -- 'file' or 'folder'
            original_parent_id INTEGER,
            original_name TEXT NOT NULL,
            trashed_date REAL NOT NULL,
            PRIMARY KEY (item_id, item_type)
        )
        ''')
        
        # Ensure the root 'TDrive' folder exists
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
        if cursor.fetchone() is None:
            logger.info("Root folder 'TDrive' not found, creating it.")
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date) VALUES (?, ?, ?)", 
                           (None, 'TDrive', time.time()))

        # Ensure the 'Recycle Bin' folder exists
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
        if cursor.fetchone() is None:
            logger.info("Recycle Bin folder not found, creating it.")
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)", 
                           (None, 'Recycle Bin', time.time(), 0))
        
        # Ensure the database version is initialized
        cursor.execute("SELECT value FROM metadata WHERE key = 'db_version'")
        if cursor.fetchone() is None:
            logger.info("Database version not found, initializing to '0'.")
            cursor.execute("INSERT INTO metadata (key, value) VALUES ('db_version', '0')")
        
        conn.commit()
        conn.close()
        logger.debug("Database schema initialization complete.")

    def get_expired_items(self) -> list:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cutoff_date = time.time() - (self.TRASH_RETENTION_DAYS * 86400)
            
            cursor.execute("SELECT item_id, item_type FROM trash_metadata WHERE trashed_date < ?", (cutoff_date,))
            rows = cursor.fetchall()
            return [{'id': row['item_id'], 'type': row['item_type']} for row in rows]
        finally:
            conn.close()

    def _format_timestamp(self, ts: float | None) -> str:
        if ts is None:
            return "-"
        try:
            dt_obj = datetime.datetime.fromtimestamp(ts)
            return dt_obj.strftime("%Y/%m/%d %p %I:%M").replace("AM", "上午").replace("PM", "下午")
        except (ValueError, TypeError):
            return "-"

    def _format_size(self, bytes_num: float | int | None) -> str:
        if not isinstance(bytes_num, (int, float)) or bytes_num is None:
            return "0 B"
        if bytes_num == 0:
            return "0 B"
        
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(bytes_num, k))) if bytes_num > 0 else 0
        
        if i == 0:
            return f"{bytes_num:.0f} {sizes[i]}"
        return f"{bytes_num / (k ** i):.1f} {sizes[i]}"
    
    def _is_valid_item_name(self, name: str) -> bool:
        if not name or name in (".", ".."):
            return False
        if any(c in name for c in r'\/<>:"|?*'):
            return False
        return True

    def _update_folder_size_recursively(self, cursor: sqlite3.Cursor, folder_id: int, size_delta: float):
        current_id = folder_id
        while current_id is not None:
            cursor.execute("UPDATE folders SET total_size = total_size + ? WHERE id = ?", (size_delta, current_id))
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_id,))
            result = cursor.fetchone()
            current_id = result['parent_id'] if result else None

    def _increment_db_version(self, cursor: sqlite3.Cursor):
        cursor.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'db_version'")

    def _search_single_folder_items(self, search_term: str, folder_id: int) -> dict:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            search_pattern = f'%{search_term}%'
            
            folders = []
            files = []

            cursor.execute("SELECT id, name, total_size, modif_date, parent_id FROM folders WHERE parent_id = ? AND name LIKE ? COLLATE NOCASE", 
                           (folder_id, search_pattern))
            for row in cursor.fetchall():
                folders.append({
                    "id": row['id'],
                    "parent_id": row['parent_id'],
                    "name": row['name'],
                    "raw_size": row['total_size'],
                    "size": self._format_size(row['total_size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })

            cursor.execute("""
                SELECT m.id, m.folder_id, m.name, f.size, m.modif_date 
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                WHERE m.folder_id = ? AND m.name LIKE ? COLLATE NOCASE
            """, (folder_id, search_pattern))
            
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'], "parent_id": row['folder_id'], "name": row['name'],
                    "raw_size": row['size'], "size": self._format_size(row['size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })
            
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    def _check_name_collision(self, cursor: sqlite3.Cursor, folder_id: int, name: str, item_type: str, exclude_id: int | None = None):
        if item_type == 'folder':
            query = "SELECT id FROM folders WHERE parent_id = ? AND name = ?"
            params = [folder_id, name]
            if exclude_id is not None:
                query += " AND id != ?"
                params.append(exclude_id)
        elif item_type == 'file':
            query = "SELECT id FROM file_folder_map WHERE folder_id = ? AND name = ?"
            params = [folder_id, name]
            if exclude_id is not None:
                query += " AND id != ?"
                params.append(exclude_id)
        else:
            raise ValueError("item_type must be 'file' or 'folder'")

        cursor.execute(query, params)
        if cursor.fetchone():
             raise errors.ItemAlreadyExistsError(f"此位置已存在名為 '{name}' 的{'資料夾' if item_type == 'folder' else '檔案'}。")
    
    def _get_recycle_bin_id(self, cursor: sqlite3.Cursor) -> int:
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
        res = cursor.fetchone()
        if not res:
            # Should not happen if init_db works, but safety fallback
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)", 
                           (None, 'Recycle Bin', time.time(), 0))
            return cursor.lastrowid
        return res['id']


    # --- Public API ---

    def get_folder_contents(self, folder_id: int) -> dict:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            folders = []
            files = []

            cursor.execute("SELECT id, name, total_size, modif_date FROM folders WHERE parent_id = ?", (folder_id,))
            for row in cursor.fetchall():
                folders.append({
                    "id": row['id'], "name": row['name'], "raw_size": row['total_size'],
                    "size": self._format_size(row['total_size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })

            cursor.execute("""
                SELECT m.id, m.name, f.size, m.modif_date 
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                WHERE m.folder_id = ?
            """, (folder_id,))
            
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'], "name": row['name'], "raw_size": row['size'],
                    "size": self._format_size(row['size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })
            
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    def get_folder_tree(self) -> list:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, parent_id, name FROM folders WHERE name != 'Recycle Bin' ORDER BY name COLLATE NOCASE")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            if conn:
                conn.close()

    def find_file_by_hash(self, file_hash: str) -> int | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM files WHERE hash = ?", (file_hash,))
            row = cursor.fetchone()
            return row['id'] if row else None
        finally:
            if conn:
                conn.close()
    
    def add_folder(self, parent_id, name):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"資料夾名稱 '{name}' 包含無效字元。")
        
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM folders WHERE id = ?", (parent_id,))
                p_info = cursor.fetchone()
                if p_info and p_info['name'] == 'Recycle Bin':
                     raise errors.InvalidOperationError("無法在回收桶中手動建立資料夾。")

                self._check_name_collision(cursor, parent_id, name, 'folder')

                cursor.execute(
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0)
                )
                self._increment_db_version(cursor)
                return cursor.lastrowid
        finally:
            if conn:
                conn.close()

    def add_file(self, folder_id: int, name: str, modif_date_ts: float, file_id: int | None = None, file_hash: str | None = None, size: float | None = None, chunks_data: list | None = None):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"檔案名稱 '{name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
                target_info = cursor.fetchone()
                if target_info and target_info['name'] == 'Recycle Bin':
                     raise errors.InvalidOperationError("無法在回收桶中手動新增檔案。")

                self._check_name_collision(cursor, folder_id, name, 'file')
                
                target_file_id = file_id
                target_size = size

                if target_file_id is None:
                    if file_hash is None or size is None:
                        raise ValueError("file_hash and size are required when creating new file content.")
                    
                    cursor.execute(
                        "INSERT INTO files (hash, size) VALUES (?, ?)",
                        (file_hash, size)
                    )
                    target_file_id = cursor.lastrowid
                    target_size = size
                    
                    if chunks_data:
                        chunk_records = [(target_file_id, c[0], c[1], c[2]) for c in chunks_data]
                        cursor.executemany("INSERT INTO chunks (file_id, part_num, message_id, part_hash) VALUES (?, ?, ?, ?)", chunk_records)
                else:
                    cursor.execute("SELECT size FROM files WHERE id = ?", (target_file_id,))
                    row = cursor.fetchone()
                    if not row:
                        raise errors.PathNotFoundError(f"Referenced file content ID {target_file_id} not found.")
                    target_size = row['size']

                cursor.execute(
                    "INSERT INTO file_folder_map (folder_id, file_id, name, modif_date) VALUES (?, ?, ?, ?)",
                    (folder_id, target_file_id, name, modif_date_ts)
                )

                self._update_folder_size_recursively(cursor, folder_id, target_size)
                self._increment_db_version(cursor)
                
                return target_file_id
        finally:
            if conn:
                conn.close()

    def remove_file(self, map_id: int) -> list:
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT m.folder_id, m.file_id, f.size 
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """, (map_id,))
                
                map_info = cursor.fetchone()
                if not map_info:
                    return []
                
                folder_id = map_info['folder_id']
                file_id = map_info['file_id']
                size = map_info['size']

                cursor.execute("DELETE FROM file_folder_map WHERE id = ?", (map_id,))
                cursor.execute("DELETE FROM trash_metadata WHERE item_id = ? AND item_type = 'file'", (map_id,))
                
                self._update_folder_size_recursively(cursor, folder_id, -size)
                
                cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (file_id,))
                still_referenced = cursor.fetchone() is not None

                message_ids = []
                if not still_referenced:
                    cursor.execute("SELECT message_id FROM chunks WHERE file_id = ?", (file_id,))
                    message_ids = [row['message_id'] for row in cursor.fetchall()]
                    cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))

                self._increment_db_version(cursor)
                return message_ids
        finally:
            if conn:
                conn.close()

    def remove_folder(self, folder_id: int) -> list:
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT parent_id, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    return []

                get_descendants_query = """
                WITH RECURSIVE folder_hierarchy(id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT f.id FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
                )
                SELECT id FROM folder_hierarchy;
                """
                cursor.execute(get_descendants_query, (folder_id,))
                all_folder_ids = [row['id'] for row in cursor.fetchall()]
                
                if not all_folder_ids:
                    return []

                folder_placeholders = ','.join(['?'] * len(all_folder_ids))

                cursor.execute(f"SELECT file_id FROM file_folder_map WHERE folder_id IN ({folder_placeholders})", all_folder_ids)
                affected_content_ids = list(set([row['file_id'] for row in cursor.fetchall()]))

                cursor.execute(f"DELETE FROM file_folder_map WHERE folder_id IN ({folder_placeholders})", all_folder_ids)
                cursor.execute(f"DELETE FROM folders WHERE id IN ({folder_placeholders})", all_folder_ids)
                cursor.execute("DELETE FROM trash_metadata WHERE item_id = ? AND item_type = 'folder'", (folder_id,))

                all_message_ids = []
                
                if affected_content_ids:
                    content_placeholders = ','.join(['?'] * len(affected_content_ids))
                    cursor.execute(f"SELECT DISTINCT file_id FROM file_folder_map WHERE file_id IN ({content_placeholders})", affected_content_ids)
                    referenced_content_ids = set([row['file_id'] for row in cursor.fetchall()])
                    orphan_content_ids = [fid for fid in affected_content_ids if fid not in referenced_content_ids]

                    if orphan_content_ids:
                        orphan_placeholders = ','.join(['?'] * len(orphan_content_ids))
                        cursor.execute(f"SELECT message_id FROM chunks WHERE file_id IN ({orphan_placeholders})", orphan_content_ids)
                        all_message_ids = [row['message_id'] for row in cursor.fetchall()]
                        cursor.execute(f"DELETE FROM files WHERE id IN ({orphan_placeholders})", orphan_content_ids)

                if folder_info['parent_id'] is not None:
                    self._update_folder_size_recursively(cursor, folder_info['parent_id'], -folder_info['total_size'])
                
                self._increment_db_version(cursor)
                return all_message_ids
        finally:
            if conn:
                conn.close()

    def soft_delete_item(self, item_id: int, item_type: str):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                recycle_bin_id = self._get_recycle_bin_id(cursor)
                
                if item_type == 'folder':
                    cursor.execute("SELECT parent_id, name, total_size FROM folders WHERE id = ?", (item_id,))
                    info = cursor.fetchone()
                elif item_type == 'file':
                    cursor.execute("""
                        SELECT m.folder_id as parent_id, m.name, f.size as total_size 
                        FROM file_folder_map m 
                        JOIN files f ON m.file_id = f.id 
                        WHERE m.id = ?
                    """, (item_id,))
                    info = cursor.fetchone()
                else:
                    raise ValueError("Invalid item type")

                if not info:
                    raise errors.PathNotFoundError(f"找不到 ID 為 {item_id} 的 {item_type}。")

                current_parent_id = info['parent_id']
                original_name = info['name']
                size = info['total_size']
                
                if current_parent_id == recycle_bin_id:
                     raise errors.InvalidOperationError("項目已在回收桶中。")

                timestamp_suffix = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                new_name = f"{original_name}_deleted_{timestamp_suffix}"
                
                if item_type == 'folder':
                    cursor.execute("UPDATE folders SET parent_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (recycle_bin_id, new_name, time.time(), item_id))
                else:
                    cursor.execute("UPDATE file_folder_map SET folder_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (recycle_bin_id, new_name, time.time(), item_id))

                cursor.execute("""
                    INSERT OR REPLACE INTO trash_metadata (item_id, item_type, original_parent_id, original_name, trashed_date)
                    VALUES (?, ?, ?, ?, ?)
                """, (item_id, item_type, current_parent_id, original_name, time.time()))

                self._update_folder_size_recursively(cursor, current_parent_id, -size)
                self._update_folder_size_recursively(cursor, recycle_bin_id, size)
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def restore_item(self, item_id: int, item_type: str):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT original_parent_id, original_name 
                    FROM trash_metadata 
                    WHERE item_id = ? AND item_type = ?
                """, (item_id, item_type))
                metadata = cursor.fetchone()
                
                if not metadata:
                    raise errors.PathNotFoundError("找不到項目的回收桶中繼資料。")
                
                original_parent_id = metadata['original_parent_id']
                target_name = metadata['original_name']
                
                cursor.execute("SELECT id FROM folders WHERE id = ?", (original_parent_id,))
                if not cursor.fetchone():
                    logger.warning(f"Original parent {original_parent_id} not found. Restoring to root.")
                    cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
                    root = cursor.fetchone()
                    original_parent_id = root['id']

                recycle_bin_id = self._get_recycle_bin_id(cursor)
                if item_type == 'folder':
                    cursor.execute("SELECT total_size FROM folders WHERE id = ?", (item_id,))
                    info = cursor.fetchone()
                    size = info['total_size']
                else:
                    cursor.execute("""
                        SELECT f.size 
                        FROM file_folder_map m 
                        JOIN files f ON m.file_id = f.id 
                        WHERE m.id = ?
                    """, (item_id,))
                    info = cursor.fetchone()
                    size = info['size']

                base_name = target_name
                ext = ""
                if item_type == 'file' and '.' in base_name:
                    base_name, ext = os.path.splitext(target_name)
                
                counter = 0
                while True:
                    try:
                        self._check_name_collision(cursor, original_parent_id, target_name, item_type, exclude_id=item_id)
                        break
                    except errors.ItemAlreadyExistsError:
                        counter += 1
                        target_name = f"{base_name} ({counter}){ext}"

                if item_type == 'folder':
                    cursor.execute("UPDATE folders SET parent_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (original_parent_id, target_name, time.time(), item_id))
                else:
                    cursor.execute("UPDATE file_folder_map SET folder_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (original_parent_id, target_name, time.time(), item_id))
                
                cursor.execute("DELETE FROM trash_metadata WHERE item_id = ? AND item_type = ?", (item_id, item_type))
                self._update_folder_size_recursively(cursor, recycle_bin_id, -size)
                self._update_folder_size_recursively(cursor, original_parent_id, size)
                self._increment_db_version(cursor)
                return target_name
        finally:
            if conn:
                conn.close()

    def get_trashed_items(self) -> dict:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            folders = []
            files = []
            recycle_bin_id = self._get_recycle_bin_id(cursor)
            
            cursor.execute("""
                SELECT f.id, tm.original_name as name, f.total_size, tm.trashed_date, f.name as physical_name, tm.original_parent_id, tm.trashed_date as trashed_date_ts
                FROM folders f
                JOIN trash_metadata tm ON f.id = tm.item_id AND tm.item_type = 'folder'
                WHERE f.parent_id = ?
            """, (recycle_bin_id,))
            for row in cursor.fetchall():
                folders.append({
                    "id": row['id'], 
                    "name": row['name'], 
                    "raw_size": row['total_size'],
                    "size": self._format_size(row['total_size']),
                    "trashed_date": self._format_timestamp(row['trashed_date']),
                    "trashed_date_ts": row['trashed_date_ts'],
                    "original_parent_id": row['original_parent_id'],
                    "type": "folder"
                })

            cursor.execute("""
                SELECT m.id, tm.original_name as name, f.size, tm.trashed_date, tm.original_parent_id, tm.trashed_date as trashed_date_ts
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                JOIN trash_metadata tm ON m.id = tm.item_id AND tm.item_type = 'file'
                WHERE m.folder_id = ?
            """, (recycle_bin_id,))
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'], 
                    "name": row['name'], 
                    "raw_size": row['size'],
                    "size": self._format_size(row['size']),
                    "trashed_date": self._format_timestamp(row['trashed_date']),
                    "trashed_date_ts": row['trashed_date_ts'],
                    "original_parent_id": row['original_parent_id'],
                    "type": "file"
                })
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    def empty_trash(self) -> list:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            recycle_bin_id = self._get_recycle_bin_id(cursor)
            cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (recycle_bin_id,))
            folders_to_del = [row['id'] for row in cursor.fetchall()]
            cursor.execute("SELECT id FROM file_folder_map WHERE folder_id = ?", (recycle_bin_id,))
            files_to_del = [row['id'] for row in cursor.fetchall()]
            conn.close()
            
            all_msg_ids = []
            for fid in folders_to_del:
                all_msg_ids.extend(self.remove_folder(fid))
            for fid in files_to_del:
                all_msg_ids.extend(self.remove_file(fid))
            return all_msg_ids
        except Exception as e:
            logger.error(f"Error emptying trash: {e}", exc_info=True)
            return []

    def rename_folder(self, folder_id: int, new_name: str):
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"資料夾名稱 '{new_name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"找不到 ID 為 '{folder_id}' 的資料夾。")
                parent_id = folder_info['parent_id']
                self._check_name_collision(cursor, parent_id, new_name, 'folder', exclude_id=folder_id)
                cursor.execute("UPDATE folders SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), folder_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def rename_file(self, map_id: int, new_name: str):
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"檔案名稱 '{new_name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT folder_id FROM file_folder_map WHERE id = ?", (map_id,))
                map_info = cursor.fetchone()
                if not map_info:
                    raise errors.PathNotFoundError(f"找不到 ID 為 '{map_id}' 的檔案。")
                folder_id = map_info['folder_id']
                self._check_name_collision(cursor, folder_id, new_name, 'file', exclude_id=map_id)
                cursor.execute("UPDATE file_folder_map SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), map_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def move_file(self, map_id: int, new_parent_id: int):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT m.folder_id, m.name, m.file_id, f.size 
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """, (map_id,))
                
                map_info = cursor.fetchone()
                if not map_info:
                    raise errors.PathNotFoundError(f"找不到 ID 為 '{map_id}' 的檔案。")
                
                old_parent_id = map_info['folder_id']
                file_name = map_info['name']
                file_size = map_info['size']
                
                if old_parent_id == new_parent_id:
                    return

                if new_parent_id is not None:
                    cursor.execute("SELECT id, name FROM folders WHERE id = ?", (new_parent_id,))
                    dest_info = cursor.fetchone()
                    if not dest_info:
                        raise errors.PathNotFoundError(f"找不到 ID 為 '{new_parent_id}' 的目標資料夾。")
                    if dest_info['name'] == 'Recycle Bin':
                        raise errors.InvalidOperationError("無法手動移動項目至回收桶，請使用刪除功能。")

                self._check_name_collision(cursor, new_parent_id, file_name, 'file')
                cursor.execute("UPDATE file_folder_map SET folder_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), map_id))
                self._update_folder_size_recursively(cursor, old_parent_id, -file_size)
                self._update_folder_size_recursively(cursor, new_parent_id, file_size)
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def move_folder(self, folder_id: int, new_parent_id: int):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id, name, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"找不到 ID 為 '{folder_id}' 的資料夾。")
                
                old_parent_id = folder_info['parent_id']
                folder_name = folder_info['name']
                folder_size = folder_info['total_size']

                if old_parent_id == new_parent_id:
                    return

                if new_parent_id is not None:
                    cursor.execute("SELECT id, name FROM folders WHERE id = ?", (new_parent_id,))
                    dest_info = cursor.fetchone()
                    if not dest_info:
                        raise errors.PathNotFoundError(f"找不到 ID 為 '{new_parent_id}' 的目標資料夾。")
                    if dest_info['name'] == 'Recycle Bin':
                        raise errors.InvalidOperationError("無法手動移動項目至回收桶，請使用刪除功能。")

                    current_check_id = new_parent_id
                    while current_check_id is not None:
                        if current_check_id == folder_id:
                            raise errors.InvalidNameError("無法將資料夾移動至其自身或子資料夾中。")
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_check_id,))
                        res = cursor.fetchone()
                        current_check_id = res['parent_id'] if res else None

                self._check_name_collision(cursor, new_parent_id, folder_name, 'folder')
                cursor.execute("UPDATE folders SET parent_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), folder_id))
                self._update_folder_size_recursively(cursor, old_parent_id, -folder_size)
                self._update_folder_size_recursively(cursor, new_parent_id, folder_size)
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def get_file_details(self, map_id: int) -> dict | None:
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.id as map_id, m.name, f.id as file_id, f.size, f.hash 
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                WHERE m.id = ?
            """, (map_id,))
            file_row = cursor.fetchone()
            if not file_row:
                return None
            
            file_id = file_row['file_id']
            cursor.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ? ORDER BY part_num", (file_id,))
            chunks_data = cursor.fetchall()

            return {
                "id": file_row['map_id'],
                "file_id": file_id,
                "name": file_row['name'],
                "size": file_row['size'],
                "hash": file_row['hash'],
                "chunks": [{"part_num": r['part_num'], "message_id": r['message_id'], "part_hash": r['part_hash']} for r in chunks_data]
            }
        except Exception as e:
            logger.error(f"Error fetching details for file map {map_id}: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def get_folder_contents_recursive(self, folder_id: int) -> dict | None:
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
            root_folder_row = cursor.fetchone()
            if not root_folder_row:
                raise errors.PathNotFoundError(f"Root folder with ID {folder_id} not found.")
            root_folder_name = root_folder_row['name']

            query = """
            WITH RECURSIVE
              folder_hierarchy(id, parent_id, name, path) AS (
                SELECT id, parent_id, name, name FROM folders WHERE id = :folder_id
                UNION ALL
                SELECT f.id, f.parent_id, f.name, fh.path || '/' || f.name FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
              )
            SELECT
              fh.id, 'folder' as type, fh.name, SUBSTR(fh.path, LENGTH(:root_name) + 2) as relative_path,
              NULL as size, NULL as hash, NULL as file_id
            FROM folder_hierarchy fh WHERE fh.id != :folder_id
            UNION ALL
            SELECT
              m.id, 'file' as type, m.name,
              CASE WHEN fh.id = :folder_id THEN m.name ELSE SUBSTR(fh.path, LENGTH(:root_name) + 2) || '/' || m.name END as relative_path,
              f.size, f.hash, f.id as file_id
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            JOIN folder_hierarchy fh ON m.folder_id = fh.id;
            """
            cursor.execute(query, {"folder_id": folder_id, "root_name": root_folder_name})
            items = [dict(row) for row in cursor.fetchall()]
            
            content_ids = list(set([item['file_id'] for item in items if item['type'] == 'file']))
            
            chunks_map = {}
            if content_ids:
                chunk_query = f"SELECT file_id, part_num, message_id, part_hash FROM chunks WHERE file_id IN ({','.join(['?'] * len(content_ids))}) ORDER BY file_id, part_num"
                cursor.execute(chunk_query, content_ids)
                for chunk_row in cursor.fetchall():
                    f_id = chunk_row['file_id']
                    if f_id not in chunks_map: chunks_map[f_id] = []
                    chunks_map[f_id].append(dict(chunk_row))

            for item in items:
                if item['type'] == 'file':
                    item['chunks'] = chunks_map.get(item['file_id'], [])

            return {"folder_name": root_folder_name, "items": items}

        except Exception as e:
            logger.error(f"Failed to recursively fetch contents for folder {folder_id}: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def search_db_items(self, search_term: str, base_folder_id: int, progress_callback: callable):
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            folders_to_visit = [base_folder_id]
            visited_folders = set()
            
            folders_batch, files_batch = [], []
            BATCH_SIZE = 50 

            def yield_batch():
                nonlocal folders_batch, files_batch
                if folders_batch or files_batch:
                    progress_callback({"folders": folders_batch, "files": files_batch})
                    folders_batch, files_batch = [], []

            while folders_to_visit:
                current_folder_id = folders_to_visit.pop(0)
                if current_folder_id in visited_folders:
                    continue
                visited_folders.add(current_folder_id)

                local_results = self._search_single_folder_items(search_term, current_folder_id)
                
                if local_results["folders"]:
                    folders_batch.extend(local_results["folders"])
                if local_results["files"]:
                    files_batch.extend(local_results["files"])

                if len(folders_batch) + len(files_batch) >= BATCH_SIZE:
                    yield_batch()

                cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (current_folder_id,))
                for sub_folder_row in cursor.fetchall():
                    folders_to_visit.append(sub_folder_row['id'])

            yield_batch()  
        finally:
            if conn:
                conn.close()
    
    def get_db_version(self, db_path=None) -> int:
        target_db_path = db_path if db_path else self.db_path
        
        if not os.path.exists(target_db_path):
            return 0

        conn = self._get_conn(db_path=target_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM metadata WHERE key = 'db_version'")
            result = cursor.fetchone()
            return int(result['value']) if result else 0
        except Exception as e:
            logger.error(f"Failed to get DB version from '{target_db_path}': {e}", exc_info=True)
            return 0
        finally:
            if conn:
                conn.close()

    def check_folder_exists(self, folder_id: int) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,))
            return cursor.fetchone() is not None
        finally:
            conn.close()