import sqlite3
import gzip
import os
import logging
import threading

logger = logging.getLogger(__name__)

DB_PATH = "./file/user_data/local_previews.db"
MAX_CACHE_SIZE = 500 * 1024 * 1024  # 500 MB compressed


class PreviewCacheManager:
    """SQLite-backed LRU cache for text and document previews."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS preview_cache (
                        content_id INTEGER PRIMARY KEY,
                        data BLOB NOT NULL,
                        content_type TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        last_accessed_at REAL DEFAULT (julianday('now')),
                        compressed_size INTEGER NOT NULL,
                        orig_size INTEGER,
                        last_chunk INTEGER DEFAULT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def get(self, content_id: int):
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "SELECT * FROM preview_cache WHERE content_id = ?", (content_id,)
                )
                row = cur.fetchone()
                if row:
                    conn.execute(
                        "UPDATE preview_cache SET last_accessed_at = julianday('now') WHERE content_id = ?",
                        (content_id,),
                    )
                    conn.commit()
                    return dict(row)
                return None
            finally:
                conn.close()

    def put(self, content_id: int, data: bytes, content_type: str, source_hash: str, orig_size: int, last_chunk=None):
        with self._lock:
            conn = self._get_conn()
            try:
                compressed = gzip.compress(data)
                self._evict_if_needed(len(compressed))
                conn.execute(
                    """INSERT OR REPLACE INTO preview_cache
                       (content_id, data, content_type, source_hash, compressed_size, orig_size, last_chunk)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (content_id, compressed, content_type, source_hash, len(compressed), orig_size, last_chunk),
                )
                conn.commit()
            finally:
                conn.close()

    def update(self, content_id: int, new_data: bytes, last_chunk: int):
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "SELECT data, orig_size FROM preview_cache WHERE content_id = ?",
                    (content_id,),
                )
                row = cur.fetchone()
                if row:
                    existing = gzip.decompress(row[0])
                    merged = existing + new_data
                    recompressed = gzip.compress(merged)
                    self._evict_if_needed(len(recompressed))
                    conn.execute(
                        """UPDATE preview_cache
                           SET data = ?, compressed_size = ?, orig_size = ?, last_chunk = ?,
                               last_accessed_at = julianday('now')
                           WHERE content_id = ?""",
                        (recompressed, len(recompressed), row[1], last_chunk, content_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    def delete(self, content_id: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM preview_cache WHERE content_id = ?", (content_id,))
                conn.commit()
            finally:
                conn.close()

    def get_total_size(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COALESCE(SUM(compressed_size), 0) FROM preview_cache")
                return cur.fetchone()[0]
            finally:
                conn.close()

    def evict(self, incoming_size: int):
        """Evict LRU items until total + incoming <= MAX_CACHE_SIZE."""
        with self._lock:
            conn = self._get_conn()
            try:
                total = conn.execute("SELECT COALESCE(SUM(compressed_size), 0) FROM preview_cache").fetchone()[0]
                target = MAX_CACHE_SIZE - incoming_size
                while total > target:
                    row = conn.execute(
                        "SELECT content_id, compressed_size FROM preview_cache ORDER BY last_accessed_at ASC LIMIT 1"
                    ).fetchone()
                    if not row:
                        break
                    conn.execute("DELETE FROM preview_cache WHERE content_id = ?", (row[0],))
                    total -= row[1]
                conn.commit()
            finally:
                conn.close()

    def _evict_if_needed(self, incoming_size: int):
        total = self._get_total_size_unlocked()
        if total + incoming_size > MAX_CACHE_SIZE:
            self._evict_unlocked(incoming_size)

    def _get_total_size_unlocked(self) -> int:
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT COALESCE(SUM(compressed_size), 0) FROM preview_cache")
            return cur.fetchone()[0]
        finally:
            conn.close()

    def _evict_unlocked(self, incoming_size: int):
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COALESCE(SUM(compressed_size), 0) FROM preview_cache").fetchone()[0]
            target = MAX_CACHE_SIZE - incoming_size
            while total > target:
                row = conn.execute(
                    "SELECT content_id, compressed_size FROM preview_cache ORDER BY last_accessed_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    break
                conn.execute("DELETE FROM preview_cache WHERE content_id = ?", (row[0],))
                total -= row[1]
            conn.commit()
        finally:
            conn.close()
