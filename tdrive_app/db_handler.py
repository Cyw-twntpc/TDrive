import sqlite3
import os
import time
import datetime
import logging
import math

from . import errors

logger = logging.getLogger(__name__)

class DatabaseHandler:
    """
    Handles all interactions with the SQLite database, serving as the data
    access layer for the application.

    This class is responsible for initializing the database schema, managing
    connections, and providing a set of CRUD (Create, Read, Update, Delete)
    operations for files, folders, and their associated metadata. It uses
    transactions to ensure data integrity.
    """
    def __init__(self, db_path='./file/tdrive.db'):
        """
        Initializes the database handler, setting the path and ensuring
        the schema and initial data are correctly set up.
        """
        self.db_path = db_path
        # Ensure the directory for the database file exists.
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self, db_path=None):
        """Establishes and returns a new database connection."""
        path_to_connect = db_path if db_path else self.db_path
        conn = sqlite3.connect(path_to_connect)
        # Allows accessing query results by column name.
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """
        Ensures all necessary tables, the root folder, and metadata entries
        are created in the database. This is idempotent.
        """
        logger.debug(f"Initializing database schema at {self.db_path}...")
        conn = self._get_conn()
        cursor = conn.cursor()

        # Folder hierarchy table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            total_size REAL DEFAULT 0, -- Using REAL for consistency with file sizes
            modif_date REAL, -- Unix timestamp for modification date
            FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE,
            UNIQUE (parent_id, name)
        )
        ''')

        # File metadata table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            size REAL NOT NULL,
            hash TEXT NOT NULL,
            modif_date REAL, -- Unix timestamp for modification date
            FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE,
            UNIQUE (parent_id, name)
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
            FOREIGN KEY (file_id) REFERENCES files (id),
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
        
        # Ensure the root 'TDrive' folder exists
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
        if cursor.fetchone() is None:
            logger.info("Root folder 'TDrive' not found, creating it.")
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date) VALUES (?, ?, ?)", 
                           (None, 'TDrive', time.time()))
        
        # Ensure the database version is initialized
        cursor.execute("SELECT value FROM metadata WHERE key = 'db_version'")
        if cursor.fetchone() is None:
            logger.info("Database version not found, initializing to '0'.")
            cursor.execute("INSERT INTO metadata (key, value) VALUES ('db_version', '0')")
        
        conn.commit()
        conn.close()
        logger.debug("Database schema initialization complete.")

    def _format_timestamp(self, ts: float | None) -> str:
        """Formats a Unix timestamp into a human-readable date-time string."""
        if ts is None:
            return "-"
        try:
            dt_obj = datetime.datetime.fromtimestamp(ts)
            return dt_obj.strftime("%Y/%m/%d %p %I:%M").replace("AM", "上午").replace("PM", "下午")
        except (ValueError, TypeError):
            return "-"

    def _format_size(self, bytes_num: float | int | None) -> str:
        """Formats a byte count into a human-readable string (B, KB, MB, GB)."""
        if not isinstance(bytes_num, (int, float)) or bytes_num is None:
            return "0 B"
        if bytes_num == 0:
            return "0 B"
        
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(bytes_num, k))) if bytes_num > 0 else 0
        
        # Format without decimal for Bytes, with one decimal for others.
        if i == 0:
            return f"{bytes_num:.0f} {sizes[i]}"
        return f"{bytes_num / (k ** i):.1f} {sizes[i]}"
    
    def _is_valid_item_name(self, name: str) -> bool:
        """
        Validates an item name to prevent path traversal and invalid characters.
        """
        if not name or name in (".", ".."):
            return False
        # Disallow common path separators and characters invalid in Windows/Linux filenames
        if any(c in name for c in r'\/<>:"|?*'):
            return False
        return True

    def _update_folder_size_recursively(self, cursor: sqlite3.Cursor, folder_id: int, size_delta: float):
        """
        Recursively traverses up the folder hierarchy, adding the `size_delta`
        to each parent folder's `total_size`.
        """
        current_id = folder_id
        while current_id is not None:
            cursor.execute("UPDATE folders SET total_size = total_size + ? WHERE id = ?", (size_delta, current_id))
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_id,))
            result = cursor.fetchone()
            current_id = result['parent_id'] if result else None

    def _increment_db_version(self, cursor: sqlite3.Cursor):
        """Increments the 'db_version' value in the metadata table by one."""
        cursor.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'db_version'")

    def _search_single_folder_items(self, search_term: str, folder_id: int) -> dict:
        """
        Searches for items within a single folder matching the search term.
        This is a helper for the recursive public search method.
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            search_pattern = f'%{search_term}%'
            
            folders = []
            files = []

            # Find matching subfolders
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

            # Find matching files
            cursor.execute("SELECT id, name, size, modif_date FROM files WHERE parent_id = ? AND name LIKE ? COLLATE NOCASE",
                           (folder_id, search_pattern))
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'], "parent_id": folder_id, "name": row['name'],
                    "raw_size": row['size'], "size": self._format_size(row['size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })
            
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    # --- Public API ---

    def get_folder_contents(self, folder_id: int) -> dict:
        """
        Retrieves the immediate subfolders and files for a given folder ID.
        """
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

            cursor.execute("SELECT id, name, size, modif_date FROM files WHERE parent_id = ?", (folder_id,))
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
        """
        Retrieves a flat list of all folders, used to build a folder tree view in the UI.
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, parent_id, name FROM folders ORDER BY name COLLATE NOCASE")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            if conn:
                conn.close()

    def find_file_by_hash(self, file_hash):
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT id, parent_id, name, size, hash, modif_date FROM files WHERE hash = ?", (file_hash,))
            file_row = cursor.fetchone()
        finally:
            if conn:
                conn.close()

        if file_row:
            chunks_data = []
            conn_chunks = None
            try:
                conn_chunks = self._get_conn()
                cursor_chunks = conn_chunks.cursor()
                cursor_chunks.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ?", (file_row['id'],))
                for chunk_row in cursor_chunks.fetchall():
                    chunks_data.append([chunk_row['part_num'], chunk_row['message_id'], chunk_row['part_hash']])
            finally:
                if conn_chunks:
                    conn_chunks.close()

            return {
                "id": file_row['id'],
                "parent_id": file_row['parent_id'],
                "name": file_row['name'],
                "size": file_row['size'],
                "hash": file_row['hash'],
                "modif_date": file_row['modif_date'],
                "split_files": chunks_data
            }
        return None
    
    def add_folder(self, parent_id, name):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"The folder name '{name}' contains invalid characters.")
        
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0)
                )
                self._increment_db_version(cursor)
        except sqlite3.IntegrityError:
            raise errors.ItemAlreadyExistsError(f"A folder with the name '{name}' already exists in this location.")
        finally:
            if conn:
                conn.close()

    def add_file(self, folder_id: int, name: str, size: float, file_hash: str, modif_date_ts: float, chunks_data: list):
        """Adds a new file and its associated chunks to the database."""
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"The file name '{name}' contains invalid characters.")

        conn = self._get_conn()
        try:
            with conn: # Using 'with' statement for automatic transaction management.
                cursor = conn.cursor()
                
                cursor.execute(
                    "INSERT INTO files (parent_id, name, size, hash, modif_date) VALUES (?, ?, ?, ?, ?)",
                    (folder_id, name, size, file_hash, modif_date_ts)
                )
                new_file_id = cursor.lastrowid
                
                if chunks_data:
                    chunk_records = [(new_file_id, c[0], c[1], c[2]) for c in chunks_data]
                    cursor.executemany("INSERT INTO chunks (file_id, part_num, message_id, part_hash) VALUES (?, ?, ?, ?)", chunk_records)
                
                self._update_folder_size_recursively(cursor, folder_id, size)
                self._increment_db_version(cursor)
        except sqlite3.IntegrityError:
            raise errors.ItemAlreadyExistsError(f"A file with the name '{name}' already exists in this location.")
        finally:
            if conn:
                conn.close()

    def remove_file(self, file_id: int) -> list:
        """
        Removes a file and its chunks from the database.
        
        Returns:
            A list of message_ids associated with the file's chunks, which
            should be deleted from the remote storage.
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT parent_id, size FROM files WHERE id = ?", (file_id,))
                file_info = cursor.fetchone()
                if not file_info:
                    raise errors.PathNotFoundError(f"File with ID '{file_id}' not found.")
                
                cursor.execute("SELECT message_id FROM chunks WHERE file_id = ?", (file_id,))
                message_ids = [row['message_id'] for row in cursor.fetchall()]

                cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
                # Deleting chunks and other related data is handled by 'ON DELETE CASCADE'
                
                self._update_folder_size_recursively(cursor, file_info['parent_id'], -file_info['size'])
                self._increment_db_version(cursor)
                
                return message_ids
        finally:
            if conn:
                conn.close()

    def remove_folder(self, folder_id: int) -> list:
        """
        Recursively removes a folder and all its contents (subfolders and files).

        Returns:
            A list of all message_ids from all deleted files, which should be
            deleted from the remote storage.
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT parent_id, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"Folder with ID '{folder_id}' not found.")

                # Use a Common Table Expression (CTE) to recursively find all descendant folders.
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
                
                id_placeholders = ','.join(['?'] * len(all_folder_ids))
                
                cursor.execute(f"SELECT id FROM files WHERE parent_id IN ({id_placeholders})", all_folder_ids)
                all_file_ids = [row['id'] for row in cursor.fetchall()]

                all_message_ids = []
                if all_file_ids:
                    file_id_placeholders = ','.join(['?'] * len(all_file_ids))
                    cursor.execute(f"SELECT message_id FROM chunks WHERE file_id IN ({file_id_placeholders})", all_file_ids)
                    all_message_ids = [row['message_id'] for row in cursor.fetchall()]
                    
                    cursor.execute(f"DELETE FROM chunks WHERE file_id IN ({file_id_placeholders})", all_file_ids)
                    cursor.execute(f"DELETE FROM files WHERE id IN ({file_id_placeholders})", all_file_ids)

                cursor.execute(f"DELETE FROM folders WHERE id IN ({id_placeholders})", all_folder_ids)
                
                if folder_info['parent_id'] is not None:
                    self._update_folder_size_recursively(cursor, folder_info['parent_id'], -folder_info['total_size'])
                
                self._increment_db_version(cursor)
                return all_message_ids
        finally:
            if conn:
                conn.close()

    def rename_folder(self, folder_id: int, new_name: str):
        """Renames a folder."""
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"The folder name '{new_name}' contains invalid characters.")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"Folder with ID '{folder_id}' not found.")
                
                parent_id = folder_info['parent_id']
                # Check for name collision in the same directory.
                cursor.execute("SELECT id FROM folders WHERE parent_id = ? AND name = ? AND id != ?",
                               (parent_id, new_name, folder_id))
                if cursor.fetchone():
                    raise errors.ItemAlreadyExistsError(f"An item named '{new_name}' already exists in this location.")

                cursor.execute("UPDATE folders SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), folder_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def rename_file(self, file_id: int, new_name: str):
        """Renames a file."""
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"The file name '{new_name}' contains invalid characters.")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT parent_id FROM files WHERE id = ?", (file_id,))
                file_info = cursor.fetchone()
                if not file_info:
                    raise errors.PathNotFoundError(f"File with ID '{file_id}' not found.")
                parent_id = file_info['parent_id']

                cursor.execute("SELECT id FROM files WHERE parent_id = ? AND name = ? AND id != ?",
                               (parent_id, new_name, file_id))
                if cursor.fetchone():
                    raise errors.ItemAlreadyExistsError(f"An item named '{new_name}' already exists in this location.")

                cursor.execute("UPDATE files SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), file_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def get_file_details(self, file_id: int) -> dict | None:
        """
        Retrieves all necessary details for a single file to begin a download,
        including its chunks.
        """
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            cursor.execute("SELECT id, name, size, hash FROM files WHERE id = ?", (file_id,))
            file_row = cursor.fetchone()

            if not file_row:
                logger.warning(f"Could not find file details for ID: {file_id}")
                return None

            cursor.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ? ORDER BY part_num", (file_id,))
            chunks_data = cursor.fetchall()

            return {
                "id": file_row['id'],
                "name": file_row['name'],
                "size": file_row['size'],
                "hash": file_row['hash'],
                "chunks": [{"part_num": r['part_num'], "message_id": r['message_id'], "part_hash": r['part_hash']} for r in chunks_data]
            }
        except Exception as e:
            logger.error(f"Error fetching details for file {file_id}: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def get_folder_contents_recursive(self, folder_id: int) -> dict | None:
        """
        Recursively retrieves all items (subfolders and files) within a given folder.
        This is primarily used for downloading an entire folder.
        """
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
            root_folder_row = cursor.fetchone()
            if not root_folder_row:
                raise errors.PathNotFoundError(f"Root folder with ID {folder_id} not found.")
            root_folder_name = root_folder_row['name']

            # Use a recursive CTE to get all descendants and construct their relative paths.
            query = """
            WITH RECURSIVE
              folder_hierarchy(id, parent_id, name, path) AS (
                SELECT id, parent_id, name, name FROM folders WHERE id = :folder_id
                UNION ALL
                SELECT f.id, f.parent_id, f.name, fh.path || '/' || f.name FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
              )
            SELECT
              fh.id, 'folder' as type, fh.name, SUBSTR(fh.path, LENGTH(:root_name) + 2) as relative_path,
              NULL as size, NULL as hash
            FROM folder_hierarchy fh WHERE fh.id != :folder_id
            UNION ALL
            SELECT
              f.id, 'file' as type, f.name,
              CASE WHEN fh.id = :folder_id THEN f.name ELSE SUBSTR(fh.path, LENGTH(:root_name) + 2) || '/' || f.name END as relative_path,
              f.size, f.hash
            FROM files f JOIN folder_hierarchy fh ON f.parent_id = fh.id;
            """
            cursor.execute(query, {"folder_id": folder_id, "root_name": root_folder_name})
            items = [dict(row) for row in cursor.fetchall()]
            
            file_ids = tuple([item['id'] for item in items if item['type'] == 'file'])
            
            chunks_map = {}
            if file_ids:
                chunk_query = f"SELECT file_id, part_num, message_id, part_hash FROM chunks WHERE file_id IN ({','.join(['?'] * len(file_ids))}) ORDER BY file_id, part_num"
                cursor.execute(chunk_query, file_ids)
                for chunk_row in cursor.fetchall():
                    f_id = chunk_row['file_id']
                    if f_id not in chunks_map: chunks_map[f_id] = []
                    chunks_map[f_id].append(dict(chunk_row))

            for item in items:
                if item['type'] == 'file':
                    item['chunks'] = chunks_map.get(item['id'], [])

            return {"folder_name": root_folder_name, "items": items}

        except Exception as e:
            logger.error(f"Failed to recursively fetch contents for folder {folder_id}: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def search_db_items(self, search_term: str, base_folder_id: int, progress_callback: callable):
        """
        Recursively searches for files and folders matching a search term, starting
        from a base folder. Results are streamed back via a progress callback.
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            folders_to_visit = [base_folder_id]
            visited_folders = set()
            
            folders_batch, files_batch = [], []
            BATCH_SIZE = 50 # Yield results in batches to avoid overwhelming the UI thread.

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

                # Find next level of subfolders to traverse
                cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (current_folder_id,))
                for sub_folder_row in cursor.fetchall():
                    folders_to_visit.append(sub_folder_row['id'])

            yield_batch()  # Send any remaining items
        finally:
            if conn:
                conn.close()
    
    def get_db_version(self, db_path=None) -> int:
        """
        Retrieves the current version of the database from the metadata table.
        """
        target_db_path = db_path if db_path else self.db_path
        
        if not os.path.exists(target_db_path):
            logger.warning(f"Database file '{target_db_path}' not found, cannot get version. Returning 0.")
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
