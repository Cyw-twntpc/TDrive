import logging
from typing import Dict, List

from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)


class BotRepository:
    """Handles SQLite persistence for worker_bots and registered storage_groups."""

    def __init__(self, db: DatabaseConnection):
        self.db = db

    def get_all_bots(self) -> List[Dict]:
        """Retrieve all registered worker bots from main_db."""
        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT bot_id, token, username, group_joined FROM worker_bots")
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error retrieving worker bots: {e}")
            return []

    def add_bot(self, bot_id: int, token: str, username: str) -> bool:
        """Insert a newly created worker bot into worker_bots."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(
                        cursor,
                        "INSERT OR IGNORE INTO worker_bots (bot_id, token, username, group_joined) VALUES (?, ?, ?, ?)",
                        (bot_id, token, username, 0),
                    )
            return True
        except Exception as e:
            logger.error(f"Error adding bot to DB: {e}")
            return False

    def mark_bot_joined(self, bot_id: int) -> bool:
        """Mark a worker bot as joined and promoted in TDrive storage groups."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "UPDATE worker_bots SET group_joined = 1 WHERE bot_id = ?", (bot_id,))
            return True
        except Exception as e:
            logger.error(f"Error marking bot as joined: {e}")
            return False

    def remove_bot(self, bot_id: int) -> bool:
        """Delete a worker bot record (for example when externally deleted or revoked)."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "DELETE FROM worker_bots WHERE bot_id = ?", (bot_id,))
            return True
        except Exception as e:
            logger.error(f"Error removing bot from DB: {e}")
            return False

    def update_bot_token(self, bot_id: int, new_token: str) -> bool:
        """Update the persisted API token for an existing worker bot."""
        try:
            with self.db._db_lock:
                conn = self.db._get_conn()
                with conn:
                    cursor = conn.cursor()
                    self.db._execute_write(cursor, "UPDATE worker_bots SET token = ? WHERE bot_id = ?", (new_token, bot_id))
            logger.info(f"[db] Updated token for bot_id={bot_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating bot token: {e}")
            return False

    def get_storage_group_ids(self) -> List[int]:
        """Return all telegram_group_id entries stored in the storage_groups table."""
        group_ids: List[int] = []
        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_group_id FROM storage_groups")
            for row in cursor.fetchall():
                if row['telegram_group_id']:
                    group_ids.append(row['telegram_group_id'])
        except Exception as e:
            logger.debug(f"Could not query storage_groups table for bot promotion: {e}")
        return group_ids
