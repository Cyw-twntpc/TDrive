import sqlite3
import os
import logging
from typing import Dict

logger = logging.getLogger(__name__)

class TransferDatabaseConnection:
    SCHEMA_DEFINITION = {'main_tasks': {'columns': {'task_id': 'TEXT PRIMARY KEY', 'type': 'TEXT NOT NULL', 'is_folder': 'INTEGER DEFAULT 0', 'status': "TEXT DEFAULT 'queued'", 'total_size': 'INTEGER DEFAULT 0', 'created_at': 'REAL', 'updated_at': 'REAL', 'local_path': 'TEXT', 'remote_id': 'INTEGER', 'error_message': 'TEXT'}, 'constraints': []}, 'sub_tasks': {'columns': {'sub_task_id': 'TEXT PRIMARY KEY', 'main_task_id': 'TEXT NOT NULL', 'status': "TEXT DEFAULT 'queued'", 'stage': "TEXT DEFAULT 'init'", 'local_path': 'TEXT NOT NULL', 'remote_id': 'INTEGER', 'total_size': 'INTEGER DEFAULT 0', 'file_hash': 'TEXT', 'preview_msg_id': 'INTEGER', 'file_details_json': 'TEXT'}, 'constraints': ['FOREIGN KEY (main_task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE']}, 'task_progress': {'columns': {'sub_task_id': 'TEXT NOT NULL', 'part_num': 'INTEGER NOT NULL', 'message_id': 'INTEGER', 'part_hash': 'TEXT'}, 'constraints': ['PRIMARY KEY (sub_task_id, part_num)', 'FOREIGN KEY (sub_task_id) REFERENCES sub_tasks (sub_task_id) ON DELETE CASCADE']}, 'traffic_stats': {'columns': {'date': 'TEXT PRIMARY KEY', 'bytes': 'INTEGER DEFAULT 0'}, 'constraints': []}, 'created_artifacts': {'columns': {'id': 'INTEGER PRIMARY KEY AUTOINCREMENT', 'task_id': 'TEXT NOT NULL', 'artifact_type': 'TEXT NOT NULL', 'db_id': 'INTEGER NOT NULL', 'created_at': 'REAL'}, 'constraints': ['FOREIGN KEY (task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE']}, 'task_thumbnails': {'columns': {'id': 'INTEGER PRIMARY KEY AUTOINCREMENT', 'task_id': 'TEXT NOT NULL', 'target_folder_id': 'INTEGER NOT NULL', 'file_id': 'INTEGER NOT NULL', 'thumbnail_blob': 'BLOB'}, 'constraints': ['FOREIGN KEY (task_id) REFERENCES main_tasks (task_id) ON DELETE CASCADE']}}

    def __init__(self, db_path='./file/user_data/transfer_history.db'):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, cached_statements=0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute('PRAGMA journal_mode=WAL;')
            for table_name, definition in self.SCHEMA_DEFINITION.items():
                self._sync_table_schema(conn, table_name, definition)
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sub_main ON sub_tasks(main_task_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_main_updated ON main_tasks(updated_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_artifacts_task ON created_artifacts(task_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_thumbs_task ON task_thumbnails(task_id)')
            conn.commit()
        except Exception as e:
            logger.error(f'Failed to initialize transfer DB: {e}')
        finally:
            conn.close()

    def _sync_table_schema(self, conn: sqlite3.Connection, table_name: str, definition: Dict):
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cursor.fetchone():
            self._create_table(cursor, table_name, definition)
            return
        cursor.execute(f'PRAGMA table_info({table_name})')
        current_cols_info = cursor.fetchall()
        current_cols = {row['name'] for row in current_cols_info}
        target_cols = set(definition['columns'].keys())
        missing = target_cols - current_cols
        extra = current_cols - target_cols
        if not missing and (not extra):
            return
        logger.info(f'Schema mismatch for {table_name}. Missing: {missing}, Extra: {extra}. Rebuilding...')
        self._rebuild_table(conn, table_name, definition, current_cols & target_cols)

    def _create_table(self, cursor: sqlite3.Cursor, table_name: str, definition: Dict):
        cols_def = [f'{col} {dtype}' for col, dtype in definition['columns'].items()]
        constraints = definition.get('constraints', [])
        full_def = ', '.join(cols_def + constraints)
        sql = f'CREATE TABLE {table_name} ({full_def})'
        cursor.execute(sql)

    def _rebuild_table(self, conn: sqlite3.Connection, table_name: str, definition: Dict, common_cols: set):
        """
            Recreates the table preserving data for common columns.
            SQLite standard migration pattern: New -> Copy -> Drop Old -> Rename
            """
        temp_table = f'{table_name}_new'
        cursor = conn.cursor()
        self._create_table(cursor, temp_table, definition)
        if common_cols:
            cols_str = ', '.join(list(common_cols))
            cursor.execute(f'INSERT INTO {temp_table} ({cols_str}) SELECT {cols_str} FROM {table_name}')
        cursor.execute(f'DROP TABLE {table_name}')
        cursor.execute(f'ALTER TABLE {temp_table} RENAME TO {table_name}')

