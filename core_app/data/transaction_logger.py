import pickle
import os
import logging
import sqlite3
from typing import Tuple, Any

logger = logging.getLogger(__name__)

class TransactionLogger:
    def __init__(self, log_path: str = './file/pending_ops.bin'):
        self.log_path = log_path
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def append(self, sql: str, params: Tuple[Any, ...]):
        """將 SQL 指令以二進位追加寫入日誌"""
        try:
            with open(self.log_path, 'ab') as f:
                pickle.dump((sql, params), f)
        except Exception as e:
            logger.error(f"Log append failed: {e}")

    def rotate(self):
        """
        將當前日誌內容附加到備份檔，並清空當前日誌。
        這確保了即使上次同步失敗，備份檔也會累積所有未同步的變更，
        且已納入本次 Snapshot 的變更最終會被 clear() 清除。
        """
        if not os.path.exists(self.log_path): return
        
        bak_path = self.log_path + '.bak'
        try:
            # 如果當前 Log 有內容，將其附加到 bak 檔
            if os.path.getsize(self.log_path) > 0:
                with open(self.log_path, 'rb') as src:
                    content = src.read()
                    if content:
                        with open(bak_path, 'ab') as dst:
                            dst.write(content)
                
                # 清空當前 Log (Truncate)
                with open(self.log_path, 'wb') as f:
                    pass # Just open and close to clear it
                    
        except OSError as e:
            logger.error(f"Log rotation failed: {e}")

    def replay(self, conn: sqlite3.Connection):
        """啟動時重放日誌到記憶體 DB"""
        # 先重放備份 (如果存在)
        self._replay_file(conn, self.log_path + '.bak')
        # 再重放當前日誌
        self._replay_file(conn, self.log_path)

    def _replay_file(self, conn: sqlite3.Connection, path: str):
        if not os.path.exists(path): return
        
        logger.info(f"Replaying transactions from {path}...")
        count = 0
        try:
            with open(path, 'rb') as f:
                with conn:
                    while True:
                        try:
                            sql, params = pickle.load(f)
                            conn.execute(sql, params)
                            count += 1
                        except EOFError:
                            break
            logger.info(f"Replayed {count} transactions from {path}.")
        except Exception as e:
            logger.error(f"Replay from {path} failed: {e}")

    def clear(self):
        """雲端同步成功後清除備份日誌"""
        bak_path = self.log_path + '.bak'
        if os.path.exists(bak_path):
            try:
                os.remove(bak_path)
                logger.debug("Backup transaction log cleared.")
            except OSError as e:
                logger.error(f"Failed to clear backup log: {e}")
