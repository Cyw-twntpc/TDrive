import pickle
import os
import logging
import sqlite3
import struct
import zlib
from typing import Tuple, Any

logger = logging.getLogger(__name__)

class TransactionLogger:
    def __init__(self, log_path: str = './file/user_data/pending_ops.bin'):
        self.log_path = log_path
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def append(self, sql: str, params: Tuple[Any, ...]):
        """Writes SQL command securely with length header and CRC32."""
        try:
            data = pickle.dumps((sql, params))
            data_len = len(data)
            crc = zlib.crc32(data) & 0xFFFFFFFF
            
            # Format: <I (length 4 bytes) + data + <I (crc 4 bytes)
            header = struct.pack('<I', data_len)
            footer = struct.pack('<I', crc)
            
            with open(self.log_path, 'ab') as f:
                f.write(header)
                f.write(data)
                f.write(footer)
                f.flush()
                os.fsync(f.fileno()) # Force write to physical disk
        except Exception as e:
            logger.error(f"Log append failed: {e}")
            raise # Ensure caller (DB Handler) knows the write failed

    def rotate(self):
        """Atomic log rotation: append to .bak, then truncate via os.replace."""
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
                            logger.warning(f"Incomplete payload data found in {path} (Expected {data_len}, got {len(data)}). Safe truncation applied.")
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
                            sql, params = pickle.loads(data)
                            conn.execute(sql, params)
                            count += 1
                        except Exception as parse_err:
                            logger.error(f"Failed to parse or execute healthy record: {parse_err}")
                            break

            logger.info(f"Successfully replayed {count} healthy transactions from {path}.")
        except Exception as e:
            logger.error(f"Replay from {path} encountered an unexpected error: {e}")

    def clear(self):
        """Clears backup log after successful cloud sync."""
        bak_path = self.log_path + '.bak'
        if os.path.exists(bak_path):
            try:
                os.remove(bak_path)
                logger.debug("Backup transaction log cleared.")
            except OSError as e:
                logger.error(f"Failed to clear backup log: {e}")
