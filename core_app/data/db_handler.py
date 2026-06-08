import sqlite3
import os
import time
import datetime
import logging
import math
import threading

from ..common import errors
from .transaction_logger import TransactionLogger
from .sync_manager import SyncManager

logger = logging.getLogger(__name__)

class DatabaseHandler:
    TRASH_RETENTION_DAYS = 30
    _instance = None
    _lock = threading.Lock()
    
    # Singleton Pattern
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(DatabaseHandler, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path=':memory:'):
        # Only initialize once
        if self._initialized:
            return
            
        self.db_path = db_path # Should be ':memory:' for production as per requirement
        self._connection = None
        self._db_lock = threading.RLock()
        self.transaction_logger = TransactionLogger()
        self.sync_manager = SyncManager()
        self._init_db()
        self._initialized = True

    def _get_conn(self, db_path=None):
        """
        In memory mode, we must return the SAME connection instance every time.
        If db_path is provided (e.g. for checking old file DBs), we create a new connection.
        """
        if db_path and db_path != ':memory:':
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL;')
            return conn

        if self._connection is None:
            self._connection = sqlite3.connect(':memory:', check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute('PRAGMA foreign_keys = ON')
            self._connection.execute('PRAGMA journal_mode=WAL;')
        
        return self._connection

    def _execute_write(self, cursor: sqlite3.Cursor, sql: str, params: tuple, score: int = 1):
        """Executes write operation with Write-Ahead Logging (WAL) pattern."""
        self.transaction_logger.append(sql, params)
        cursor.execute(sql, params)
        self.sync_manager.add_change(score)

    def _init_db(self):
        logger.debug(f"Initializing database schema in {self.db_path}...")
        self._create_tables()
        self._seed_default_data()
        self._create_triggers()

    def _create_tables(self):
        conn = self._get_conn()
        cursor = conn.cursor()

        # Map Files Table (New Middleware Table)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS map_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER, -- Cloud Message ID
            folder_id INTEGER, -- If aggregated, belongs to this folder
            ref_count INTEGER DEFAULT 0 -- Garbage collection helper
        )
        ''')

        # Folder hierarchy table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            total_size REAL DEFAULT 0,
            modif_date REAL,
            thumbs_db_msg_id INTEGER,
            thumbs_db_hash TEXT,
            active_map_id INTEGER, -- Points to map_files.id (Current aggregated map for writing)
            FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE,
            FOREIGN KEY (active_map_id) REFERENCES map_files (id) ON DELETE SET NULL,
            UNIQUE (parent_id, name)
        )
        ''')

        # Files table (Content Entity)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE NOT NULL,
            size REAL NOT NULL,
            preview_msg_id INTEGER,
            preview_hash TEXT,
            map_id INTEGER, -- Reference to map_files table
            FOREIGN KEY (map_id) REFERENCES map_files (id) ON DELETE SET NULL
        )
        ''')

        # File-Folder Mapping table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_folder_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            modif_date REAL,
            FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE,
            FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE RESTRICT,
            UNIQUE (folder_id, name)
        )
        ''')

        # Trash Metadata
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
        
        # --- FTS5 Search Index ---
        # Create virtual table for full-text search
        cursor.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            name, 
            item_type UNINDEXED, 
            item_id UNINDEXED, 
            folder_id UNINDEXED, 
            tokenize='porter unicode61'
        )
        ''')
        conn.commit()

    def _seed_default_data(self):
        """Inserts mandatory root objects if they don't exist."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Init Root
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date) VALUES (?, ?, ?)", 
                           (None, 'TDrive', time.time()))

        # Init Recycle Bin
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)", 
                           (None, 'Recycle Bin', time.time(), 0))
        conn.commit()

    def _create_triggers(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Triggers for Folders
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS folders_ai AFTER INSERT ON folders BEGIN
            INSERT INTO search_index(name, item_type, item_id, folder_id) 
            VALUES (new.name, 'folder', new.id, new.parent_id);
        END;
        ''')
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS folders_ad AFTER DELETE ON folders BEGIN
            DELETE FROM search_index WHERE item_type='folder' AND item_id=old.id;
        END;
        ''')
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS folders_au AFTER UPDATE ON folders BEGIN
            UPDATE search_index SET name=new.name, folder_id=new.parent_id 
            WHERE item_type='folder' AND item_id=new.id;
        END;
        ''')

        # Triggers for Files (file_folder_map)
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON file_folder_map BEGIN
            INSERT INTO search_index(name, item_type, item_id, folder_id) 
            VALUES (new.name, 'file', new.id, new.folder_id);
        END;
        ''')
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON file_folder_map BEGIN
            DELETE FROM search_index WHERE item_type='file' AND item_id=old.id;
        END;
        ''')
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON file_folder_map BEGIN
            UPDATE search_index SET name=new.name, folder_id=new.folder_id 
            WHERE item_type='file' AND item_id=new.id;
        END;
        ''')
        conn.commit()

    def rebuild_search_index(self):
        """Manually rebuilds the FTS5 search index."""
        conn = self._get_conn()
        with conn:
            conn.execute("INSERT INTO search_index(search_index) VALUES('rebuild')")
            logger.info("Search index rebuilt.")

    def get_expired_items(self) -> list:
        conn = self._get_conn()
        cursor = conn.cursor()
        cutoff_date = time.time() - (self.TRASH_RETENTION_DAYS * 86400)
        
        cursor.execute("SELECT item_id, item_type FROM trash_metadata WHERE trashed_date < ?", (cutoff_date,))
        rows = cursor.fetchall()
        return [{'id': row['item_id'], 'type': row['item_type']} for row in rows]

    # --- Map File Helper Methods ---

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

    # --- Standard Methods (Modified) ---

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
            dt_obj = datetime.datetime.fromtimestamp(ts)
            return dt_obj.strftime("%Y/%m/%d %p %I:%M").replace("AM", "上午").replace("PM", "下午")
        except: return "-"

    def _is_valid_item_name(self, name: str) -> bool:
        if not name or name in (".", ".."): return False
        if any(c in name for c in r'\/<>:"|?*'): return False
        return True

    def _update_folder_size_recursively(self, cursor: sqlite3.Cursor, folder_id: int, size_delta: float):
        current_id = folder_id
        while current_id is not None:
            self._execute_write(cursor, "UPDATE folders SET total_size = total_size + ? WHERE id = ?", (size_delta, current_id), score=0)
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_id,))
            result = cursor.fetchone()
            current_id = result['parent_id'] if result else None

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
    
    def _get_recycle_bin_id(self, cursor: sqlite3.Cursor) -> int:
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Recycle Bin'")
        return cursor.fetchone()['id']

    # --- Public API ---

    def run_integrity_check(self) -> bool:
        """Executes PRAGMA integrity_check to verify database health."""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            rows = cursor.fetchall()
            
            # integrity_check returns a single row with 'ok' if healthy
            if rows and len(rows) > 0:
                result = rows[0][0]
                if str(result).lower() == 'ok':
                    return True
                else:
                    errors = [str(r[0]) for r in rows]
                    logger.error(f"Database integrity check failed: {errors}")
                    return False
            return False
        except Exception as e:
            logger.error(f"Error during database integrity check: {e}")
            return False

    def get_folder_contents(self, folder_id: int) -> dict:
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Verify folder exists first
        cursor.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (folder_id,))
        current_folder_row = cursor.fetchone()
        if not current_folder_row:
             raise errors.PathNotFoundError(f"Folder {folder_id} not found.")
             
        path_info = []
        curr_id = folder_id
        while curr_id:
            cursor.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (curr_id,))
            r = cursor.fetchone()
            if r:
                path_info.insert(0, {"id": r['id'], "name": r['name']})
                curr_id = r['parent_id']
            else:
                break
                
        current_folder = {
            "id": current_folder_row['id'],
            "name": current_folder_row['name'],
            "parent_id": current_folder_row['parent_id'],
            "path": path_info
        }

        folders = []
        files = []

        cursor.execute("SELECT id, name, total_size, modif_date, thumbs_db_msg_id FROM folders WHERE parent_id = ?", (folder_id,))
        for row in cursor.fetchall():
            folders.append({
                "id": row['id'], "name": row['name'], "raw_size": row['total_size'],
                "size": self._format_size(row['total_size']),
                "modif_date": self._format_timestamp(row['modif_date']),
                "thumbs_db_msg_id": row['thumbs_db_msg_id']
            })

        cursor.execute("""
            SELECT m.id, m.name, f.size, m.modif_date, f.preview_msg_id 
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            WHERE m.folder_id = ?
        """, (folder_id,))
        
        for row in cursor.fetchall():
            files.append({
                "id": row['id'], "name": row['name'], "raw_size": row['size'],
                "size": self._format_size(row['size']),
                "modif_date": self._format_timestamp(row['modif_date']),
                "preview_msg_id": row['preview_msg_id']
            })
        
        return {
            "success": True,
            "folders": folders, 
            "files": files,
            "current_folder": current_folder
        }

    def get_folder_tree(self) -> list:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, parent_id, name FROM folders WHERE name != 'Recycle Bin' ORDER BY name COLLATE NOCASE")
        return [dict(row) for row in cursor.fetchall()]

    def find_file_by_hash(self, file_hash: str) -> int | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM files WHERE hash = ?", (file_hash,))
        row = cursor.fetchone()
        return row['id'] if row else None
    
    def add_folder(self, parent_id, name):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"資料夾名稱 '{name}' 包含無效字元。")
        
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM folders WHERE id = ?", (parent_id,))
                p_info = cursor.fetchone()
                if p_info and p_info['name'] == 'Recycle Bin':
                        raise errors.InvalidOperationError("無法在回收桶中建立資料夾。")

                self._check_name_collision(cursor, parent_id, name, 'folder')

                self._execute_write(cursor,
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0), score=2
                )
                return cursor.lastrowid

    def add_file(self, folder_id: int, name: str, modif_date_ts: float, 
                 file_id: int | None = None, file_hash: str | None = None, size: float | None = None,
                 preview_msg_id: int | None = None, preview_hash: str | None = None,
                 map_id: int | None = None):
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
                        "INSERT INTO files (hash, size, preview_msg_id, preview_hash, map_id) VALUES (?, ?, ?, ?, ?)",
                        (file_hash, size, preview_msg_id, preview_hash, map_id), score=1
                    )
                    target_file_id = cursor.lastrowid
                    
                    if map_id is not None:
                        self._execute_write(cursor, "UPDATE map_files SET ref_count = ref_count + 1 WHERE id = ?", (map_id,), score=0)
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

    def remove_file(self, map_id: int) -> dict | None:
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT m.folder_id, m.file_id, f.size, f.preview_msg_id, f.map_id 
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """, (map_id,))
                
                map_info = cursor.fetchone()
                if not map_info:
                    return None
                
                folder_id = map_info['folder_id']
                file_id = map_info['file_id']
                size = map_info['size']
                used_map_id = map_info['map_id']

                self._execute_write(cursor, "DELETE FROM file_folder_map WHERE id = ?", (map_id,), score=5)
                self._execute_write(cursor, "DELETE FROM trash_metadata WHERE item_id = ? AND item_type = 'file'", (map_id,), score=0)
                
                self._update_folder_size_recursively(cursor, folder_id, -size)
                
                cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (file_id,))
                still_referenced = cursor.fetchone() is not None

                result = {
                    "orphan": False, 
                    "file_id": file_id, 
                    "map_id": used_map_id,
                    "parent_id": folder_id,
                    "msg_ids_to_delete": [],
                    "map_msg_id_to_delete": None
                }

                if not still_referenced:
                    result["orphan"] = True
                    if map_info['preview_msg_id']:
                        result['msg_ids_to_delete'].append(map_info['preview_msg_id'])
                    
                    self._execute_write(cursor, "DELETE FROM files WHERE id = ?", (file_id,), score=1)
                    
                    # Decrement map ref count
                    self._execute_write(cursor, "UPDATE map_files SET ref_count = ref_count - 1 WHERE id = ?", (used_map_id,), score=0)

                    # Check if map is dead
                    cursor.execute("SELECT ref_count, msg_id FROM map_files WHERE id = ?", (used_map_id,))
                    map_row = cursor.fetchone()
                    if map_row and map_row['ref_count'] <= 0:
                        self._execute_write(cursor, "DELETE FROM map_files WHERE id = ?", (used_map_id,), score=1)
                        if map_row['msg_id']:
                             result["map_msg_id_to_delete"] = map_row['msg_id']

                return result

    def remove_folder(self, folder_id: int) -> list:
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT parent_id, total_size, thumbs_db_msg_id FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info: return []

                msgs_to_delete = []
                if folder_info['thumbs_db_msg_id']:
                    msgs_to_delete.append(folder_info['thumbs_db_msg_id'])

                # Get all sub-folders
                get_descendants = """
                WITH RECURSIVE folder_hierarchy(id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT f.id FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
                )
                SELECT id FROM folder_hierarchy;
                """
                cursor.execute(get_descendants, (folder_id,))
                all_folder_ids = [row['id'] for row in cursor.fetchall()]
                
                if not all_folder_ids:
                    return [{"msg_ids_to_delete": msgs_to_delete, "parent_id": folder_info['parent_id']}]

                f_placeholders = ','.join(['?'] * len(all_folder_ids))
                
                # Find all files in these folders (Need map_ids for trash cleanup)
                cursor.execute(f"SELECT id FROM file_folder_map WHERE folder_id IN ({f_placeholders})", all_folder_ids)
                map_ids = [row['id'] for row in cursor.fetchall()]
                
                results = []
                if msgs_to_delete:
                    results.append({"msg_ids_to_delete": msgs_to_delete, "parent_id": folder_info['parent_id']})
                
                cursor.execute(f"SELECT m.file_id, f.size, f.preview_msg_id, f.map_id FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.folder_id IN ({f_placeholders})", all_folder_ids)
                files_info = cursor.fetchall()
                
                # Delete maps (Path mappings)
                self._execute_write(cursor, f"DELETE FROM file_folder_map WHERE folder_id IN ({f_placeholders})", all_folder_ids, score=5)
                
                # [FIX] Clean up Trash Metadata for these files
                if map_ids:
                    m_placeholders = ','.join(['?'] * len(map_ids))
                    self._execute_write(cursor, f"DELETE FROM trash_metadata WHERE item_type = 'file' AND item_id IN ({m_placeholders})", map_ids, score=0)
                
                # Check orphans & Cleanup Map Files
                orphans = []
                for finfo in files_info:
                    fid = finfo['file_id']
                    mid = finfo['map_id']

                    cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (fid,))
                    if not cursor.fetchone():
                        # It's an orphan
                        self._execute_write(cursor, "DELETE FROM files WHERE id = ?", (fid,), score=1)
                        self._execute_write(cursor, "UPDATE map_files SET ref_count = ref_count - 1 WHERE id = ?", (mid,), score=0)
                        
                        # Check Map Liveness
                        cursor.execute("SELECT ref_count, msg_id FROM map_files WHERE id = ?", (mid,))
                        map_row = cursor.fetchone()
                        
                        map_msg_id_del = None
                        if map_row and map_row['ref_count'] <= 0:
                            self._execute_write(cursor, "DELETE FROM map_files WHERE id = ?", (mid,), score=1)
                            map_msg_id_del = map_row['msg_id']
                        
                        res = {
                            "orphan": True,
                            "file_id": fid,
                            "map_id": mid,
                            "msg_ids_to_delete": [],
                            "map_msg_id_to_delete": map_msg_id_del
                        }
                        if finfo['preview_msg_id']:
                            res['msg_ids_to_delete'].append(finfo['preview_msg_id'])
                        results.append(res)

                # Delete folders
                self._execute_write(cursor, f"DELETE FROM folders WHERE id IN ({f_placeholders})", all_folder_ids, score=20) # Force sync
                self._execute_write(cursor, "DELETE FROM trash_metadata WHERE item_id = ? AND item_type = 'folder'", (folder_id,), score=0)
                
                # Force commit to ensure deletion persists in memory DB
                conn.commit()

                if folder_info['parent_id']:
                    self._update_folder_size_recursively(cursor, folder_info['parent_id'], -folder_info['total_size'])
                
                return results

    def soft_delete_item(self, item_id: int, item_type: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                recycle_bin_id = self._get_recycle_bin_id(cursor)
                
                if item_type == 'folder':
                    cursor.execute("SELECT parent_id, name, total_size FROM folders WHERE id = ?", (item_id,))
                    info = cursor.fetchone()
                else:
                    cursor.execute("""
                        SELECT m.folder_id as parent_id, m.name, f.size as total_size 
                        FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.id = ?
                    """, (item_id,))
                    info = cursor.fetchone()

                if not info: raise errors.PathNotFoundError(f"Item {item_id} not found.")
                
                current_parent_id = info['parent_id']
                if current_parent_id == recycle_bin_id:
                    raise errors.InvalidOperationError("項目已在回收桶中。")

                self._execute_write(cursor, """
                    INSERT OR REPLACE INTO trash_metadata (item_id, item_type, original_parent_id, original_name, trashed_date)
                    VALUES (?, ?, ?, ?, ?)
                """, (item_id, item_type, info['parent_id'], info['name'], time.time()), score=5)
                
                timestamp_suffix = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                new_name = f"{info['name']}_deleted_{timestamp_suffix}"
                
                if item_type == 'folder':
                    self._execute_write(cursor, "UPDATE folders SET parent_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (recycle_bin_id, new_name, time.time(), item_id), score=1)
                else:
                    self._execute_write(cursor, "UPDATE file_folder_map SET folder_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (recycle_bin_id, new_name, time.time(), item_id), score=1)
                                   
                self._update_folder_size_recursively(cursor, info['parent_id'], -info['total_size'])
                self._update_folder_size_recursively(cursor, recycle_bin_id, info['total_size'])

    def restore_item(self, item_id: int, item_type: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT original_parent_id, original_name FROM trash_metadata WHERE item_id = ? AND item_type = ?", (item_id, item_type))
                meta = cursor.fetchone()
                if not meta: raise errors.PathNotFoundError("Metadata not found")
                
                original_parent_id = meta['original_parent_id']
                target_name = meta['original_name']
                
                cursor.execute("SELECT id FROM folders WHERE id = ?", (original_parent_id,))
                if not cursor.fetchone():
                    cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
                    original_parent_id = cursor.fetchone()['id']
                
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
                
                recycle_bin_id = self._get_recycle_bin_id(cursor)
                
                if item_type == 'folder':
                    cursor.execute("SELECT total_size FROM folders WHERE id = ?", (item_id,))
                    size = cursor.fetchone()['total_size']
                    self._execute_write(cursor, "UPDATE folders SET parent_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (original_parent_id, target_name, time.time(), item_id), score=5)
                else:
                    cursor.execute("""
                        SELECT f.size FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.id = ?
                    """, (item_id,))
                    size = cursor.fetchone()['size']
                    self._execute_write(cursor, "UPDATE file_folder_map SET folder_id = ?, name = ?, modif_date = ? WHERE id = ?", 
                                   (original_parent_id, target_name, time.time(), item_id), score=5)
                
                self._execute_write(cursor, "DELETE FROM trash_metadata WHERE item_id = ? AND item_type = ?", (item_id, item_type), score=1)
                self._update_folder_size_recursively(cursor, recycle_bin_id, -size)
                self._update_folder_size_recursively(cursor, original_parent_id, size)
                return target_name

    def get_trashed_items(self) -> dict:
        conn = self._get_conn()
        cursor = conn.cursor()
        folders = []
        files = []
        recycle_bin_id = self._get_recycle_bin_id(cursor)
        
        # Get trashed folders
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

        # Get trashed files
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

    def empty_trash(self) -> list:
        with self._db_lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            recycle_id = self._get_recycle_bin_id(cursor)
            
            cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (recycle_id,))
            f_ids = [r['id'] for r in cursor.fetchall()]
            cursor.execute("SELECT id FROM file_folder_map WHERE folder_id = ?", (recycle_id,))
            m_ids = [r['id'] for r in cursor.fetchall()]
            
            results = []
            for fid in f_ids:
                results.extend(self.remove_folder(fid))
            for mid in m_ids:
                res = self.remove_file(mid)
                if res: results.append(res)
                
            return results

    def get_file_details(self, map_id: int) -> dict | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.id as map_id, m.name, f.id as file_id, f.size, f.hash, f.preview_msg_id, f.preview_hash,
                   mp.msg_id as map_msg_id, f.map_id as map_ref_id
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            JOIN map_files mp ON f.map_id = mp.id
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
            "map_msg_id": row['map_msg_id'],
            "map_ref_id": row['map_ref_id']
        }
    
    def rename_folder(self, folder_id: int, new_name: str):
        if not self._is_valid_item_name(new_name): raise errors.InvalidNameError(f"Invalid name '{new_name}'")
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
                info = cursor.fetchone()
                if not info: raise errors.PathNotFoundError(f"Folder {folder_id} not found")
                self._check_name_collision(cursor, info['parent_id'], new_name, 'folder', exclude_id=folder_id)
                self._execute_write(cursor, "UPDATE folders SET name = ?, modif_date = ? WHERE id = ?", (new_name, time.time(), folder_id), score=1)

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
                self._update_folder_size_recursively(cursor, old_parent_id, -info['size'])
                self._update_folder_size_recursively(cursor, new_parent_id, info['size'])

    def move_folder(self, folder_id: int, new_parent_id: int):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id, name, total_size FROM folders WHERE id = ?", (folder_id,))
                info = cursor.fetchone()
                if not info: raise errors.PathNotFoundError(f"Folder {folder_id} not found")
                
                old_parent_id = info['parent_id']
                if old_parent_id == new_parent_id: return

                if new_parent_id:
                    cursor.execute("SELECT name FROM folders WHERE id = ?", (new_parent_id,))
                    dest = cursor.fetchone()
                    if not dest: raise errors.PathNotFoundError(f"Dest {new_parent_id} not found")
                    if dest['name'] == 'Recycle Bin': raise errors.InvalidOperationError("Use delete to move to Recycle Bin")
                    
                    check_id = new_parent_id
                    while check_id:
                        if check_id == folder_id: raise errors.InvalidOperationError("Cannot move folder into itself")
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (check_id,))
                        res = cursor.fetchone()
                        check_id = res['parent_id'] if res else None

                self._check_name_collision(cursor, new_parent_id, info['name'], 'folder')
                self._execute_write(cursor, "UPDATE folders SET parent_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), folder_id), score=5)
                self._update_folder_size_recursively(cursor, old_parent_id, -info['total_size'])
                self._update_folder_size_recursively(cursor, new_parent_id, info['total_size'])

    def get_folder_contents_recursive(self, folder_id: int) -> dict | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
        root = cursor.fetchone()
        if not root: return None
        
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
        cursor.execute(query, {"folder_id": folder_id, "root_name": root['name']})
        items = [dict(row) for row in cursor.fetchall()]
        
        # NOTE: Caller needs to use metadata_manager to get chunks.
        
        return {"folder_name": root['name'], "items": items}

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

        full_sql = f"""
        WITH RECURSIVE 
            {ancestry_cte if not is_global_search else ''}
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
            pass

    def update_folder_thumbs_info(self, folder_id: int, msg_id: int, hash_val: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                self._execute_write(conn, "UPDATE folders SET thumbs_db_msg_id = ?, thumbs_db_hash = ? WHERE id = ?", (msg_id, hash_val, folder_id), score=1)
    
    def update_file_preview_info(self, file_id: int, msg_id: int, hash_val: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                self._execute_write(conn, "UPDATE files SET preview_msg_id = ?, preview_hash = ? WHERE id = ?", (msg_id, hash_val, file_id), score=1)

    def check_folder_exists(self, folder_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,))
        return cursor.fetchone() is not None