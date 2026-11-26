import base64
import contextlib
import json
import os
import logging
import sqlite3
import struct
import threading
import zlib
from typing import Tuple, Any

logger = logging.getLogger(__name__)

_BYTES_MARKER = "__tdrive_bytes__"


def _encode_param(value: Any) -> Any:
    """Convert SQL parameters (including bytes BLOBs) into JSON-serializable structures."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BYTES_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, list):
        return [_encode_param(v) for v in value]
    if isinstance(value, tuple):
        return [_encode_param(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode_param(v) for k, v in value.items()}
    return value


def _decode_param(value: Any) -> Any:
    """Restore JSON-encoded SQL parameters (including marked bytes BLOBs) to Python values."""
    if isinstance(value, dict):
        if len(value) == 1 and _BYTES_MARKER in value and isinstance(value[_BYTES_MARKER], str):
            return base64.b64decode(value[_BYTES_MARKER].encode("ascii"))
        return {k: _decode_param(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_param(v) for v in value]
    return value


def _encode_record(sql: str, params: Tuple[Any, ...]) -> bytes:
    """Serialize a SQL statement and parameter tuple into UTF-8 JSON bytes."""
    encoded_params = [_encode_param(p) for p in (params or ())]
    return json.dumps([sql, encoded_params], separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _decode_record(data: bytes) -> Tuple[str, Tuple[Any, ...]]:
    """Deserialize UTF-8 JSON WAL payload into (sql, params_tuple) after structural validation."""
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], str) or not isinstance(payload[1], list):
        raise ValueError("Invalid WAL record structure")
    sql = payload[0]
    params = tuple(_decode_param(v) for v in payload[1])
    return sql, params

class TransactionLogger:
    def __init__(self, log_path: str = './file/user_data/pending_ops.bin'):
        self.log_path = log_path
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._batch_buffer = bytearray()
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    @contextlib.contextmanager
    def batch(self):
        """Buffer WAL appends in memory and flush once with a single fsync on exit."""
        with self._lock:
            self._batch_depth += 1
        try:
            yield
        finally:
            should_flush = False
            with self._lock:
                if self._batch_depth > 0:
                    self._batch_depth -= 1
                if self._batch_depth == 0 and self._batch_buffer:
                    should_flush = True
            if should_flush:
                self.flush_batch()

    def flush_batch(self):
        """Flush buffered WAL records to disk in a single write and fsync."""
        with self._lock:
            if not self._batch_buffer:
                return
            payload = bytes(self._batch_buffer)
            try:
                with open(self.log_path, 'ab') as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                self._batch_buffer.clear()
            except Exception as e:
                logger.error(f"Batch log flush failed: {e}")
                raise

    def append(self, sql: str, params: Tuple[Any, ...]):
        """Writes SQL command securely with length header and CRC32."""
        try:
            data = _encode_record(sql, params)
            data_len = len(data)
            crc = zlib.crc32(data) & 0xFFFFFFFF

            # Format: <I (length 4 bytes) + data + <I (crc 4 bytes)
            header = struct.pack('<I', data_len)
            footer = struct.pack('<I', crc)
            frame = header + data + footer

            with self._lock:
                if self._batch_depth > 0:
                    self._batch_buffer.extend(frame)
                    return

                with open(self.log_path, 'ab') as f:
                    f.write(frame)
                    f.flush()
                    os.fsync(f.fileno()) # Force write to physical disk
        except Exception as e:
            logger.error(f"Log append failed: {e}")
            raise # Ensure caller (DB Handler) knows the write failed

    def rotate(self):
        """Atomic log rotation: append to .bak, then truncate via os.replace."""
        with self._lock:
            if self._batch_buffer:
                self.flush_batch()
            if not os.path.exists(self.log_path): return
            
            bak_path = self.log_path + '.bak'
            try:
                if os.path.getsize(self.log_path) > 0:
                    with open(self.log_path, 'rb') as src:
                        content = src.read()
                        if content:
                            with open(bak_path, 'ab') as dst:
                                dst.write(content)
                                dst.flush()
                                os.fsync(dst.fileno())
                    
                    tmp_path = self.log_path + '.tmp'
                    with open(tmp_path, 'wb') as f:
                        pass
                    os.replace(tmp_path, self.log_path)
                        
            except OSError as e:
                logger.error(f"Log rotation failed: {e}")

    def replay(self, conn: sqlite3.Connection):
        """Replays the transaction log to the in-memory database."""
        self._replay_file(conn, self.log_path + '.bak')
        self._replay_file(conn, self.log_path)

    def _replay_file(self, conn: sqlite3.Connection, path: str):
        if not os.path.exists(path): return
        
        logger.info(f"Replaying transactions from {path}...")
        count = 0
        try:
            with open(path, 'rb') as f:
                with conn:
                    while True:
                        header = f.read(4)
                        if not header:
                            break # EOF reached safely
                        if len(header) < 4:
                            logger.warning(f"Incomplete length header found in {path}. Safe truncation applied.")
                            break
                        
                        data_len = struct.unpack('<I', header)[0]
                        
                        data = f.read(data_len)
                        if len(data) < data_len:
                            logger.warning(f"Incomplete payload in {path}: expected {data_len}, got {len(data)}.")
                            break
                            
                        footer = f.read(4)
                        if len(footer) < 4:
                            logger.warning(f"Incomplete CRC32 footer found in {path}. Safe truncation applied.")
                            break
                            
                        expected_crc = struct.unpack('<I', footer)[0]
                        actual_crc = zlib.crc32(data) & 0xFFFFFFFF
                        
                        if expected_crc != actual_crc:
                            logger.error(f"CRC32 mismatch detected in {path}! Dropping corrupted record and stopping replay.")
                            break
                        
                        try:
                            sql, params = _decode_record(data)
                            conn.execute(sql, params)
                            count += 1
                        except sqlite3.IntegrityError as integrity_err:
                            # Skip already-present/conflicting rows cleanly so later un-synced mutations still replay.
                            logger.warning(f"Skipping conflicting/already-applied WAL record in {path}: {integrity_err}")
                            continue
                        except Exception as parse_err:
                            logger.error(f"Failed to parse or execute healthy record: {parse_err}")
                            break

            logger.info(f"Successfully replayed {count} healthy transactions from {path}.")
        except Exception as e:
            logger.error(f"Replay from {path} encountered an unexpected error: {e}")

    def has_pending_ops(self) -> bool:
        """Return True if buffered or persisted un-synced WAL operations exist on disk."""
        with self._lock:
            if self._batch_buffer:
                return True
            for candidate in (self.log_path, self.log_path + '.bak'):
                try:
                    if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                        return True
                except OSError:
                    return True
            return False

    def reset_logs(self) -> None:
        """Clear in-memory batch buffer and delete both active and backup WAL files (e.g. on logout)."""
        with self._lock:
            self._batch_buffer.clear()
            self._batch_depth = 0
            for candidate in (self.log_path, self.log_path + '.bak', self.log_path + '.tmp'):
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError as e:
                        logger.warning(f"Failed to remove WAL file {candidate}: {e}")

    def clear(self):
        """Clears backup log after successful cloud sync."""
        bak_path = self.log_path + '.bak'
        if os.path.exists(bak_path):
            try:
                os.remove(bak_path)
                logger.debug("Backup transaction log cleared.")
            except OSError as e:
                logger.error(f"Failed to clear backup log: {e}")
