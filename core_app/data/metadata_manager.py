import json
import gzip
import logging
import asyncio
import io
import os
import time
from typing import Dict, List, OrderedDict, Any, Optional
from collections import OrderedDict as SysOrderedDict

from ..api import crypto_handler as cr
from .db_handler import DatabaseHandler
# Avoid circular import if SharedState is only for typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .shared_state import SharedState

logger = logging.getLogger(__name__)

class MetadataManager:
    """
    Manages the lifecycle of the in-memory database and Cloud Map Files.
    Acts as a high-level facade for DB operations involving cloud data.
    """
    
    def __init__(self, db_handler: DatabaseHandler, shared_state: 'SharedState'):
        self.db = db_handler
        self.shared_state = shared_state
        # LRU Cache for Map Files: { msg_id: { "file_id": [chunks...] } }
        self._map_cache: OrderedDict[int, Dict] = SysOrderedDict()
        self._cache_size = 20
        self._lock = asyncio.Lock()
        
        # Setup SyncManager callback
        # SyncManager is initialized in DatabaseHandler
        if hasattr(self.db, 'sync_manager'):
             self.db.sync_manager.set_callback(self._trigger_background_sync, self.shared_state.loop)

    async def _trigger_background_sync(self):
        """Callback for SyncManager to trigger DB upload."""
        if not self.shared_state.is_logged_in or not self.shared_state.client:
            logger.debug("Skipping background sync: Not logged in.")
            return

        try:
            logger.info("Executing adaptive background sync...")
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
                logger.info("Snapshot not found in recent history. Trying deep search...")
                messages = await client.get_messages(group_id, limit=1, search='#tdrive_db_snapshot')
                if messages:
                    snapshot_msg = messages[0]

            conn = self.db._get_conn()
            
            if snapshot_msg:
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
                        ALLOWED_TABLES = {'folders', 'files', 'file_folder_map', 'map_files', 'trash_metadata'}
                        sql_dump = sql_dump.replace('\r\n', '\n')
                        statements = sql_dump.split(';\n')
                        
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
                        except: pass

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
                # 1. Rotate Log: Mark current pending ops as "about to be synced"
                if hasattr(self.db, 'transaction_logger'):
                    self.db.transaction_logger.rotate()

                conn = self.db._get_conn()
                
                # 2. Dump SQL (Captures state including rotated logs)
                dump_io = io.StringIO()
                for line in conn.iterdump():
                    dump_io.write('%s\n' % line)
                sql_data = dump_io.getvalue().encode('utf-8')
                
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
                
                # Cleanup old snapshots
                try:
                    # Search logic for deletion remains useful to clean up old clutter
                    old_msgs = await client.get_messages(group_id, limit=10, search='#tdrive_db_snapshot')
                    ids_to_del = [m.id for m in old_msgs if m.id != msg.id]
                    if ids_to_del:
                        await client.delete_messages(group_id, ids_to_del)
                except: pass
                
            except Exception as e:
                logger.error(f"Failed to sync DB to cloud: {e}", exc_info=True)

    # --- Map File Management ---

    async def fetch_map_file(self, client, group_id: int, api_id: int, map_msg_id: int) -> Dict:
        if map_msg_id in self._map_cache:
            self._map_cache.move_to_end(map_msg_id)
            return self._map_cache[map_msg_id]

        try:
            msgs = await client.get_messages(group_id, ids=[map_msg_id])
            if not msgs or not msgs[0]: return {}
            
            enc_data = await msgs[0].download_media(file=bytes)
            key = cr.generate_key_from_api_id(str(api_id))
            
            decrypted = cr.decrypt(enc_data, key)
            decompressed = gzip.decompress(decrypted)
            json_data = json.loads(decompressed.decode('utf-8'))
            
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
            json_str = json.dumps(map_data)
            compressed = gzip.compress(json_str.encode('utf-8'))
            key = cr.generate_key_from_api_id(str(api_id))
            encrypted = cr.encrypt(compressed, key)
            
            f = io.BytesIO(encrypted)
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
            SELECT mp.msg_id 
            FROM files f 
            JOIN map_files mp ON f.map_id = mp.id 
            WHERE f.id = ?
        """, (file_id,))
        row = cursor.fetchone()
        
        if not row: return []
        map_msg_id = row['msg_id']
        
        map_data = await self.fetch_map_file(client, group_id, api_id, map_msg_id)
        return map_data.get(str(file_id), [])

    # --- Transfer Integration Logic ---

    async def batch_process_file_transfers(self, client, group_id: int, api_id: int, 
                                           folder_id: int, file_chunks_map: Dict[int, List[List]]):
        """
        Batch processes multiple files for a single folder to minimize IO.
        """
        active_map_id = self.db.get_folder_active_map(folder_id)
        
        current_map_data = {}
        current_cloud_msg_id = None
        current_db_map_id = active_map_id
        
        # Load active map
        if active_map_id:
            map_info = self.db.get_map_file_info(active_map_id)
            if map_info and map_info['msg_id']:
                current_map_data = await self.fetch_map_file(client, group_id, api_id, map_info['msg_id'])
                current_cloud_msg_id = map_info['msg_id']
            else:
                current_db_map_id = None
        
        total_chunks = sum(len(v) for v in current_map_data.values())
        files_in_current_batch = []

        async def _flush():
            nonlocal current_map_data, current_cloud_msg_id, current_db_map_id, files_in_current_batch
            
            if not files_in_current_batch and not current_map_data: return

            new_msg_id = await self.save_map_file(client, group_id, api_id, current_map_data)
            
            saved_db_map_id = current_db_map_id
            if saved_db_map_id:
                self.db.update_map_file_msg_id(saved_db_map_id, new_msg_id)
                if current_cloud_msg_id and current_cloud_msg_id != new_msg_id:
                    try: await client.delete_messages(group_id, [current_cloud_msg_id])
                    except: pass
                    self._map_cache.pop(current_cloud_msg_id, None)
            else:
                saved_db_map_id = self.db.create_map_file_record(new_msg_id, folder_id)
                self.db.set_folder_active_map(folder_id, saved_db_map_id)
            
            # Update DB for files in this batch
            conn = self.db._get_conn()
            for fid in files_in_current_batch:
                # Direct SQL execution as this is an internal batch op, or use helper?
                # These updates need to be synced!
                # Using _execute_write via db methods is best.
                # But here we have direct SQL.
                # Since db_handler encapsulates _execute_write, we should call a method on db.
                # But we don't have a specific method for "update file map_id".
                # We can call cursor.execute but that bypasses log.
                # We should add a method in DB handler or expose _execute_write.
                # I'll update the logic here to use a new helper or raw execute via a public method if possible.
                # Actually, I can just use `conn.execute` here if I don't care about logging this specific technical update?
                # No, if we crash, we lose the link between File and Map. We need to log it.
                
                # I will assume I can't easily change db_handler API right now without another file write.
                # But wait, I just rewrote db_handler.
                # I can modify `DatabaseHandler` to add `update_file_map_link`?
                # Or, I can access `db._execute_write` if I'm naughty (Python allows it).
                # `MetadataManager` is "core" enough to access protected members.
                
                self.db._execute_write(conn.cursor(), 
                                       "UPDATE files SET map_id = ? WHERE id = ?", 
                                       (saved_db_map_id, fid), score=1)
                
                self.db.increment_map_ref(saved_db_map_id)
            
            files_in_current_batch = []
            return saved_db_map_id

        for fid, chunks in file_chunks_map.items():
            if total_chunks + len(chunks) > 1000:
                await _flush()
                # Rotate
                current_map_data = {}
                current_cloud_msg_id = None
                current_db_map_id = None
                total_chunks = 0
            
            current_map_data[str(fid)] = chunks
            files_in_current_batch.append(fid)
            total_chunks += len(chunks)
        
        await _flush()

    async def process_file_transfer(self, client, group_id: int, api_id: int, 
                                    folder_id: int, file_id: int, chunks: List[List]):
        """
        Wrapper for single file processing.
        """
        await self.batch_process_file_transfers(client, group_id, api_id, folder_id, {file_id: chunks})

    async def handle_deletion(self, client, group_id: int, api_id: int, deletion_results: List[Dict]):
        """
        Process deletion results from DB.
        """
        # Group by map_id to minimize downloads
        tasks_by_map = {} # { map_id: { "msg_id": 123, "files_to_remove": [fid1, fid2] } }
        maps_to_delete_cloud_ids = set()
        
        for res in deletion_results:
            if not res.get('orphan'): continue
            
            mid = res['map_id']
            
            # Case 1: The entire map is deleted (ref_count <= 0)
            if res.get('map_msg_id_to_delete'):
                cloud_msg_id = res['map_msg_id_to_delete']
                maps_to_delete_cloud_ids.add(cloud_msg_id)
                self._map_cache.pop(cloud_msg_id, None)
                # No need to process file removals for this map since it's gone
                continue

            # Case 2: Partial removal from map
            if mid not in tasks_by_map:
                info = self.db.get_map_file_info(mid)
                if info and info['msg_id']:
                    tasks_by_map[mid] = {"msg_id": info['msg_id'], "files": []}
            
            if mid in tasks_by_map:
                tasks_by_map[mid]["files"].append(str(res['file_id']))

        # Execute Cloud Deletions for dead maps
        if maps_to_delete_cloud_ids:
            try:
                await client.delete_messages(group_id, list(maps_to_delete_cloud_ids))
                logger.info(f"Deleted {len(maps_to_delete_cloud_ids)} empty map files from cloud.")
            except Exception as e:
                logger.error(f"Failed to delete empty map files: {e}")

        # Process Updates (Partial removals)
        for map_id, task in tasks_by_map.items():
            msg_id = task['msg_id']
            
            # Skip if this map was already marked for full deletion (defensive check)
            if msg_id in maps_to_delete_cloud_ids: continue

            files_to_remove = task['files']
            
            # Fetch
            map_data = await self.fetch_map_file(client, group_id, api_id, msg_id)
            if not map_data: continue
            
            # Modify
            dirty = False
            for fid in files_to_remove:
                if fid in map_data:
                    del map_data[fid]
                    dirty = True
            
            if not dirty: continue
            
            if not map_data:
                # Empty Map -> Delete Cloud Message (Should be handled by DB ref_count logic, but double check)
                await client.delete_messages(group_id, [msg_id])
                self._map_cache.pop(msg_id, None)
                self.db.update_map_file_msg_id(map_id, None) 
            else:
                # Re-upload
                new_msg_id = await self.save_map_file(client, group_id, api_id, map_data)
                self.db.update_map_file_msg_id(map_id, new_msg_id)
                
                await client.delete_messages(group_id, [msg_id])
                self._map_cache.pop(msg_id, None)