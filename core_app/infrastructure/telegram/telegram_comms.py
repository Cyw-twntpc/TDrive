from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SetHistoryTTLRequest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
from telethon.errors import FloodWaitError
import asyncio
import logging
import random
import io
import re
import time
from typing import Callable, List, TypeVar, Awaitable, Optional

from core_app.core import crypto_handler as cr
from core_app.infrastructure.local_fs import file_processor as fp

logger = logging.getLogger(__name__)

CALLBACK_ELAPSED = 0.5 # seconds - Throttle UI updates

_rotation_lock = asyncio.Lock()

T = TypeVar('T')

async def _retry_with_backoff(
    func: Callable[[], Awaitable[T]], 
    max_retries: int = 5, 
    base_delay: float = 1.0, 
    max_delay: float = 32.0
) -> T:
    """Executes async function with exponential backoff and FloodWait handling."""
    attempt = 0
    while True:
        try:
            return await func()
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError: Sleeping for {e.seconds} seconds.")
            await asyncio.sleep(e.seconds)
        except (OSError, ValueError, asyncio.TimeoutError) as e: 
            attempt += 1
            if attempt > max_retries:
                logger.error(f"Operation failed after {max_retries} attempts: {e}")
                raise
            
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = random.uniform(0, 0.5 * delay)
            sleep_time = delay + jitter
            
            logger.warning(f"Operation failed (Attempt {attempt}/{max_retries}): {e}. Retrying in {sleep_time:.2f}s...")
            await asyncio.sleep(sleep_time)
        except Exception as e:
             logger.error(f"Non-retriable error: {e}")
             raise

async def _ensure_no_ttl(shared_state, group_id: int):
    """Disables auto-delete (TTL) on the storage group. Uses main user account."""
    client = shared_state.client
    try:
        entity = await client.get_entity(group_id)
        current_ttl = getattr(entity, 'ttl_period', 0)
        
        if current_ttl and current_ttl > 0:
            logger.info(f"Auto-delete is enabled (TTL: {current_ttl}s) for group {group_id}. Disabling it...")
            await client(SetHistoryTTLRequest(peer=entity, period=0))
            logger.info("Auto-delete successfully disabled.")
    except Exception as e:
        logger.warning(f"Failed to check or disable auto-delete (TTL) for group {group_id}: {e}")

async def _ensure_permanently_muted(shared_state, group_id: int):
    """Permanently mutes notifications (mute_until=2147483647) for the given storage group. Uses main user account."""
    client = shared_state.client
    try:
        input_peer = await client.get_input_entity(group_id)
        peer_notify = InputNotifyPeer(peer=input_peer)
        settings = InputPeerNotifySettings(mute_until=2147483647)
        await client(UpdateNotifySettingsRequest(peer=peer_notify, settings=settings))
        logger.info(f"Permanently muted notifications for group {group_id}.")
    except Exception as e:
        logger.warning(f"Failed to permanently mute notifications for group {group_id}: {e}")

async def get_group_message_count(client, group_id: int) -> int:
    """Returns the total number of messages in the given storage group using client.get_messages(group_id, limit=0)."""
    try:
        res = await client.get_messages(group_id, limit=0)
        return getattr(res, 'total', 0) if res is not None else 0
    except Exception as e:
        logger.error(f"Failed to get message count for group {group_id}: {e}")
        return 0

