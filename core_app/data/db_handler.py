import sqlite3
import os
import time
import datetime
import logging
import math

from ..common import errors

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
        conn.execute('PRAGMA synchronous=NORMAL')
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

            # Find matching files (JOIN files and file_folder_map)
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
        """
        Checks if an item with the same name already exists in the specified folder.
        Raises ItemAlreadyExistsError if a collision is found.
        
        Args:
            cursor: Active database cursor.
            folder_id: The ID of the folder to check within.
            name: The name to check.
            item_type: 'folder' or 'file'.
            exclude_id: Optional ID to exclude from the check (used for rename operations).
        """
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

            # JOIN files table to get size
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

    def find_file_by_hash(self, file_hash: str) -> int | None:
        """
        Checks if a file with the given hash already exists in the 'files' table.
        Returns the file_id if found, otherwise None.
        """
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
                self._check_name_collision(cursor, parent_id, name, 'folder')

                cursor.execute(
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0)
                )
                self._increment_db_version(cursor)
                return cursor.lastrowid # Return the ID of the newly created folder
        finally:
            if conn:
                conn.close()

    def add_file(self, folder_id: int, name: str, modif_date_ts: float, file_id: int | None = None, file_hash: str | None = None, size: float | None = None, chunks_data: list | None = None):
        """
        Adds a file entry to the database.
        
        If 'file_id' is provided, it links the new entry to existing content (deduplication/copy).
        If 'file_id' is None, it creates a new content entry in 'files' and 'chunks' tables 
        using 'file_hash', 'size', and 'chunks_data'.
        """
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"The file name '{name}' contains invalid characters.")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                # Check for name collision first
                self._check_name_collision(cursor, folder_id, name, 'file')
                
                target_file_id = file_id
                target_size = size

                if target_file_id is None:
                    # Case 1: New file content
                    if file_hash is None or size is None:
                        raise ValueError("file_hash and size are required when creating new file content.")
                    
                    # Insert into files table (content) - modif_date in files table tracks when content was first added
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
                    # Case 2: Existing content (Deduplication)
                    # We need the size to update folder statistics
                    cursor.execute("SELECT size FROM files WHERE id = ?", (target_file_id,))
                    row = cursor.fetchone()
                    if not row:
                        raise errors.PathNotFoundError(f"Referenced file content ID {target_file_id} not found.")
                    target_size = row['size']

                # Create the structure mapping
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
        """
        Removes a file mapping from a folder. If the file content is no longer
        referenced by any other folder, the content and chunks are also removed.
        
        Args:
            map_id: The ID of the record in 'file_folder_map' to remove.

        Returns:
            A list of message_ids associated with the file's chunks if the content
            was deleted (no longer referenced). Returns empty list if content remains.
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                # Get info about the map and the underlying file content
                cursor.execute("""
                    SELECT m.folder_id, m.file_id, f.size 
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """, (map_id,))
                
                map_info = cursor.fetchone()
                if not map_info:
                    raise errors.PathNotFoundError(f"File map with ID '{map_id}' not found.")
                
                folder_id = map_info['folder_id']
                file_id = map_info['file_id']
                size = map_info['size']

                # Delete the mapping
                cursor.execute("DELETE FROM file_folder_map WHERE id = ?", (map_id,))
                
                # Update folder size
                self._update_folder_size_recursively(cursor, folder_id, -size)
                
                # Check if the content is still referenced
                cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (file_id,))
                still_referenced = cursor.fetchone() is not None

                message_ids = []
                if not still_referenced:
                    # Content is orphan, delete it
                    cursor.execute("SELECT message_id FROM chunks WHERE file_id = ?", (file_id,))
                    message_ids = [row['message_id'] for row in cursor.fetchall()]

                    # ON DELETE CASCADE will handle chunks
                    cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))

                self._increment_db_version(cursor)
                
                return message_ids
        finally:
            if conn:
                conn.close()

    def remove_folder(self, folder_id: int) -> list:
        """
        Recursively removes a folder and all its contents (subfolders and files).

        Returns:
            A list of all message_ids from all deleted files (that are not referenced elsewhere),
            which should be deleted from the remote storage.
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT parent_id, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"Folder with ID '{folder_id}' not found.")

                # 1. Find all descendant folder IDs (including self)
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

                # 2. Find all file maps in these folders to get content IDs
                cursor.execute(f"SELECT file_id FROM file_folder_map WHERE folder_id IN ({folder_placeholders})", all_folder_ids)
                affected_content_ids = list(set([row['file_id'] for row in cursor.fetchall()]))

                # 3. Delete folders (CASCADE logic handled manually or via foreign keys if set, but map has restrict usually)
                # First delete maps to avoid foreign key constraints if any
                cursor.execute(f"DELETE FROM file_folder_map WHERE folder_id IN ({folder_placeholders})", all_folder_ids)
                
                # Then delete folders
                cursor.execute(f"DELETE FROM folders WHERE id IN ({folder_placeholders})", all_folder_ids)

                all_message_ids = []
                
                # 4. Check for orphan content
                if affected_content_ids:
                    content_placeholders = ','.join(['?'] * len(affected_content_ids))
                    
                    # Find which of these content IDs are still referenced by other maps
                    cursor.execute(f"SELECT DISTINCT file_id FROM file_folder_map WHERE file_id IN ({content_placeholders})", affected_content_ids)
                    referenced_content_ids = set([row['file_id'] for row in cursor.fetchall()])
                    
                    orphan_content_ids = [fid for fid in affected_content_ids if fid not in referenced_content_ids]

                    if orphan_content_ids:
                        orphan_placeholders = ','.join(['?'] * len(orphan_content_ids))
                        
                        # Get message IDs for these orphans
                        cursor.execute(f"SELECT message_id FROM chunks WHERE file_id IN ({orphan_placeholders})", orphan_content_ids)
                        all_message_ids = [row['message_id'] for row in cursor.fetchall()]
                        
                        # Delete orphans
                        cursor.execute(f"DELETE FROM files WHERE id IN ({orphan_placeholders})", orphan_content_ids)

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
                
                # Check for name collision
                self._check_name_collision(cursor, parent_id, new_name, 'folder', exclude_id=folder_id)

                cursor.execute("UPDATE folders SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), folder_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def rename_file(self, map_id: int, new_name: str):
        """Renames a file mapping."""
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

                # Check for name collision
                self._check_name_collision(cursor, folder_id, new_name, 'file', exclude_id=map_id)

                cursor.execute("UPDATE file_folder_map SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), map_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def move_file(self, map_id: int, new_parent_id: int):
        """Moves a file to a new folder."""
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                # Get file map info and content size
                cursor.execute("""
                    SELECT m.folder_id, m.name, m.file_id, f.size 
                    FROM file_folder_map m
                    JOIN files f ON m.file_id = f.id
                    WHERE m.id = ?
                """, (map_id,))
                
                map_info = cursor.fetchone()
                if not map_info:
                    raise errors.PathNotFoundError(f"File with ID '{map_id}' not found.")
                
                old_parent_id = map_info['folder_id']
                file_name = map_info['name']
                file_size = map_info['size']
                
                if old_parent_id == new_parent_id:
                    return # No change needed

                # Verify destination
                if new_parent_id is not None:
                    cursor.execute("SELECT id FROM folders WHERE id = ?", (new_parent_id,))
                    if not cursor.fetchone():
                        raise errors.PathNotFoundError(f"Destination folder with ID '{new_parent_id}' not found.")

                # Check for name collision in destination
                self._check_name_collision(cursor, new_parent_id, file_name, 'file')

                # Update parent_id in mapping
                cursor.execute("UPDATE file_folder_map SET folder_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), map_id))
                
                # Update sizes
                self._update_folder_size_recursively(cursor, old_parent_id, -file_size)
                self._update_folder_size_recursively(cursor, new_parent_id, file_size)
                
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def move_folder(self, folder_id: int, new_parent_id: int):
        """Moves a folder to a new parent folder."""
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                # Get folder info
                cursor.execute("SELECT parent_id, name, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"Folder with ID '{folder_id}' not found.")
                
                old_parent_id = folder_info['parent_id']
                folder_name = folder_info['name']
                folder_size = folder_info['total_size']

                if old_parent_id == new_parent_id:
                    return

                # Verify destination exists (if not root)
                if new_parent_id is not None:
                    cursor.execute("SELECT id FROM folders WHERE id = ?", (new_parent_id,))
                    if not cursor.fetchone():
                        raise errors.PathNotFoundError(f"Destination folder with ID '{new_parent_id}' not found.")

                    # Circular dependency check: Cannot move a folder into its own subtree
                    # Traverse up from new_parent_id to see if we hit folder_id
                    current_check_id = new_parent_id
                    while current_check_id is not None:
                        if current_check_id == folder_id:
                            raise errors.InvalidNameError("Cannot move a folder into itself or its subfolders.")
                        
                        cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_check_id,))
                        res = cursor.fetchone()
                        current_check_id = res['parent_id'] if res else None

                # Check for name collision
                self._check_name_collision(cursor, new_parent_id, folder_name, 'folder')

                # Update parent_id
                cursor.execute("UPDATE folders SET parent_id = ?, modif_date = ? WHERE id = ?", 
                               (new_parent_id, time.time(), folder_id))
                
                # Update sizes
                self._update_folder_size_recursively(cursor, old_parent_id, -folder_size)
                self._update_folder_size_recursively(cursor, new_parent_id, folder_size)
                
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def get_file_details(self, map_id: int) -> dict | None:
        """
        Retrieves all necessary details for a single file to begin a download,
        including its chunks.
        
        Args:
            map_id: The ID of the file mapping to download.
        """
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
                logger.warning(f"Could not find file details for Map ID: {map_id}")
                return None
            
            file_id = file_row['file_id']

            cursor.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ? ORDER BY part_num", (file_id,))
            chunks_data = cursor.fetchall()

            return {
                "id": file_row['map_id'], # Keep consistent with UI expecting the item ID
                "file_id": file_id, # Include content ID for reference
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
            
            # Get unique content IDs for chunk retrieval
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
                    # Map chunks using content ID (file_id)
                    item['chunks'] = chunks_map.get(item['file_id'], [])

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

    def check_folder_exists(self, folder_id: int) -> bool:
        """Checks if a folder with the given ID exists."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,))
            return cursor.fetchone() is not None
        finally:
            conn.close()
