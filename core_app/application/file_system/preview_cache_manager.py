import gzip
import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from typing import Optional

from core_app.core import crypto_handler
from core_app.core.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

DB_PATH = "./file/user_data/local_previews.db"
MAX_TOTAL_CACHE_SIZE = 3 * 1024 * 1024 * 1024  # 3 GB global cap
MAX_CACHE_SIZE = MAX_TOTAL_CACHE_SIZE
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024  # 512 MB disk reserve watermark

CATEGORY_QUOTAS: dict[str, int] = {
    "video_chunk": 1536 * 1024 * 1024,  # 1.5 GB
    "audio_chunk": 800 * 1024 * 1024,   # 800 MB
    "document": 500 * 1024 * 1024,      # 500 MB
    "image": 300 * 1024 * 1024,         # 300 MB
    "text": 200 * 1024 * 1024,          # 200 MB
}

_CONTENT_TYPE_TO_CATEGORY = {
    "video_chunk": "video_chunk",
    "audio_chunk": "audio_chunk",
    "document_preview": "document",
    "document_full": "document",
    "image_preview": "image",
    "text": "text",
}


def _category_for_content_type(content_type: str) -> str:
    return _CONTENT_TYPE_TO_CATEGORY.get(content_type, "document")


def _hash_digest(source_hash: str) -> str:
    """Compute a one-way digest for SQLite metadata so raw file hashes are never persisted."""
    return hashlib.sha256(f"tdrive-cache-v2:{source_hash}".encode("utf-8")).hexdigest()


def _derive_key(source_hash: str) -> bytes:
    """Derive the AES-256-GCM key matching Telegram chunk encryption."""
    return crypto_handler.generate_key(source_hash[:32], source_hash[-32:])