async def get_all_tdrive_groups(shared_state) -> list[tuple[int, int, str]]:
    r"""
    Scans all dialogs using await client.get_dialogs(limit=None).
    Matches dialog names using regex (e.g. r"^TDrive(_\d+|\s+\d+)?$" or r"^TDrive(\s+\d+)?$").
    Extracts group_index (index 0 or 1 for "TDrive", N for "TDrive_N" / "TDrive N").
    Returns sorted list of (group_index, telegram_group_id, group_name).
    Uses main user account.
    """
    client = shared_state.client
    try:
        dialogs = await client.get_dialogs(limit=None)
        pattern = re.compile(r"^TDrive(?:[_\s]+(\d+))?$")
        groups = []
        for dialog in dialogs:
            name = getattr(dialog, 'name', '') or ""
            match = pattern.match(name)
            if match:
                idx_str = match.group(1)
                group_index = int(idx_str) if idx_str is not None else 1
                group_id = getattr(dialog, 'id', None)
                if group_id is not None:
                    groups.append((group_index, group_id, name))
        
        groups.sort(key=lambda x: x[0])
        logger.debug(f"Discovered {len(groups)} TDrive storage groups: {[g[2] for g in groups]}")
        return groups
    except Exception as e:
        logger.error(f"Failed to scan TDrive storage groups: {e}")
        return []

async def rotate_storage_group(shared_state, db_connection=None) -> tuple[int, int]:
    """
    Creates a new storage group (e.g. TDrive_2, TDrive_3 based on max_index + 1),
    calls _ensure_permanently_muted and _ensure_no_ttl on the new group,
    updates storage_groups SQLite table, updates shared_state,
    and returns (new_group_index, new_telegram_group_id).
    Uses main user account.
    """
    client = shared_state.client
    try:
        # Determine existing max index
        existing_groups = await get_all_tdrive_groups(shared_state)
        known_indices = [g[0] for g in existing_groups]
        if hasattr(shared_state, 'group_id_map') and shared_state.group_id_map:
            known_indices.extend(shared_state.group_id_map.keys())
        if hasattr(shared_state, 'active_group_index') and shared_state.active_group_index is not None:
            known_indices.append(shared_state.active_group_index)

        max_index = max(known_indices) if known_indices else 1
        new_index = max_index + 1 if max_index >= 1 else 2
        new_group_name = f"TDrive_{new_index}"

        logger.info(f"Rotating storage group. Creating new group '{new_group_name}' (index {new_index})...")
        result = await client(CreateChannelRequest(
            title=new_group_name,
            about="TDrive Storage Group. Do not delete.",
            megagroup=True
        ))
        chats = getattr(result, 'chats', [])
        channel = chats[0] if chats else None
        raw_id = getattr(channel, 'id', 0) if channel else 0
        try:
            val = int(raw_id)
            raw_str = str(val)
            new_group_id = val if raw_str.startswith("-100") else int(f"-100{abs(val)}")
        except (ValueError, TypeError):
            new_group_id = -1000000000000 + new_index

        await _ensure_permanently_muted(shared_state, new_group_id)
        await _ensure_no_ttl(shared_state, new_group_id)

        # Update Database storage_groups table
        if db_connection is None:
            from core_app.infrastructure.database.main_db.database import DatabaseConnection
            if DatabaseConnection._instance is not None:
                db_connection = DatabaseConnection._instance

        if db_connection is not None:
            try:
                with db_connection._db_lock:
                    conn = db_connection._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE storage_groups SET is_active = 0 WHERE is_active = 1")
                    now_str = time.strftime('%Y-%m-%d %H:%M:%S')
                    cursor.execute(
                        "INSERT OR REPLACE INTO storage_groups (group_index, telegram_group_id, group_name, created_at, msg_count, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                        (new_index, new_group_id, new_group_name, now_str, 0, 1)
                    )
                    conn.commit()
            except Exception as dbe:
                logger.error(f"Failed to update storage_groups DB table during rotation: {dbe}")

        # Update shared_state
        if shared_state is not None:
            shared_state.active_group_index = new_index
            if not hasattr(shared_state, 'group_id_map') or shared_state.group_id_map is None:
                shared_state.group_id_map = {}
            shared_state.group_id_map[new_index] = new_group_id
            shared_state.current_group_msg_count = 0
            shared_state.group_id = new_group_id

        # Invite and promote existing worker bots to the newly created storage group
        bot_usernames = set()
        if db_connection is not None:
            try:
                from core_app.infrastructure.telegram.bot_manager import BotManager
                for bot_info in BotManager(db_connection).get_all_bots():
                    if bot_info.get('username'):
                        bot_usernames.add(bot_info['username'])
            except Exception as b_err:
                logger.warning(f"Failed to query worker bots for group rotation: {b_err}")

        for pool_client in getattr(shared_state, 'clients_pool', []) or []:
            worker_name = getattr(pool_client, 'tdrive_worker_name', None)
            if worker_name:
                bot_usernames.add(worker_name)

        if bot_usernames:
            from core_app.infrastructure.telegram.bot_manager import BotManager
            bot_mgr = BotManager(db_connection) if db_connection is not None else BotManager()
            for username in sorted(bot_usernames):
                try:
                    await bot_mgr.invite_and_promote_bot_to_group(shared_state, username, new_group_id)
                except Exception as inv_err:
                    logger.warning(f"Failed to invite/promote worker bot @{username} to rotated group {new_group_id}: {inv_err}")

        logger.info(f"Successfully rotated storage group to index {new_index} (ID: {new_group_id}).")
        return (new_index, new_group_id)

    except Exception as e:
        logger.error(f"Failed to rotate storage group: {e}", exc_info=True)
        raise

