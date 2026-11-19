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

from core_app.core import utils
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
        self._last_snapshot_group_id = None
        
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

    async def initialize_db(self, client, group_id: int | list[int] | None = None, api_id: int | None = None) -> bool:
        """
        Downloads and restores the latest DB snapshot from the cloud across all storage groups.
        """
        try:
            if api_id is None and hasattr(self, 'shared_state') and self.shared_state:
                api_id = getattr(self.shared_state, 'api_id', None)

            # Collect candidate storage group IDs to search
            group_ids_to_search = []

            # 1. Process group_id parameter if provided
            if group_id is not None:
                if isinstance(group_id, (list, tuple, set)):
                    for item in group_id:
                        if isinstance(item, int):
                            group_ids_to_search.append(item)
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            idx, tg_id = item[0], item[1]
                            group_ids_to_search.append(tg_id)
                            if hasattr(self, 'shared_state') and self.shared_state:
                                if not hasattr(self.shared_state, 'group_id_map') or self.shared_state.group_id_map is None:
                                    self.shared_state.group_id_map = {}
                                self.shared_state.group_id_map[idx] = tg_id
                elif isinstance(group_id, int):
                    group_ids_to_search.append(group_id)

            # 2. Discover all TDrive storage groups via telegram_comms.get_all_tdrive_groups
            try:
                discovered_groups = await telegram_comms.get_all_tdrive_groups(self.shared_state)
                for idx, tg_id, name in discovered_groups:
                    group_ids_to_search.append(tg_id)
                    if hasattr(self, 'shared_state') and self.shared_state:
                        if not hasattr(self.shared_state, 'group_id_map') or self.shared_state.group_id_map is None:
                            self.shared_state.group_id_map = {}
                        self.shared_state.group_id_map[idx] = tg_id
            except Exception as ge:
                logger.warning(f"Could not discover TDrive groups from client: {ge}")

            # 3. Include shared_state.group_id if available
            if hasattr(self, 'shared_state') and self.shared_state and getattr(self.shared_state, 'group_id', None):
                group_ids_to_search.append(self.shared_state.group_id)

            # Deduplicate while preserving order
            unique_group_ids = []
            for gid in group_ids_to_search:
                if gid not in unique_group_ids:
                    unique_group_ids.append(gid)

            logger.info(f"Searching {len(unique_group_ids)} unique storage groups for DB snapshot")

            def _extract_msg_date(msg):
                d = getattr(msg, 'date', None)
                if d is None:
                    return 0.0
                if hasattr(d, 'timestamp'):
                    return d.timestamp()
                try:
                    return float(d)
                except (ValueError, TypeError):
                    return 0.0

            # Iterate across all discovered storage groups to search for #tdrive_db_snapshot messages
            candidate_snapshots = []
            for gid in unique_group_ids:
                snapshot_msg = None
                try:
                    iter_res = client.iter_messages(gid, limit=50)
                    if hasattr(iter_res, '__aiter__'):
                        async for message in iter_res:
                            if message and getattr(message, 'text', None) and '#tdrive_db_snapshot' in str(message.text):
                                snapshot_msg = message
                                break
                    elif isinstance(iter_res, (list, tuple)):
                        for message in iter_res:
                            if message and getattr(message, 'text', None) and '#tdrive_db_snapshot' in str(message.text):
                                snapshot_msg = message
                                break
                except Exception as iter_err:
                    logger.debug(f"iter_messages failed for group {gid}: {iter_err}")

                if not snapshot_msg:
                    try:
                        get_msg_res = client.get_messages(gid, limit=1, search='#tdrive_db_snapshot')
                        messages = await get_msg_res if (asyncio.iscoroutine(get_msg_res) or hasattr(get_msg_res, '__await__')) else get_msg_res
                        if isinstance(messages, (list, tuple)) and len(messages) > 0 and messages[0]:
                            snapshot_msg = messages[0]
                    except Exception as search_err:
                        logger.debug(f"get_messages search failed for group {gid}: {search_err}")

                if snapshot_msg:
                    candidate_snapshots.append((snapshot_msg, gid))

            snapshot_msg = None
            selected_group_id = None
            if candidate_snapshots:
                # Select the snapshot with the globally latest message date
                snapshot_msg, selected_group_id = max(candidate_snapshots, key=lambda pair: _extract_msg_date(pair[0]))
                logger.info(f"Selected snapshot from group {selected_group_id} (latest date)")

            conn = self.db._get_conn()
            
            if snapshot_msg:
                self._last_snapshot_msg_id = snapshot_msg.id
                self._last_snapshot_group_id = selected_group_id
                logger.info(f"Found DB snapshot (ID: {snapshot_msg.id}). Downloading...")
                
                # Download encrypted blob
                dl_res = snapshot_msg.download_media(file=bytes) if hasattr(snapshot_msg, 'download_media') else None
                if dl_res is not None:
                    encrypted_bytes = await dl_res if (asyncio.iscoroutine(dl_res) or hasattr(dl_res, '__await__')) else dl_res
                else:
                    encrypted_bytes = None

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
                        ALLOWED_TABLES = {'folders', 'files', 'file_folder_map', 'trash_metadata', 'worker_bots', 'storage_groups'}
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
                
                # Cleanup old snapshots reliably using in-memory tracker across storage groups
                try:
                    prev_group_id = self._last_snapshot_group_id or group_id
                    if self._last_snapshot_msg_id and (prev_group_id != group_id or self._last_snapshot_msg_id != msg.id):
                        await client.delete_messages(prev_group_id, [self._last_snapshot_msg_id])
                    self._last_snapshot_msg_id = msg.id
                    self._last_snapshot_group_id = group_id
                    
                    # Fallback Search logic for deletion remains useful to clean up old clutter
                    old_msgs = await client.get_messages(group_id, limit=10, search='#tdrive_db_snapshot')
                    ids_to_del = [m.id for m in old_msgs if m.id != msg.id]
                    if ids_to_del:
                        await client.delete_messages(group_id, ids_to_del)
                except Exception:
                    logger.warning("Failed to clean up old database snapshots.", exc_info=True)
                
            except Exception as e:
                logger.error(f"Failed to sync DB to cloud: {e}", exc_info=True)

    # --- Map File Management ---

    async def fetch_map_file(self, client, api_id: int, map_msg_id: int) -> Dict:
        if map_msg_id in self._map_cache:
            self._map_cache.move_to_end(map_msg_id)
            cached = self._map_cache[map_msg_id]
            return cached

        try:
            g_idx, raw_map_msg_id = utils.decode_global_msg_id(map_msg_id)
            logger.debug(f"fetch_map_file: decoded global_id={map_msg_id} -> group={g_idx}, raw={raw_map_msg_id}")
            target_gid = self.shared_state.get_telegram_group_id(g_idx)

            msgs = await client.get_messages(target_gid, ids=[raw_map_msg_id])
            if not msgs or not msgs[0]:
                logger.warning(f"fetch_map_file: no messages returned for map_msg_id={map_msg_id} (raw={raw_map_msg_id}, group={target_gid})")
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
            
            if not isinstance(json_data, dict):
                raise ValueError(f"Invalid map file format for {map_msg_id}: expected dict, got {type(json_data)}")

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
        Compresses, Encrypts (API_ID Key), Uploads Map. Returns bit-packed global_msg_id.
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
            
            # Determine active group index for bit-packing
            active_g_idx = 1
            if hasattr(self, 'shared_state') and self.shared_state:
                active_g_idx = getattr(self.shared_state, 'active_group_index', 1)
            
            global_msg_id = utils.encode_global_msg_id(active_g_idx, msg.id)
            logger.debug(f"save_map_file: encoded group_index={active_g_idx}, raw_msg_id={msg.id} -> global={global_msg_id}")

            # Cache the result immediately
            if len(self._map_cache) >= self._cache_size:
                self._map_cache.popitem(last=False)
            self._map_cache[global_msg_id] = map_data
            
            return global_msg_id
        except Exception as e:
            logger.error(f"Failed to save map file: {e}")
            raise

    async def get_file_chunks(self, client, api_id: int, file_id: int) -> List[List[Any]]:
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

        map_data = await self.fetch_map_file(client, api_id, map_msg_id)

        file_data = map_data.get(str(file_id))
        if isinstance(file_data, dict) and 'c' in file_data:
            chunks = file_data['c']
            logger.debug(f"get_file_chunks: retrieved {len(chunks)} chunks for file_id={file_id}")
            return chunks

        logger.warning(f"get_file_chunks: map_data missing or malformed for file_id={file_id}, returning raw={file_data}")
        return file_data or []

    # --- Transfer Integration Logic ---



    async def process_file_transfer(self, client, api_id: int, 
                                    folder_id: int, file_id: int, chunks: List[List], metadata: Dict = None):
        """
        Saves file chunk mapping and metadata to an independent Map File.
        """
        logger.info(f"process_file_transfer: file_id={file_id}, target_group_index={self.shared_state.active_group_index}")
        target_gid = self.shared_state.active_telegram_group_id
        map_data = {str(file_id): {'c': chunks, 'm': metadata or {}}}
        map_global_msg_id = await self.save_map_file(client, target_gid, api_id, map_data)
        self.shared_state.current_group_msg_count += 1
        await telegram_comms.check_and_rotate_group_if_needed(self.shared_state)
        self.file_repo.update_file_map_msg_id(file_id, map_global_msg_id)

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
                        map_data = await self.fetch_map_file(client, api_id, map_msg_id)
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
                    except Exception as e:
                        logger.error(f"Failed to fetch map {map_msg_id} during deletion: {e}")

                if messages_to_delete:
                    msgs_by_group = defaultdict(set)
                    for global_msg_id in messages_to_delete:
                        g_idx, raw_id = utils.decode_global_msg_id(global_msg_id)
                        target_gid = self.shared_state.get_telegram_group_id(g_idx) if hasattr(self, 'shared_state') and self.shared_state else group_id
                        msgs_by_group[target_gid].add(raw_id)

                    total_deleted = 0
                    for target_gid, raw_ids in msgs_by_group.items():
                        raw_list = list(raw_ids)
                        for i in range(0, len(raw_list), 100):
                            batch = raw_list[i:i+100]
                            await client.delete_messages(target_gid, batch)
                            total_deleted += len(batch)
                    logger.info(f"Deleted {len(maps_to_delete_cloud_ids)} map files and {total_deleted - len(maps_to_delete_cloud_ids)} chunks across target groups.")
            except Exception as e:
                logger.error(f"Failed to delete map files and chunks: {e}")

    async def update_folder_thumbnails(self, client, group_id: int, folder_id: int, new_thumbs_map: Dict[int, bytes], thumb_manager: Any):
        """
        Thread-safe method to update thumbs.db for a specific folder.
        Handles download, merge, re-upload, and cleanup of old versions.
        thumbs_db_msg_id in DB is stored as a global_msg_id (bit-packed group_index + raw_msg_id).
        """
        async with self._folder_locks[folder_id]:
            try:
                target_gid = self.shared_state.active_telegram_group_id if self.shared_state else group_id

                # 1. Check if DB is loaded; if not, try to download
                if not thumb_manager.has_db(folder_id):
                    conn = self.db._get_conn()
                    cur = conn.cursor()
                    cur.execute("SELECT thumbs_db_msg_id, thumbs_db_hash FROM folders WHERE id = ?", (folder_id,))
                    db_info = cur.fetchone()
                    
                    if db_info and db_info['thumbs_db_msg_id']:
                        g_idx, old_raw_id = utils.decode_global_msg_id(db_info['thumbs_db_msg_id'])
                        old_target_gid = self.shared_state.get_telegram_group_id(g_idx) if self.shared_state else target_gid
                        logger.debug(f"Downloading existing thumbs.db for folder {folder_id} before update...")
                        old_db_bytes = await telegram_comms.download_data_as_bytes(
                            client, old_target_gid, [old_raw_id], db_info['thumbs_db_hash']
                        )
                        if old_db_bytes:
                            thumb_manager.load_thumbs_db_from_bytes(folder_id, old_db_bytes)

                logger.debug(f"Updating thumbs.db for folder {folder_id} with {len(new_thumbs_map)} new items.")
                
                db_bytes = thumb_manager.update_thumbs_db(folder_id, new_thumbs_map)
                
                if db_bytes:
                    loop = asyncio.get_running_loop()
                    db_hash = await loop.run_in_executor(None, cr.hash_bytes, db_bytes)
                    
                    upload_info = await telegram_comms.upload_data_as_file(
                        client, target_gid, db_bytes, db_hash
                    )
                    
                    if upload_info:
                        raw_new_msg_id = upload_info[0][1]
                        new_msg_id = utils.encode_global_msg_id(
                            self.shared_state.active_group_index if self.shared_state else 1,
                            raw_new_msg_id
                        )
                        
                        # 4. Get Old Msg ID to delete
                        conn = self.db._get_conn()
                        cur = conn.cursor()
                        cur.execute("SELECT thumbs_db_msg_id FROM folders WHERE id = ?", (folder_id,))
                        row = cur.fetchone()
                        old_msg_id = row['thumbs_db_msg_id'] if row else None
                        
                        # 5. Update Metadata
                        self.folder_repo.update_folder_thumbs_info(folder_id, new_msg_id, db_hash)
                        
                        # 6. Clear thumb_src_folder_id for files in this folder
                        cur.execute('''
                            UPDATE files SET thumb_src_folder_id = NULL 
                            WHERE id IN (SELECT file_id FROM file_folder_map WHERE folder_id = ?)
                        ''', (folder_id,))
                        conn.commit()
                        # 7. Delete Old Cloud Message
                        if old_msg_id and old_msg_id != new_msg_id and old_msg_id != 0:
                            try:
                                g_idx, old_del_raw_id = utils.decode_global_msg_id(old_msg_id)
                                old_del_gid = self.shared_state.get_telegram_group_id(g_idx) if self.shared_state else target_gid
                                await client.delete_messages(old_del_gid, [old_del_raw_id])
                            except Exception as e:
                                logger.warning(f"Failed to delete old thumbs.db message {old_msg_id}: {e}")

            except Exception as e:
                logger.error(f"Error updating thumbs.db for folder {folder_id}: {e}", exc_info=True)