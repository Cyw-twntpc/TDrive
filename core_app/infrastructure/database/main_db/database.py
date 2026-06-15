import sqlite3
import logging
from core_app.application.sync.sync_manager import SyncManager
import threading
import time
from core_app.infrastructure.database.main_db.transaction_logger import TransactionLogger

logger = logging.getLogger(__name__)

class DatabaseConnection:
    _instance = None
    _lock = threading.Lock()
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(DatabaseConnection, cls).__new__(cls)
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
            conn = sqlite3.connect(db_path, cached_statements=0)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL;')
            return conn

        if self._connection is None:
            self._connection = sqlite3.connect(':memory:', check_same_thread=False, cached_statements=0)
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
            FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE,
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
            map_msg_id INTEGER,
            thumb_src_folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL
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

