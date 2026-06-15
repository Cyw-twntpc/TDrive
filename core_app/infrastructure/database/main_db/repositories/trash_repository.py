import os
import logging
import time
import datetime
from core_app.core import errors
from .base_repository import BaseRepository


logger = logging.getLogger(__name__)

class TrashRepository(BaseRepository):
    TRASH_RETENTION_DAYS = 30

    def remove_file(self, map_id: int) -> dict | None:
        with self._db_lock:
            conn = self._get_conn()
            with conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT m.folder_id, m.file_id, f.size, f.preview_msg_id, f.map_msg_id 
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
                used_map_msg_id = map_info['map_msg_id']

                self._execute_write(cursor, "DELETE FROM file_folder_map WHERE id = ?", (map_id,), score=5)
                self._execute_write(cursor, "DELETE FROM trash_metadata WHERE item_id = ? AND item_type = 'file'", (map_id,), score=0)
                
                self._update_folder_size_recursively(cursor, folder_id, -size)
                
                cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (file_id,))
                still_referenced = cursor.fetchone() is not None

                result = {
                    "orphan": False, 
                    "file_id": file_id, 
                    "map_msg_id": used_map_msg_id,
                    "parent_id": folder_id,
                    "msg_ids_to_delete": [],
                    "map_msg_id_to_delete": None
                }

                if not still_referenced:
                    result["orphan"] = True
                    if map_info['preview_msg_id']:
                        result['msg_ids_to_delete'].append(map_info['preview_msg_id'])
                    
                    self._execute_write(cursor, "DELETE FROM files WHERE id = ?", (file_id,), score=1)
                    
                    if used_map_msg_id:
                        result["map_msg_id_to_delete"] = used_map_msg_id

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
                
                # Get thumbs_db_msg_id for all descendant folders
                cursor.execute(f"SELECT thumbs_db_msg_id FROM folders WHERE id IN ({f_placeholders})", all_folder_ids)
                for row in cursor.fetchall():
                    if row['thumbs_db_msg_id'] and row['thumbs_db_msg_id'] != 0:
                        msgs_to_delete.append(row['thumbs_db_msg_id'])
                
                # Find all files in these folders (Need map_ids for trash cleanup)
                cursor.execute(f"SELECT id FROM file_folder_map WHERE folder_id IN ({f_placeholders})", all_folder_ids)
                map_ids = [row['id'] for row in cursor.fetchall()]
                
                results = []
                if msgs_to_delete:
                    results.append({"msg_ids_to_delete": msgs_to_delete, "parent_id": folder_info['parent_id']})
                
                cursor.execute(f"SELECT m.file_id, f.size, f.preview_msg_id, f.map_msg_id FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.folder_id IN ({f_placeholders})", all_folder_ids)
                files_info = cursor.fetchall()
                
                # Delete maps (Path mappings)
                self._execute_write(cursor, f"DELETE FROM file_folder_map WHERE folder_id IN ({f_placeholders})", all_folder_ids, score=5)
                
                # [FIX] Clean up Trash Metadata for these files
                if map_ids:
                    m_placeholders = ','.join(['?'] * len(map_ids))
                    self._execute_write(cursor, f"DELETE FROM trash_metadata WHERE item_type = 'file' AND item_id IN ({m_placeholders})", map_ids, score=0)
                
                # Check orphans & Cleanup Map Files
                for finfo in files_info:
                    fid = finfo['file_id']
                    map_msg_id = finfo['map_msg_id']

                    cursor.execute("SELECT 1 FROM file_folder_map WHERE file_id = ?", (fid,))
                    if not cursor.fetchone():
                        # It's an orphan
                        self._execute_write(cursor, "DELETE FROM files WHERE id = ?", (fid,), score=1)
                        
                        res = {
                            "orphan": True,
                            "file_id": fid,
                            "msg_ids_to_delete": [],
                            "map_msg_id_to_delete": map_msg_id
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
                    cursor.execute("SELECT file_id FROM file_folder_map WHERE id = ?", (item_id,))
                    f_id = cursor.fetchone()['file_id']
                    
                    cursor.execute("SELECT thumb_src_folder_id FROM files WHERE id = ?", (f_id,))
                    curr_src = cursor.fetchone()['thumb_src_folder_id']
                    if curr_src is None:
                        self._execute_write(cursor, "UPDATE files SET thumb_src_folder_id = ? WHERE id = ?", (info['parent_id'], f_id), score=1)
                                   
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
        return {"folders": folders, "files": files, "recycle_bin_id": recycle_bin_id}

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

    def get_expired_items(self) -> list:
        conn = self._get_conn()
        cursor = conn.cursor()
        cutoff_date = time.time() - (self.TRASH_RETENTION_DAYS * 86400)
        
        cursor.execute("SELECT item_id, item_type FROM trash_metadata WHERE trashed_date < ?", (cutoff_date,))
        rows = cursor.fetchall()
        return [{'id': row['item_id'], 'type': row['item_type']} for row in rows]