async def check_and_rotate_group_if_needed(shared_state, db_connection=None) -> tuple[int, int] | None:
    """
    Checks if current_group_msg_count >= 995000 (or max_group_messages).
    If so, performs group rotation under _rotation_lock with double-checked count verification.
    Uses main user account.
    """
    threshold = getattr(shared_state, 'max_group_messages', 995000)
    current_count = getattr(shared_state, 'current_group_msg_count', 0)
    if current_count >= threshold:
        async with _rotation_lock:
            current_count = getattr(shared_state, 'current_group_msg_count', 0)
            if current_count >= threshold:
                logger.info(f"Current group message count ({current_count}) >= threshold ({threshold}). Triggering auto group rotation...")
                return await rotate_storage_group(shared_state, db_connection=db_connection)
    logger.debug(f"Group msg count ({current_count}) below threshold ({threshold}), no rotation needed")
    return None

# Note: Group ID saving/loading logic should be moved to AuthService or similar high-level manager.
# telegram_comms should just be given a group_id to work with.
# For compatibility with existing calls, we might keep get_group helper here but purely functional.

async def get_group_id(shared_state, group_name: str = "TDrive") -> int | None:
    """Finds or creates the dedicated storage group on Telegram. Uses main user account."""
    client = shared_state.client
    try:
        dialogs = await client.get_dialogs(limit=None)
        for dialog in dialogs:
            if dialog.is_group and dialog.name == group_name:
                await _ensure_no_ttl(shared_state, dialog.id)
                await _ensure_permanently_muted(shared_state, dialog.id)
                return dialog.id

        logger.info(f"Group '{group_name}' not found, creating a new one...")
        result = await client(CreateChannelRequest(
            title=group_name,
            about="TDrive Storage Group. Do not delete.",
            megagroup=True
        ))
        channel = result.chats[0]
        group_id = int(f"-100{channel.id}")
        await _ensure_no_ttl(shared_state, group_id)
        await _ensure_permanently_muted(shared_state, group_id)
        return group_id
    except Exception as e:
        logger.error(f"Failed to find or create group: {e}", exc_info=True)
        return None

async def upload_single_chunk(client, group_id: int, part_bytes: bytes, progress_callback: Callable | None = None):
    """Uploads a single byte sequence with backoff and retry, disguised as an mp4."""
    async def _upload_chunk():
        f = io.BytesIO(part_bytes)
        f.name = "chunk.txt"
        return await client.send_file(
            group_id,
            file=f,
            force_document=True,
            progress_callback=progress_callback
        )
    return await _retry_with_backoff(_upload_chunk)

