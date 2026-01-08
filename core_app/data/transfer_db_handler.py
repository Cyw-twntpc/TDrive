import sqlite3
import os
import json
import logging
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class TransferDBHandler:
    def __init__(self, db_path='./file/transfer_history.db'):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS main_tasks (
                task_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                is_folder INTEGER DEFAULT 0,
                status TEXT DEFAULT 'queued',
                total_size INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL,
                local_path TEXT,
                remote_id INTEGER,
                error_message TEXT
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS sub_tasks (
                sub_task_id TEXT PRIMARY KEY,
                main_task_id TEXT NOT NULL,
                status TEXT DEFAULT 'queued',
                stage TEXT DEFAULT 'init',
                local_path TEXT NOT NULL,
                remote_id INTEGER,
                total_size INTEGER DEFAULT 0,
                file_hash TEXT,
                file_details_json TEXT,
                FOREIGN KEY (main_task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_progress (
                sub_task_id TEXT NOT NULL,
                part_num INTEGER NOT NULL,
                message_id INTEGER,
                part_hash TEXT,
                PRIMARY KEY (sub_task_id, part_num),
                FOREIGN KEY (sub_task_id) REFERENCES sub_tasks (sub_task_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS traffic_stats (
                date TEXT PRIMARY KEY,
                bytes INTEGER DEFAULT 0
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS created_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL, -- 'file' or 'folder'
                db_id INTEGER NOT NULL,
                created_at REAL,
                FOREIGN KEY (task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute("DROP TABLE IF EXISTS task_thumbnails")
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_thumbnails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                target_folder_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                thumbnail_blob BLOB,
                FOREIGN KEY (task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE
            )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sub_main ON sub_tasks(main_task_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_main_updated ON main_tasks(updated_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_artifacts_task ON created_artifacts(task_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_thumbs_task ON task_thumbnails(task_id)')

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to initialize transfer DB: {e}")
        finally:
            conn.close()

    # --- Artifact Tracking ---

    def add_created_artifact(self, task_id: str, artifact_type: str, db_id: int):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute('''
                INSERT INTO created_artifacts (task_id, artifact_type, db_id, created_at)
                VALUES (?, ?, ?, ?)
                ''', (task_id, artifact_type, db_id, time.time()))
        except Exception as e:
            logger.error(f"Error adding created artifact: {e}")
        finally:
            conn.close()

    def get_created_artifacts(self, task_id: str) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM created_artifacts WHERE task_id = ? ORDER BY id DESC", (task_id,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update_sub_task_stage(self, sub_task_id: str, stage: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE sub_tasks SET stage = ? WHERE sub_task_id = ?", (stage, sub_task_id))
        finally:
            conn.close()

    # --- Thumbnail Staging ---

    def add_task_thumbnail(self, task_id: str, folder_id: int, file_id: int, thumb_blob: bytes):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute('''
                INSERT INTO task_thumbnails (task_id, target_folder_id, file_id, thumbnail_blob)
                VALUES (?, ?, ?, ?)
                ''', (task_id, folder_id, file_id, thumb_blob))
        except Exception as e:
            logger.error(f"Error adding task thumbnail: {e}")
        finally:
            conn.close()

    def get_task_thumbnails(self, task_id: str) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT task_id, target_folder_id, file_id, thumbnail_blob FROM task_thumbnails WHERE task_id = ?", (task_id,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
            
    def delete_task_thumbnails(self, task_id: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM task_thumbnails WHERE task_id = ?", (task_id,))
        finally:
            conn.close()

    # --- Traffic Statistics ---

    def get_traffic_by_date(self, date_str: str) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT bytes FROM traffic_stats WHERE date = ?", (date_str,))
            row = cursor.fetchone()
            return row['bytes'] if row else 0
        finally:
            conn.close()

    def update_traffic(self, date_str: str, bytes_delta: int):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute('''
                INSERT INTO traffic_stats (date, bytes) VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET bytes = bytes + excluded.bytes
                ''', (date_str, bytes_delta))
        finally:
            conn.close()

    # --- CRUD Operations ---

    def create_main_task(self, task_data: Dict[str, Any]):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute('''
                INSERT INTO main_tasks (
                    task_id, type, is_folder, status, total_size, 
                    created_at, updated_at, local_path, remote_id, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    task_data['task_id'],
                    task_data['type'],
                    1 if task_data.get('is_folder') else 0,
                    task_data.get('status', 'queued'),
                    task_data.get('total_size', 0),
                    task_data.get('created_at'),
                    task_data.get('updated_at'),
                    task_data.get('local_path'),
                    task_data.get('remote_id'),
                    task_data.get('error_message', '')
                ))
        except Exception as e:
            logger.error(f"Error creating main task {task_data.get('task_id')}: {e}")
        finally:
            conn.close()

    def create_sub_tasks_bulk(self, sub_tasks: List[Dict[str, Any]]):
        if not sub_tasks: return
        conn = self._get_conn()
        try:
            with conn:
                conn.executemany('''
                INSERT INTO sub_tasks (
                    sub_task_id, main_task_id, status, local_path, 
                    remote_id, total_size, file_hash, file_details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', [
                    (
                        st['sub_task_id'],
                        st['main_task_id'],
                        st.get('status', 'queued'),
                        st['local_path'],
                        st.get('remote_id'),
                        st.get('total_size', 0),
                        st.get('file_hash'),
                        json.dumps(st.get('file_details')) if st.get('file_details') else None
                    ) for st in sub_tasks
                ])
        except Exception as e:
            logger.error(f"Error creating sub tasks: {e}")
        finally:
            conn.close()

    def update_main_task_status(self, task_id: str, status: str, updated_at: float, error_msg: str = None):
        conn = self._get_conn()
        try:
            with conn:
                query = "UPDATE main_tasks SET status = ?, updated_at = ?"
                params = [status, updated_at]
                if error_msg is not None:
                    query += ", error_message = ?"
                    params.append(error_msg)
                query += " WHERE task_id = ?"
                params.append(task_id)
                conn.execute(query, params)
        finally:
            conn.close()

    def update_main_task_total_size(self, task_id: str, new_size: int):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE main_tasks SET total_size = ? WHERE task_id = ?", (new_size, task_id))
        finally:
            conn.close()

    def update_sub_task_status(self, sub_task_id: str, status: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE sub_tasks SET status = ? WHERE sub_task_id = ?", (status, sub_task_id))
        finally:
            conn.close()
            
    def update_sub_task_hash(self, sub_task_id: str, file_hash: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE sub_tasks SET file_hash = ? WHERE sub_task_id = ?", (file_hash, sub_task_id))
        finally:
            conn.close()

    def add_progress_part(self, sub_task_id: str, part_num: int, message_id: int = None, part_hash: str = None):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute('''
                INSERT OR REPLACE INTO task_progress (sub_task_id, part_num, message_id, part_hash)
                VALUES (?, ?, ?, ?)
                ''', (sub_task_id, part_num, message_id, part_hash))
        finally:
            conn.close()

    def delete_task(self, task_id: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM main_tasks WHERE task_id = ?", (task_id,))
        finally:
            conn.close()

    def get_main_task_status(self, task_id: str) -> Optional[str]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM main_tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            return row['status'] if row else None
        finally:
            conn.close()

    def get_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        conn = self._get_conn()
        result = {"uploads": {}, "downloads": {}}
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM main_tasks")
            main_rows = cursor.fetchall()
            if not main_rows: return result

            tasks_map = {}
            for row in main_rows:
                task = dict(row)
                task['is_folder'] = bool(task['is_folder'])
                task_data = {
                    "task_id": task['task_id'],
                    "type": task['type'],
                    "is_folder": task['is_folder'],
                    "status": task['status'],
                    "total_size": task['total_size'],
                    "created_at": task['created_at'],
                    "updated_at": task['updated_at'],
                    "error_message": task['error_message'],
                    "file_path": task['local_path'] if task['type'] == 'upload' else None,
                    "parent_id": task['remote_id'] if task['type'] == 'upload' else None,
                    "save_path": task['local_path'] if task['type'] == 'download' else None,
                    "db_id": task['remote_id'] if task['type'] == 'download' else None,
                    "child_tasks": {}
                }
                if not task['is_folder']:
                    task_data.update({"file_hash": None, "transferred_parts": [], "split_files_info": [], "file_details": None})
                target_dict = result["uploads"] if task['type'] == 'upload' else result["downloads"]
                target_dict[task['task_id']] = task_data
                tasks_map[task['task_id']] = task_data

            cursor.execute("SELECT * FROM sub_tasks")
            sub_rows = cursor.fetchall()
            sub_tasks_map = {}
            for row in sub_rows:
                st = dict(row)
                main_id = st['main_task_id']
                if main_id not in tasks_map: continue
                main_task = tasks_map[main_id]
                sub_data = {
                    "status": st['status'],
                    "file_path": st['local_path'],
                    "save_path": st['local_path'],
                    "parent_id": st['remote_id'],
                    "db_id": st['remote_id'],
                    "total_size": st['total_size'],
                    "transferred_parts": [],
                }
                if main_task['type'] == 'upload':
                    sub_data['file_hash'] = st['file_hash']
                    sub_data['split_files_info'] = []
                elif main_task['type'] == 'download' and st['file_details_json']:
                    sub_data['file_details'] = json.loads(st['file_details_json'])

                if main_task['is_folder']:
                    main_task['child_tasks'][st['sub_task_id']] = sub_data
                    sub_tasks_map[st['sub_task_id']] = sub_data
                else:
                    if main_task['type'] == 'upload': main_task['file_hash'] = sub_data['file_hash']
                    elif main_task['type'] == 'download': main_task['file_details'] = sub_data.get('file_details')
                    sub_tasks_map[st['sub_task_id']] = main_task 

            cursor.execute("SELECT * FROM task_progress ORDER BY sub_task_id, part_num")
            for row in cursor.fetchall():
                sub_id = row['sub_task_id']
                if sub_id not in sub_tasks_map: continue
                target = sub_tasks_map[sub_id]
                target['transferred_parts'].append(row['part_num'])
                if 'split_files_info' in target and row['message_id']:
                    target['split_files_info'].append([row['part_num'], row['message_id'], row['part_hash']])
        except Exception as e:
            logger.error(f"Error loading tasks from DB: {e}")
            return {"uploads": {}, "downloads": {}}
        finally:
            conn.close()
        return result

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM main_tasks WHERE task_id = ?", (task_id,))
            main_row = cursor.fetchone()
            if not main_row: return None
            
            main_task = dict(main_row)
            is_folder = bool(main_task['is_folder'])
            task_type = main_task['type']
            task_data = {
                "task_id": main_task['task_id'],
                "type": task_type,
                "is_folder": is_folder,
                "status": main_task['status'],
                "total_size": main_task['total_size'],
                "created_at": main_task['created_at'],
                "updated_at": main_task['updated_at'],
                "error_message": main_task['error_message'],
                "file_path": main_task['local_path'] if task_type == 'upload' else None,
                "parent_id": main_task['remote_id'] if task_type == 'upload' else None,
                "save_path": main_task['local_path'] if task_type == 'download' else None,
                "db_id": main_task['remote_id'] if task_type == 'download' else None,
                "child_tasks": {}
            }
            if not is_folder:
                task_data.update({"file_hash": None, "transferred_parts": [], "split_files_info": [], "file_details": None})

            cursor.execute("SELECT * FROM sub_tasks WHERE main_task_id = ?", (task_id,))
            sub_tasks_refs = {}
            for s_row in cursor.fetchall():
                st = dict(s_row)
                sub_id = st['sub_task_id']
                sub_data = {
                    "status": st['status'], "file_path": st['local_path'], "save_path": st['local_path'],
                    "parent_id": st['remote_id'], "db_id": st['remote_id'], "total_size": st['total_size'],
                    "transferred_parts": [],
                }
                if task_type == 'upload':
                    sub_data['file_hash'] = st['file_hash']
                    sub_data['split_files_info'] = []
                elif task_type == 'download' and st['file_details_json']:
                    sub_data['file_details'] = json.loads(st['file_details_json'])

                if is_folder:
                    task_data['child_tasks'][sub_id] = sub_data
                    sub_tasks_refs[sub_id] = sub_data
                else:
                    if task_type == 'upload': task_data['file_hash'] = sub_data['file_hash']
                    else: task_data['file_details'] = sub_data.get('file_details')
                    sub_tasks_refs[sub_id] = task_data

            cursor.execute("""
                SELECT p.* FROM task_progress p
                JOIN sub_tasks s ON p.sub_task_id = s.sub_task_id
                WHERE s.main_task_id = ?
                ORDER BY p.part_num
            """, (task_id,))
            for p_row in cursor.fetchall():
                sid = p_row['sub_task_id']
                if sid in sub_tasks_refs:
                    target = sub_tasks_refs[sid]
                    target['transferred_parts'].append(p_row['part_num'])
                    if 'split_files_info' in target and p_row['message_id']:
                        target['split_files_info'].append([p_row['part_num'], p_row['message_id'], p_row['part_hash']])
            return task_data
        except Exception as e:
            logger.error(f"Error fetching task {task_id}: {e}")
            return None
        finally:
            conn.close()

    def reset_zombie_tasks(self):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE main_tasks SET status = 'paused' WHERE status = 'transferring'")
        finally:
            conn.close()

    def pause_active_sub_tasks(self, main_task_id: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE sub_tasks SET status = 'paused' WHERE main_task_id = ? AND status = 'transferring'", (main_task_id,))
        except Exception as e:
            logger.error(f"Error pausing sub-tasks for {main_task_id}: {e}")
        finally:
            conn.close()
