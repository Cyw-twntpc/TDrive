import logging
import time
from typing import Optional
from core_app.core import errors
from .base_repository import BaseRepository


logger = logging.getLogger(__name__)

class FolderRepository(BaseRepository):
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

    def update_folder_thumbs_info(self, folder_id: int, msg_id: int, hash_val: str):
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                self._execute_write(conn.cursor(), "UPDATE folders SET thumbs_db_msg_id = ?, thumbs_db_hash = ? WHERE id = ?", 
                                    (msg_id, hash_val, folder_id), score=1)

    def get_fragmented_folders(self, folder_ids: list[int]) -> list[int]:
        """
        Evaluates the given folders and returns a list of folder IDs that meet the fragmentation condition:
        1. Fragment sources > 3 OR
        2. Fragmented files >= 50% of total files
        """
        if not folder_ids:
            return []
            
        conn = self._get_conn()
        cursor = conn.cursor()
        
        placeholders = ','.join(['?'] * len(folder_ids))
        
        # We use a single query grouping by folder_id
        query = f"""
            SELECT m.folder_id, 
                   COUNT(f.id) as total_files,
                   COUNT(f.thumb_src_folder_id) as frag_count,
                   COUNT(DISTINCT f.thumb_src_folder_id) as source_count
            FROM file_folder_map m
            JOIN files f ON m.file_id = f.id
            WHERE m.folder_id IN ({placeholders})
            GROUP BY m.folder_id
        """
        cursor.execute(query, folder_ids)
        
        fragmented = []
        for row in cursor.fetchall():
            f_id = row['folder_id']
            total = row['total_files']
            frag_count = row['frag_count']
            source_count = row['source_count']
            
            if total == 0:
                continue
                
            if source_count > 3 or (frag_count / total) >= 0.5:
                fragmented.append(f_id)
                
        return fragmented

    def mark_files_thumb_src(self, file_ids: list, src_folder_id: Optional[int]):
        """Marks or clears the thumb_src_folder_id for moved files."""
        if not file_ids: return
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                placeholders = ",".join("?" for _ in file_ids)
                query = f"UPDATE files SET thumb_src_folder_id = ? WHERE id IN ({placeholders})"
                params = [src_folder_id] + file_ids
                self._execute_write(conn, query, tuple(params), score=1)

    def get_thumb_src_folders(self, file_ids: list) -> dict:
        """Returns a mapping of src_folder_id -> list of file_ids"""
        if not file_ids: return {}
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in file_ids)
        cursor = conn.cursor()
        cursor.execute(f"SELECT id, thumb_src_folder_id FROM files WHERE id IN ({placeholders}) AND thumb_src_folder_id IS NOT NULL", file_ids)
        
        result = {}
        for row in cursor.fetchall():
            src = row['thumb_src_folder_id']
            if src not in result:
                result[src] = []
            result[src].append(row['id'])
        return result

    def check_folder_exists(self, folder_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,))
        return cursor.fetchone() is not None