class PreviewCacheManager:
    """SQLite-indexed, encrypted file-backed LRU cache with per-category quotas and read tracking."""

    def __init__(self, db_path: str = DB_PATH):
        if db_path in (
            "./file/user_data/local_previews.db",
            "file/user_data/local_previews.db",
            os.path.abspath("./file/user_data/local_previews.db"),
            os.path.abspath("file/user_data/local_previews.db"),
        ):
            db_path = os.path.join(SettingsManager().settings_dir, "local_previews.db")
        self._db_path = db_path
        self._blob_dir = os.path.join(
            os.path.dirname(os.path.abspath(self._db_path)), "preview_cache_blobs"
        )
        self._lock = threading.Lock()
        self._closed = False
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        os.makedirs(self._blob_dir, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError as exc:
            logger.debug("Failed to unlink cache blob %s: %s", path, exc)

    def _abs_blob_path(self, rel_path: str) -> str:
        return os.path.join(self._blob_dir, rel_path)

    def _write_blob_atomic(self, abs_path: str, payload: bytes) -> None:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        tmp_path = f"{abs_path}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, abs_path)
        except OSError:
            self._safe_unlink(tmp_path)
            raise

    def _cleanup_orphan_blobs(self, conn: sqlite3.Connection) -> None:
        """Remove orphan .tmp or unindexed .enc files left behind by interrupted writes."""
        try:
            referenced = {
                os.path.normpath(self._abs_blob_path(row[0]))
                for row in conn.execute("SELECT rel_path FROM preview_cache").fetchall()
            }
            for root, _, filenames in os.walk(self._blob_dir):
                for fname in filenames:
                    full_path = os.path.normpath(os.path.join(root, fname))
                    if fname.endswith(".tmp") or full_path not in referenced:
                        self._safe_unlink(full_path)
        except OSError as exc:
            logger.debug("Orphan cache blob cleanup skipped: %s", exc)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                conn.execute("PRAGMA journal_mode = WAL")

                existing = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='preview_cache'"
                ).fetchone()
                if existing is not None:
                    cols = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(preview_cache)").fetchall()
                    }
                    if "cache_key" not in cols or "access_count" not in cols:
                        conn.execute("DROP TABLE IF EXISTS preview_cache")
                        shutil.rmtree(self._blob_dir, ignore_errors=True)
                        os.makedirs(self._blob_dir, exist_ok=True)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS preview_cache (
                        cache_key TEXT PRIMARY KEY,
                        content_id INTEGER NOT NULL,
                        chunk_index INTEGER NOT NULL DEFAULT -1,
                        category TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        source_hash_digest TEXT NOT NULL,
                        rel_path TEXT NOT NULL,
                        stored_size INTEGER NOT NULL,
                        orig_size INTEGER NOT NULL,
                        is_compressed INTEGER NOT NULL DEFAULT 0,
                        is_encrypted INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        last_accessed_at REAL NOT NULL,
                        access_count INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_preview_cache_cat_lru "
                    "ON preview_cache(category, last_accessed_at ASC, access_count ASC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_preview_cache_global_lru "
                    "ON preview_cache(last_accessed_at ASC, access_count ASC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_preview_cache_content "
                    "ON preview_cache(content_id)"
                )
                self._cleanup_orphan_blobs(conn)
                conn.commit()
            finally:
                conn.close()

    def _delete_row_and_blob(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        conn.execute("DELETE FROM preview_cache WHERE cache_key = ?", (row["cache_key"],))
        self._safe_unlink(self._abs_blob_path(row["rel_path"]))

    def _evict_for_write(
        self,
        conn: sqlite3.Connection,
        incoming_size: int,
        category: str,
        exclude_key: str,
    ) -> bool:
        """Enforce per-category quota, global cap, and disk free-space watermark using LRU read order."""
        total_cap = min(MAX_TOTAL_CACHE_SIZE, MAX_CACHE_SIZE)
        cat_cap = min(CATEGORY_QUOTAS.get(category, total_cap), total_cap)
        if incoming_size > cat_cap or incoming_size > total_cap:
            logger.debug(
                "Skipping cache write for %s (%d bytes): exceeds category (%d) or total (%d) cap",
                exclude_key,
                incoming_size,
                cat_cap,
                total_cap,
            )
            return False

        # Step A: Enforce per-category quota (least-recently-read first)
        cat_total = conn.execute(
            "SELECT COALESCE(SUM(stored_size), 0) FROM preview_cache WHERE category = ? AND cache_key != ?",
            (category, exclude_key),
        ).fetchone()[0]
        cat_target = cat_cap - incoming_size
        while cat_total > cat_target:
            row = conn.execute(
                """SELECT cache_key, rel_path, stored_size
                   FROM preview_cache
                   WHERE category = ? AND cache_key != ?
                   ORDER BY last_accessed_at ASC, access_count ASC, created_at ASC
                   LIMIT 1""",
                (category, exclude_key),
            ).fetchone()
            if not row:
                break
            self._delete_row_and_blob(conn, row)
            cat_total -= row["stored_size"]

        # Step B: Enforce global total cap across all categories
        global_total = conn.execute(
            "SELECT COALESCE(SUM(stored_size), 0) FROM preview_cache WHERE cache_key != ?",
            (exclude_key,),
        ).fetchone()[0]
        global_target = total_cap - incoming_size
        while global_total > global_target:
            row = conn.execute(
                """SELECT cache_key, rel_path, stored_size
                   FROM preview_cache
                   WHERE cache_key != ?
                   ORDER BY last_accessed_at ASC, access_count ASC, created_at ASC
                   LIMIT 1""",
                (exclude_key,),
            ).fetchone()
            if not row:
                break
            self._delete_row_and_blob(conn, row)
            global_total -= row["stored_size"]

        # Step C: Disk free-space guard (fail-open if disk stays below reserve watermark)
        while True:
            try:
                free_bytes = shutil.disk_usage(self._blob_dir).free
            except OSError:
                break
            if free_bytes - incoming_size >= MIN_FREE_DISK_BYTES:
                break
            row = conn.execute(
                """SELECT cache_key, rel_path, stored_size
                   FROM preview_cache
                   WHERE cache_key != ?
                   ORDER BY last_accessed_at ASC, access_count ASC, created_at ASC
                   LIMIT 1""",
                (exclude_key,),
            ).fetchone()
            if not row:
                logger.warning(
                    "Low disk space (%d free bytes < %d reserve); skipping disk cache write for %s",
                    free_bytes,
                    MIN_FREE_DISK_BYTES,
                    exclude_key,
                )
                return False
            self._delete_row_and_blob(conn, row)

        return True

    def put(
        self,
        content_id: int,
        data: bytes,
        content_type: str,
        source_hash: str,
        orig_size: int,
        last_chunk=None,
        compress: Optional[bool] = None,
    ) -> bool:
        """Encrypt (and optionally gzip) a whole-file preview payload and store it on disk."""
        del last_chunk
        category = _category_for_content_type(content_type)
        use_gzip = (category == "text") if compress is None else bool(compress)
        raw_payload = gzip.compress(data) if use_gzip else data
        if source_hash:
            stored_payload = crypto_handler.encrypt(raw_payload, _derive_key(source_hash))
            is_encrypted = 1
        else:
            stored_payload = raw_payload
            is_encrypted = 0

        cache_key = f"file:{content_id}"
        rel_path = os.path.join(category, f"file_{content_id}.enc")
        abs_path = self._abs_blob_path(rel_path)
        digest = _hash_digest(source_hash) if source_hash else ""
        now = time.time()

        with self._lock:
            if self._closed:
                return False
            conn = self._get_conn()
            try:
                if not self._evict_for_write(conn, len(stored_payload), category, cache_key):
                    conn.commit()
                    return False

                old_row = conn.execute(
                    "SELECT rel_path FROM preview_cache WHERE cache_key = ?", (cache_key,)
                ).fetchone()
                self._write_blob_atomic(abs_path, stored_payload)
                if old_row and os.path.normpath(old_row["rel_path"]) != os.path.normpath(rel_path):
                    self._safe_unlink(self._abs_blob_path(old_row["rel_path"]))

                conn.execute(
                    """INSERT OR REPLACE INTO preview_cache
                       (cache_key, content_id, chunk_index, category, content_type,
                        source_hash_digest, rel_path, stored_size, orig_size,
                        is_compressed, is_encrypted, created_at, last_accessed_at, access_count)
                       VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        cache_key,
                        content_id,
                        category,
                        content_type,
                        digest,
                        rel_path,
                        len(stored_payload),
                        int(orig_size),
                        1 if use_gzip else 0,
                        is_encrypted,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
            except (OSError, sqlite3.OperationalError) as exc:
                logger.warning("Preview disk cache write skipped (fail-open) for %s: %s", cache_key, exc)
                try:
                    conn.rollback()
                except sqlite3.Error as rb_exc:
                    logger.debug("Preview cache rollback skipped for %s: %s", cache_key, rb_exc)
                self._safe_unlink(abs_path)
                return False
            finally:
                conn.close()

    def put_chunk(
        self,
        content_id: int,
        chunk_index: int,
        encrypted_data: bytes,
        category: str,
        source_hash: str,
        orig_size: int,
    ) -> bool:
        """Persist raw Telegram AES-256-GCM ciphertext for a media chunk without re-encrypting."""
        if category not in ("video_chunk", "audio_chunk"):
            raise ValueError(f"Unsupported chunk cache category: {category}")
        if not encrypted_data:
            return False

        cache_key = f"chunk:{content_id}:{chunk_index}"
        rel_path = os.path.join(category, f"chunk_{content_id}_{chunk_index}.enc")
        abs_path = self._abs_blob_path(rel_path)
        digest = _hash_digest(source_hash) if source_hash else ""
        now = time.time()

        with self._lock:
            if self._closed:
                return False
            conn = self._get_conn()
            try:
                if not self._evict_for_write(conn, len(encrypted_data), category, cache_key):
                    conn.commit()
                    return False

                self._write_blob_atomic(abs_path, encrypted_data)
                conn.execute(
                    """INSERT OR REPLACE INTO preview_cache
                       (cache_key, content_id, chunk_index, category, content_type,
                        source_hash_digest, rel_path, stored_size, orig_size,
                        is_compressed, is_encrypted, created_at, last_accessed_at, access_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 0)""",
                    (
                        cache_key,
                        content_id,
                        int(chunk_index),
                        category,
                        category,
                        digest,
                        rel_path,
                        len(encrypted_data),
                        int(orig_size),
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
            except (OSError, sqlite3.OperationalError) as exc:
                logger.warning("Chunk disk cache write skipped (fail-open) for %s: %s", cache_key, exc)
                try:
                    conn.rollback()
                except sqlite3.Error as rb_exc:
                    logger.debug("Preview cache rollback skipped for %s: %s", cache_key, rb_exc)
                self._safe_unlink(abs_path)
                return False
            finally:
                conn.close()

    def get(
        self,
        content_id: int,
        expected_hash: Optional[str] = None,
        expected_types=None,
    ) -> Optional[dict]:
        """Fetch, verify, decrypt, and record read stats for a whole-file preview entry."""
        cache_key = f"file:{content_id}"
        with self._lock:
            if self._closed:
                return None
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM preview_cache WHERE cache_key = ?", (cache_key,)
                ).fetchone()
                if not row:
                    return None
                if expected_types is not None and row["content_type"] not in expected_types:
                    return None
                if expected_hash and row["source_hash_digest"] != _hash_digest(expected_hash):
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                abs_path = self._abs_blob_path(row["rel_path"])
                if not os.path.exists(abs_path):
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                try:
                    with open(abs_path, "rb") as f:
                        raw_bytes = f.read()
                    if expected_hash:
                        payload = (
                            crypto_handler.decrypt(raw_bytes, _derive_key(expected_hash))
                            if row["is_encrypted"]
                            else raw_bytes
                        )
                        data = gzip.decompress(payload) if row["is_compressed"] else payload
                        decoded = True
                    else:
                        data = raw_bytes
                        decoded = not bool(row["is_encrypted"]) and not bool(row["is_compressed"])
                except (ValueError, OSError, gzip.BadGzipFile, EOFError) as exc:
                    logger.warning("Corrupt preview cache blob removed for %s: %s", cache_key, exc)
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                now = time.time()
                conn.execute(
                    "UPDATE preview_cache SET last_accessed_at = ?, access_count = access_count + 1 WHERE cache_key = ?",
                    (now, cache_key),
                )
                conn.commit()

                result = dict(row)
                result["last_accessed_at"] = now
                result["access_count"] = int(row["access_count"]) + 1
                result["data"] = data
                result["decoded"] = decoded
                result["compressed_size"] = row["stored_size"]
                return result
            finally:
                conn.close()

    def get_chunk(self, content_id: int, chunk_index: int, expected_hash: str) -> Optional[bytes]:
        """Fetch, decrypt, and record read stats for a cached media chunk."""
        if not expected_hash:
            return None
        cache_key = f"chunk:{content_id}:{chunk_index}"
        with self._lock:
            if self._closed:
                return None
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM preview_cache WHERE cache_key = ?", (cache_key,)
                ).fetchone()
                if not row:
                    return None
                if row["source_hash_digest"] != _hash_digest(expected_hash):
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                abs_path = self._abs_blob_path(row["rel_path"])
                if not os.path.exists(abs_path):
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                try:
                    with open(abs_path, "rb") as f:
                        encrypted_bytes = f.read()
                    plaintext = (
                        crypto_handler.decrypt(encrypted_bytes, _derive_key(expected_hash))
                        if row["is_encrypted"]
                        else encrypted_bytes
                    )
                except (ValueError, OSError) as exc:
                    logger.warning("Corrupt chunk cache blob removed for %s: %s", cache_key, exc)
                    self._delete_row_and_blob(conn, row)
                    conn.commit()
                    return None

                now = time.time()
                conn.execute(
                    "UPDATE preview_cache SET last_accessed_at = ?, access_count = access_count + 1 WHERE cache_key = ?",
                    (now, cache_key),
                )
                conn.commit()
                return plaintext
            finally:
                conn.close()

    def delete(self, content_id: int) -> None:
        """Delete whole-file and chunk cache entries associated with content_id."""
        self.delete_many([content_id])

    def delete_many(self, content_ids: list[int]) -> None:
        """Delete all cache entries and blob files for the given content IDs."""
        if not content_ids:
            return
        with self._lock:
            if self._closed:
                return
            conn = self._get_conn()
            try:
                deleted = 0
                for i in range(0, len(content_ids), 900):
                    batch = content_ids[i:i + 900]
                    placeholders = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"SELECT cache_key, rel_path FROM preview_cache WHERE content_id IN ({placeholders})",
                        batch,
                    ).fetchall()
                    for row in rows:
                        self._safe_unlink(self._abs_blob_path(row["rel_path"]))
                    cur = conn.execute(
                        f"DELETE FROM preview_cache WHERE content_id IN ({placeholders})",
                        batch,
                    )
                    deleted += cur.rowcount or 0
                if deleted > 0:
                    conn.execute("PRAGMA incremental_vacuum(32)")
                conn.commit()
            finally:
                conn.close()

    def get_total_size(self, category: Optional[str] = None) -> int:
        with self._lock:
            if self._closed:
                return 0
            conn = self._get_conn()
            try:
                if category:
                    cur = conn.execute(
                        "SELECT COALESCE(SUM(stored_size), 0) FROM preview_cache WHERE category = ?",
                        (category,),
                    )
                else:
                    cur = conn.execute("SELECT COALESCE(SUM(stored_size), 0) FROM preview_cache")
                return cur.fetchone()[0]
            finally:
                conn.close()

    def get_read_stats(self, cache_key: str) -> Optional[dict]:
        """Return read tracking metadata for a cache key."""
        with self._lock:
            if self._closed:
                return None
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT cache_key, category, stored_size, created_at, last_accessed_at, access_count "
                    "FROM preview_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def evict(self, incoming_size: int) -> None:
        """Evict least-recently-read entries until total + incoming <= global cap."""
        with self._lock:
            if self._closed:
                return
            conn = self._get_conn()
            try:
                total_cap = min(MAX_TOTAL_CACHE_SIZE, MAX_CACHE_SIZE)
                total = conn.execute("SELECT COALESCE(SUM(stored_size), 0) FROM preview_cache").fetchone()[0]
                target = total_cap - incoming_size
                evicted = 0
                while total > target:
                    row = conn.execute(
                        """SELECT cache_key, rel_path, stored_size
                           FROM preview_cache
                           ORDER BY last_accessed_at ASC, access_count ASC, created_at ASC
                           LIMIT 1"""
                    ).fetchone()
                    if not row:
                        break
                    self._delete_row_and_blob(conn, row)
                    total -= row["stored_size"]
                    evicted += 1
                if evicted > 0:
                    conn.execute("PRAGMA incremental_vacuum(32)")
                conn.commit()
            finally:
                conn.close()

    def close(self) -> None:
        """Mark cache manager closed under lock so logout cleanup does not race active writes."""
        with self._lock:
            self._closed = True
