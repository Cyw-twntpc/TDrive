import json
import gzip
import zlib
import base64
import logging
import asyncio
import io
import os
import time
import sqlite3
from typing import Dict, List, OrderedDict, Any
from collections import OrderedDict as SysOrderedDict, defaultdict

from core_app.core import crypto_handler as cr
from core_app.infrastructure.telegram import telegram_comms
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.database.main_db.repositories.folder_repository import FolderRepository
from core_app.infrastructure.database.main_db.repositories.trash_repository import TrashRepository
# Avoid circular import if SharedState is only for typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)

class MetadataManager:
    """
    Manages the lifecycle of the in-memory database and Cloud Map Files.
    Acts as a high-level facade for DB operations involving cloud data.
    """
    
    def __init__(self, db_handler: DatabaseConnection, shared_state: 'SharedState'):
        self.db = db_handler
        self.shared_state = shared_state
        self.folder_repo = FolderRepository(self.db)
        self.file_repo = FileRepository(self.db)
        self.trash_repo = TrashRepository(self.db)
        # LRU Cache for Map Files: { msg_id: { "file_id": [chunks...] } }
        self._map_cache: OrderedDict[int, Dict] = SysOrderedDict()
        self._cache_size = 20
        self._lock = asyncio.Lock()
        
        # Folder-level locks for map file updates
        self._folder_locks = defaultdict(asyncio.Lock)
        self._last_snapshot_msg_id = None
        
        # Setup SyncManager callback
        # SyncManager is initialized in DatabaseConnection
        if hasattr(self.db, 'sync_manager'):
             self.db.sync_manager.set_callback(self._trigger_background_sync, self.shared_state.loop)

    async def _trigger_background_sync(self):
        """Callback for SyncManager to trigger DB upload."""
        if not self.shared_state.is_logged_in or not self.shared_state.client:
            logger.debug("Skipping background sync: Not logged in.")
            return

        try:
            logger.debug("Executing adaptive background sync...")
            await self.sync_db_to_cloud(
                self.shared_state.client,
                self.shared_state.group_id,
                self.shared_state.api_id
            )
        except Exception as e:
            logger.error(f"Background sync failed: {e}")

    # --- Database Snapshot Management ---

    async def initialize_db(self, client, group_id: int, api_id: int) -> bool:
        """
        Downloads and restores the latest DB snapshot from the cloud.
        """
        try:
            # Find latest snapshot message by iterating history (more reliable than search)
            snapshot_msg = None
            async for message in client.iter_messages(group_id, limit=50):
                if message.text and '#tdrive_db_snapshot' in message.text:
                    snapshot_msg = message
                    break
            
            if not snapshot_msg:
                # Fallback: Try search if not found in recent history (e.g. buried deep)
                logger.debug("Snapshot not found in recent history. Trying deep search...")
                messages = await client.get_messages(group_id, limit=1, search='#tdrive_db_snapshot')
                if messages:
                    snapshot_msg = messages[0]

            conn = self.db._get_conn()
            
            if snapshot_msg:
                self._last_snapshot_msg_id = snapshot_msg.id
                logger.info(f"Found DB snapshot (ID: {snapshot_msg.id}). Downloading...")
                
                # Download encrypted blob
                encrypted_bytes = await snapshot_msg.download_media(file=bytes)
                if not encrypted_bytes:
                    logger.error("Snapshot download failed (empty).")
                    return False

                # Decrypt: Use simple key from api_id
                key = cr.generate_key_from_api_id(str(api_id))
                try:
                    decrypted_gzip = cr.decrypt(encrypted_bytes, key)
                    sql_dump = gzip.decompress(decrypted_gzip).decode('utf-8')
                    
                    # Restore to Memory DB
                    try:
                        # 1. Disable Foreign Keys for restoration
                        conn.execute("PRAGMA foreign_keys = OFF")
                        
                        # 2. Clear existing schema completely
                        cursor = conn.cursor()
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
                        for trigger in cursor.fetchall(): cursor.execute(f"DROP TRIGGER IF EXISTS {trigger[0]}")
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='view'")
                        for view in cursor.fetchall(): cursor.execute(f"DROP VIEW IF EXISTS {view[0]}")
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                        for table in cursor.fetchall(): cursor.execute(f"DROP TABLE IF EXISTS {table[0]}")
                        
                        # 3. Create Tables ONLY (No Triggers or seed data yet)
                        self.db._create_tables()
                        
                        # 4. Smart Import: Execute INSERTs only for allowed core tables
                        ALLOWED_TABLES = {'folders', 'files', 'file_folder_map', 'trash_metadata', 'worker_bots'}
                        sql_dump = sql_dump.replace('\r\n', '\n')
                        
                        statements = []
                        current_stmt = []
                        for line in sql_dump.split('\n'):
                            if not line.strip() and not current_stmt:
                                continue
                            current_stmt.append(line)
                            stmt_str = '\n'.join(current_stmt)
                            if sqlite3.complete_statement(stmt_str):
                                statements.append(stmt_str)
                                current_stmt = []
                        
                        for statement in statements:
                            stmt = statement.strip()
                            if stmt.upper().startswith("INSERT INTO"):
                                try:
                                    parts = stmt.split()
                                    if len(parts) > 2:
                                        raw_name = parts[2]
                                        table_name = raw_name.split('(')[0].strip('"').strip("'").lower()
                                        
                                        if table_name in ALLOWED_TABLES:
                                            conn.execute(stmt)
                                except Exception as e:
                                    logger.warning(f"Skipping problematic INSERT: {e}")

                        # 5. Create Triggers
                        self.db._create_triggers()
                        
                        # 6. Rebuild Search Index (since we skipped triggers during insert)
                        self.db.rebuild_search_index()

                        # 7. Re-enable Foreign Keys
                        conn.execute("PRAGMA foreign_keys = ON")
                        
                        logger.info("Database successfully restored from snapshot.")
                        
                    except Exception as restore_err:
                        logger.error(f"SQL execution failed during restoration: {restore_err}", exc_info=True)
                        # Fallback: Initialize fresh DB structure
                        self.db._init_db()
                        conn.execute("PRAGMA foreign_keys = ON")
                except Exception as e:
                    logger.error(f"Failed to decrypt/restore snapshot: {e}")
                    # If snapshot decryption fails, start fresh to stay functional
                    self.db._init_db()
            else:
                logger.info("No remote DB snapshot found. Starting with fresh in-memory DB.")

            # --- CRITICAL: Replay Transaction Log ---
            # Replays any pending operations that were not synced before crash/shutdown
            if hasattr(self.db, 'transaction_logger'):
                try:
                    self.db.transaction_logger.replay(conn)
                except Exception as log_err:
                    logger.error(f"Transaction log replay failed: {log_err}")
                    # If log is malformed, clear it to prevent permanent crash loop
                    if "malformed" in str(log_err) or "corrupt" in str(log_err):
                        logger.warning("Deleting corrupt transaction log files.")
                        self.db.transaction_logger.clear() # Clears .bak
                        # Also clear primary log if needed (TransactionLogger doesn't have delete_primary, but clear handles bak)
                        # We might need to manually remove the main file if replay failed on it.
                        try:
                            if os.path.exists(self.db.transaction_logger.log_path):
                                os.remove(self.db.transaction_logger.log_path)
                        except Exception:
                            logger.warning("Failed to remove corrupt transaction log file.", exc_info=True)

            return True

        except Exception as e:
            logger.error(f"Error initializing DB: {e}", exc_info=True)
            return False

    async def sync_db_to_cloud(self, client, group_id: int, api_id: int):
        """
        Dumps in-memory DB, compresses, encrypts, and uploads.
        """
        async with self._lock:
            try:
                # 0. Pre-Sync Integrity Check
                if hasattr(self.db, 'run_integrity_check') and not self.db.run_integrity_check():
                    logger.error("DB integrity check failed. Aborting cloud sync.")
                    return

                def _dump_and_rotate():
                    with self.db._db_lock:
                        if hasattr(self.db, 'transaction_logger'):
                            self.db.transaction_logger.rotate()

                        conn = self.db._get_conn()
                        
                        # 2. Dump SQL (Captures state including rotated logs)
                        dump_io = io.StringIO()
                        for line in conn.iterdump():
                            dump_io.write('%s\n' % line)
                        return dump_io.getvalue().encode('utf-8')
                
                loop = asyncio.get_running_loop()
                sql_data = await loop.run_in_executor(None, _dump_and_rotate)
                
                # 3. Compress & Encrypt
                compressed_data = gzip.compress(sql_data)
                key = cr.generate_key_from_api_id(str(api_id))
                encrypted_data = cr.encrypt(compressed_data, key)
                
                # 4. Upload
                caption = "#tdrive_db_snapshot"
                
                timestamp = int(time.time())
                file_obj = io.BytesIO(encrypted_data)
                file_obj.name = f"tdrive_snapshot_{timestamp}.bin"
                
                msg = await client.send_file(group_id, file=file_obj, caption=caption)
                logger.info(f"Database snapshot uploaded (Msg ID: {msg.id}).")
                
                # 5. Clear Log: Remove the backed-up log since it's now in cloud
                if hasattr(self.db, 'transaction_logger'):
                    self.db.transaction_logger.clear()
                
                # Cleanup old snapshots reliably using in-memory tracker
                try:
                    if self._last_snapshot_msg_id and self._last_snapshot_msg_id != msg.id:
                        await client.delete_messages(group_id, [self._last_snapshot_msg_id])
                    self._last_snapshot_msg_id = msg.id
                    
                    # Fallback Search logic for deletion remains useful to clean up old clutter
                    old_msgs = await client.get_messages(group_id, limit=10, search='#tdrive_db_snapshot')
                    ids_to_del = [m.id for m in old_msgs if m.id != msg.id and m.id != self._last_snapshot_msg_id]
                    if ids_to_del:
                        await client.delete_messages(group_id, ids_to_del)
                except Exception:
                    logger.warning("Failed to clean up old database snapshots.", exc_info=True)
                
            except Exception as e:
                logger.error(f"Failed to sync DB to cloud: {e}", exc_info=True)

    # --- Map File Management ---

    async def fetch_map_file(self, client, group_id: int, api_id: int, map_msg_id: int) -> Dict:
        if map_msg_id in self._map_cache:
            self._map_cache.move_to_end(map_msg_id)
            cached = self._map_cache[map_msg_id]
            return cached

        try:
            msgs = await client.get_messages(group_id, ids=[map_msg_id])
            if not msgs or not msgs[0]:
                logger.warning(f"fetch_map_file: no messages returned for map_msg_id={map_msg_id}")
                return {}
            
            msg = msgs[0]
            key = cr.generate_key_from_api_id(str(api_id))
            
            if msg.text and msg.text.startswith("TDM:"):
                encoded_data = msg.text[4:]
                compressed_data = base64.b64decode(encoded_data)
                decrypted = cr.decrypt(compressed_data, key)
                json_data = json.loads(zlib.decompress(decrypted).decode('utf-8'))
            else:
                enc_data = await msg.download_media(file=bytes)
                decrypted = cr.decrypt(enc_data, key)
                decompressed = gzip.decompress(decrypted)
                json_data = json.loads(decompressed.decode('utf-8'))
            
            # Normalize structure for backward compatibility
            for k, v in json_data.items():
                if isinstance(v, list):
                    json_data[k] = {'c': v, 'm': {}}
            
            # Cache it
            if len(self._map_cache) >= self._cache_size:
                self._map_cache.popitem(last=False)
            self._map_cache[map_msg_id] = json_data

            return json_data
        except Exception as e:
            logger.error(f"Failed to fetch map file {map_msg_id}: {e}")
            return {}

    async def save_map_file(self, client, group_id: int, api_id: int, map_data: Dict) -> int:
        """
        Compresses, Encrypts (API_ID Key), Uploads Map. Returns msg_id.
        """
        try:
            json_str = json.dumps(map_data, separators=(',', ':'))
            compressed = zlib.compress(json_str.encode('utf-8'))
            key = cr.generate_key_from_api_id(str(api_id))
            encrypted = cr.encrypt(compressed, key)
            
            b64_encoded = base64.b64encode(encrypted).decode('utf-8')
            text_payload = f"TDM:{b64_encoded}"
            
            if len(text_payload) < 4000:
                msg = await client.send_message(group_id, text_payload)
            else:
                legacy_compressed = gzip.compress(json_str.encode('utf-8'))
                legacy_encrypted = cr.encrypt(legacy_compressed, key)
                f = io.BytesIO(legacy_encrypted)
                f.name = "map.bin"
                msg = await client.send_file(group_id, file=f, force_document=True)
            
            # Cache the result immediately
            if len(self._map_cache) >= self._cache_size:
                self._map_cache.popitem(last=False)
            self._map_cache[msg.id] = map_data
            
            return msg.id
        except Exception as e:
            logger.error(f"Failed to save map file: {e}")
            raise

    async def get_file_chunks(self, client, group_id: int, api_id: int, file_id: int) -> List[List[Any]]:
        """
        Retrieves chunk list for a specific file from its Map.
        """
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT map_msg_id 
            FROM files 
            WHERE id = ?
        """, (file_id,))
        row = cursor.fetchone()

        if not row or not row['map_msg_id']:
            logger.warning(f"get_file_chunks: map_msg_id is null/empty for file {file_id}; file may have been uploaded incompletely")
            return []

        map_msg_id = row['map_msg_id']

        map_data = await self.fetch_map_file(client, group_id, api_id, map_msg_id)

        file_data = map_data.get(str(file_id))
        if isinstance(file_data, dict) and 'c' in file_data:
            chunks = file_data['c']
            return chunks

        logger.warning(f"get_file_chunks: map_data missing or malformed for file_id={file_id}, returning raw={file_data}")
        return file_data or []

    # --- Transfer Integration Logic ---



    async def process_file_transfer(self, client, group_id: int, api_id: int, 
                                    folder_id: int, file_id: int, chunks: List[List], metadata: Dict = None):
        """
        Saves file chunk mapping and metadata to an independent Map File.
        """
        map_data = {str(file_id): {'c': chunks, 'm': metadata or {}}}
        new_msg_id = await self.save_map_file(client, group_id, api_id, map_data)
        self.file_repo.update_file_map_msg_id(file_id, new_msg_id)

    async def handle_deletion(self, client, group_id: int, api_id: int, deletion_results: List[Dict]):
        """
        Process deletion results from DB. Independent Maps mean we must fetch the map, 
        extract chunk and preview IDs, and delete them along with the map itself.
        """
        messages_to_delete = set()
        maps_to_delete_cloud_ids = set()
        
        for res in deletion_results:
            if not res.get('orphan'): continue
            
            cloud_msg_id = res.get('map_msg_id_to_delete')
            if cloud_msg_id:
                maps_to_delete_cloud_ids.add(cloud_msg_id)
                messages_to_delete.add(cloud_msg_id)
                self._map_cache.pop(cloud_msg_id, None)

        if maps_to_delete_cloud_ids:
            try:
                for map_msg_id in maps_to_delete_cloud_ids:
                    try:
                        map_data = await self.fetch_map_file(client, group_id, api_id, map_msg_id)
                        if not map_data:
                            messages_to_delete.discard(map_msg_id)
                            continue
                        for file_key, file_info in map_data.items():
                            if isinstance(file_info, dict):
                                if 'c' in file_info:
                                    # Handle chunk lists (list of lists)
                                    for chunk_batch in file_info['c']:
                                        if isinstance(chunk_batch, list):
                                            if len(chunk_batch) >= 2:
                                                messages_to_delete.add(chunk_batch[1])
                                            elif len(chunk_batch) == 1:
                                                messages_to_delete.add(chunk_batch[0])
                                        else:
                                            messages_to_delete.add(chunk_batch)
                                            
                                # Check for preview inside 'm' just in case
                                if 'm' in file_info and isinstance(file_info['m'], dict):
                                    m_data = file_info['m']
                                    if 'p' in m_data and m_data['p']:
                                        messages_to_delete.add(m_data['p'])
                                    # Legacy fallback: some old maps had c inside m
                                    if 'c' in m_data:
                                        for chunk_batch in m_data['c']:
                                            if isinstance(chunk_batch, list):
                                                if len(chunk_batch) >= 2:
                                                    messages_to_delete.add(chunk_batch[1])
                                                elif len(chunk_batch) == 1:
                                                    messages_to_delete.add(chunk_batch[0])
                                            else:
                                                messages_to_delete.add(chunk_batch)
                    except Exception as e:
                        logger.error(f"Failed to fetch map {map_msg_id} during deletion: {e}")

                if messages_to_delete:
                    msgs_list = list(messages_to_delete)
                    for i in range(0, len(msgs_list), 100):
                        await client.delete_messages(group_id, msgs_list[i:i+100])
                    logger.info(f"Deleted {len(maps_to_delete_cloud_ids)} map files and {len(msgs_list) - len(maps_to_delete_cloud_ids)} chunks from cloud.")
            except Exception as e:
                logger.error(f"Failed to delete map files and chunks: {e}")

    async def update_folder_thumbnails(self, client, group_id: int, folder_id: int, new_thumbs_map: Dict[int, bytes], thumb_manager: Any):
        """
        Thread-safe method to update thumbs.db for a specific folder.
        Handles download, merge, re-upload, and cleanup of old versions.
        """
        async with self._folder_locks[folder_id]:
            try:
                # 1. Check if DB is loaded; if not, try to download
                if not thumb_manager.has_db(folder_id):
                    conn = self.db._get_conn()
                    cur = conn.cursor()
                    cur.execute("SELECT thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id = ?", (folder_id,))
                    db_info = cur.fetchone()
                    
                    if db_info and db_info['thumbs_db_msg_id']:
                        logger.debug(f"Downloading existing thumbs.db for folder {folder_id} before update...")
                        old_db_bytes = await telegram_comms.download_data_as_bytes(
                            client, group_id, [db_info['thumbs_db_msg_id']], db_info['thumbs_db_hash']
                        )
                        if old_db_bytes:
                            thumb_manager.load_thumbs_db_from_bytes(folder_id, old_db_bytes)

                logger.debug(f"Updating thumbs.db for folder {folder_id} with {len(new_thumbs_map)} new items.")
                
                db_bytes = thumb_manager.update_thumbs_db(folder_id, new_thumbs_map)
                
                if db_bytes:
                    # 3. Hash and Upload
                    loop = asyncio.get_running_loop()
                    db_hash = await loop.run_in_executor(None, cr.hash_bytes, db_bytes)
                    
                    # We don't have easy access to controller here for hidden progress, 
                    # but thumbs.db is usually small enough. If needed, we can pass a callback.
                    upload_info = await telegram_comms.upload_data_as_file(
                        client, group_id, db_bytes, db_hash
                    )
                    
                    if upload_info:
                        new_msg_id = upload_info[0][1]
                        
                        # 4. Get Old Msg ID to delete
                        conn = self.db._get_conn()
                        cur = conn.cursor()
                        cur.execute("SELECT thumbs_db_msg_id FROM folders WHERE id = ?", (folder_id,))
                        row = cur.fetchone()
                        old_msg_id = row['thumbs_db_msg_id'] if row else None
                        
                        # 5. Update Metadata
                        self.folder_repo.update_folder_thumbs_info(folder_id, new_msg_id, db_hash)
                        
                        # 6. Clear thumb_src_folder_id ONLY for files currently in this folder.
                        # Since this folder is uploading a new package, all files in it will naturally
                        # be included. Thus, they no longer need to rely on their old source folders.
                        cur.execute('''
                            UPDATE files SET thumb_src_folder_id = NULL 
                            WHERE id IN (SELECT file_id FROM file_folder_map WHERE folder_id = ?)
                        ''', (folder_id,))
                        conn.commit()
                        # 7. Delete Old Cloud Message
                        if old_msg_id and old_msg_id != new_msg_id and old_msg_id != 0:
                            try:
                                await client.delete_messages(group_id, [old_msg_id])
                            except Exception as e:
                                logger.warning(f"Failed to delete old thumbs.db message {old_msg_id}: {e}")

            except Exception as e:
                logger.error(f"Error updating thumbs.db for folder {folder_id}: {e}", exc_info=True)