async def upload_data_as_file(client, group_id: int, data_bytes: bytes, original_hash: str, 
                              progress_callback: Callable | None = None) -> list:
    """
    Uploads raw bytes as a file (stream split and encrypted).
    Used for thumbnails DB, preview images, and Map Files.
    """
    loop = asyncio.get_running_loop()
    try:
        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        total_size = len(data_bytes)
        
        chunks = []
        for i in range(0, total_size, fp.CHUNK_SIZE):
            chunks.append(data_bytes[i:i + fp.CHUNK_SIZE])
            
        split_files_info = []
        uploaded_accumulated = 0
        
        for idx, chunk in enumerate(chunks):
            part_num = idx + 1
            encrypted_chunk = cr.encrypt(chunk, key)
            part_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_chunk)
            
            async def _upload_chunk():
                f = io.BytesIO(encrypted_chunk)
                f.name = "chunk.txt"
                return await client.send_file(
                    group_id,
                    file=f,
                    force_document=True,
                    progress_callback=lambda c, t: progress_callback(c, total_size) if progress_callback else None
                )

            message = await _retry_with_backoff(_upload_chunk)
            split_files_info.append([part_num, message.id, part_hash])
            
            uploaded_accumulated += len(encrypted_chunk)
            if progress_callback:
                progress_callback(uploaded_accumulated, total_size)

        return split_files_info

    except Exception as e:
        logger.error(f"Data upload failed: {e}", exc_info=True)
        raise

async def download_data_as_bytes(client, group_id: int, msg_ids: List[int], original_hash: str) -> Optional[bytes]:
    """
    Downloads messages and reassembles them into bytes in memory.
    """
    try:
        messages = await client.get_messages(group_id, ids=msg_ids)
        messages = [m for m in messages if m]

        if not messages:
            logger.warning(f"download_data_as_bytes: no messages returned for {msg_ids}")
            return None

        if len(messages) != len(msg_ids):
            logger.error(f"download_data_as_bytes: chunk count mismatch: requested {len(msg_ids)}, got {len(messages)}.")
            return None

        key = cr.generate_key(original_hash[:32], original_hash[-32:])
        final_buffer = io.BytesIO()

        loop = asyncio.get_running_loop()
        for idx, message in enumerate(messages):
            encrypted_bytes = await message.download_media(file=bytes)
            if not encrypted_bytes:
                logger.warning(f"download_data_as_bytes: message {idx} returned empty media")
                continue

            decrypted_chunk = await loop.run_in_executor(None, cr.decrypt, encrypted_bytes, key)
            final_buffer.write(decrypted_chunk)

        result = final_buffer.getvalue()
        return result

    except Exception as e:
        logger.error(f"download_data_as_bytes failed: {e}", exc_info=True)
        return None

async def download_single_chunk(client, message, progress_callback: Callable | None = None) -> bytes:
    """Downloads a single media chunk with backoff and retry."""
    async def _download_chunk():
        return await client.download_media(message, file=bytes, progress_callback=progress_callback)
    return await _retry_with_backoff(_download_chunk)


async def download_encrypted_chunk(client, group_id: int, raw_msg_id: int) -> Optional[bytes]:
    """Download the raw AES-256-GCM ciphertext bytes of a single Telegram chunk without decrypting."""
    try:
        messages = await client.get_messages(group_id, ids=[raw_msg_id])
        messages = [m for m in (messages or []) if m]
        if not messages:
            logger.warning(f"download_encrypted_chunk: no message returned for raw_msg_id={raw_msg_id}")
            return None

        encrypted_bytes = await messages[0].download_media(file=bytes)
        if not encrypted_bytes:
            logger.warning(f"download_encrypted_chunk: empty media for raw_msg_id={raw_msg_id}")
            return None
        return encrypted_bytes
    except Exception as e:
        logger.error(f"download_encrypted_chunk failed for msg_id={raw_msg_id}: {e}", exc_info=True)
        return None
