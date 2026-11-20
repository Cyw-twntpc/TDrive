import logging
import asyncio
import os
import sys
from pathlib import Path
import py7zr
from typing import TYPE_CHECKING, Optional, Union
from telethon import TelegramClient

# Use a forward reference for type hinting to avoid circular imports.
if TYPE_CHECKING:
    from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)


def encode_global_msg_id(group_index: int, raw_msg_id: int) -> int:
    """
    Encode a storage group index and raw message ID into a single global message ID.

    The group_index occupies high 31 bits and raw_msg_id occupies low 32 bits:
    global_id = (group_index << 32) | (raw_msg_id & 0xFFFFFFFF).
    Zero backward compatibility: group_index must be a positive integer >= 1.

    :param group_index: Storage group index (positive integer >= 1).
    :param raw_msg_id: Telegram raw message ID (non-negative integer).
    :return: Bit-packed 64-bit integer global message ID.
    :raises ValueError: If group_index <= 0 or raw_msg_id < 0.
    :raises TypeError: If group_index or raw_msg_id is not an integer.
    """
    if not isinstance(group_index, int) or isinstance(group_index, bool):
        logger.error(f"encode_global_msg_id: group_index must be an integer, got {type(group_index).__name__}")
        raise TypeError("group_index must be an integer")
    if not isinstance(raw_msg_id, int) or isinstance(raw_msg_id, bool):
        logger.error(f"encode_global_msg_id: raw_msg_id must be an integer, got {type(raw_msg_id).__name__}")
        raise TypeError("raw_msg_id must be an integer")
    if group_index <= 0:
        logger.error(f"encode_global_msg_id: group_index must be >= 1, got {group_index}")
        raise ValueError("group_index must be a positive integer >= 1")
    if raw_msg_id < 0:
        logger.error(f"encode_global_msg_id: raw_msg_id must be >= 0, got {raw_msg_id}")
        raise ValueError("raw_msg_id must be a non-negative integer")
    if group_index > 0x7FFFFFFF:
        logger.error(f"encode_global_msg_id: group_index exceeds 31-bit limit, got {group_index}")
        raise ValueError("group_index exceeds 31-bit limit")
    if raw_msg_id > 0xFFFFFFFF:
        logger.error(f"encode_global_msg_id: raw_msg_id exceeds 32-bit limit, got {raw_msg_id}")
        raise ValueError("raw_msg_id exceeds 32-bit limit")

    result = (group_index << 32) | (raw_msg_id & 0xFFFFFFFF)
    logger.debug(f"Encoded group_index={group_index}, raw_msg_id={raw_msg_id} -> global_id={result}")
    return result


def decode_global_msg_id(global_id: int) -> tuple[int, int]:
    """
    Decode a bit-packed global message ID back into (group_index, raw_msg_id).
    Zero backward compatibility: decoded group_index must be >= 1.

    :param global_id: Bit-packed global message ID (positive integer).
    :return: Tuple of (group_index, raw_msg_id).
    :raises ValueError: If global_id <= 0 or decoded group_index <= 0.
    :raises TypeError: If global_id is not an integer.
    """
    if not isinstance(global_id, int) or isinstance(global_id, bool):
        logger.error(f"decode_global_msg_id: global_id must be an integer, got {type(global_id).__name__}")
        raise TypeError("global_id must be an integer")
    if global_id <= 0:
        logger.error(f"decode_global_msg_id: global_id must be > 0, got {global_id}")
        raise ValueError("global_id must be a positive integer > 0")

    group_index = global_id >> 32
    raw_msg_id = global_id & 0xFFFFFFFF
    if group_index <= 0:
        logger.error(f"decode_global_msg_id: invalid global_id {global_id}: decoded group_index ({group_index}) must be >= 1")
        raise ValueError(f"Invalid global_id {global_id}: decoded group_index ({group_index}) must be >= 1")
    logger.debug(f"Decoded global_id={global_id} -> group_index={group_index}, raw_msg_id={raw_msg_id}")
    return group_index, raw_msg_id



async def ensure_client_connected(shared_state: 'SharedState') -> Optional[TelegramClient]:
    if shared_state.client and shared_state.client.is_connected():
        return shared_state.client

    logger.warning("Connection lost. Locking UI and attempting to reconnect to Telegram...")
    
    if shared_state.connection_emitter:
        shared_state.connection_emitter('lost')

    api_id = shared_state.api_id
    api_hash = shared_state.api_hash

    if not (api_id and api_hash):
        logger.error("Cannot reconnect: API credentials not found in SharedState.")
        if shared_state.connection_emitter:
            shared_state.connection_emitter('restored') 
        return None

    # Lazy import to avoid circular dependency with auth modules
    from core_app.application.auth.session_manager import SessionManager

    retry_delay = 5
    max_retry_delay = 30

    try:
        while True:
            try:
                if shared_state.client:
                    try:
                        await shared_state.client.disconnect()
                    except Exception as e:
                        logger.warning(f"Error disconnecting old client: {e}") 

                # Load encrypted StringSession instead of plain SQLite session file
                session = SessionManager.load_session(api_id)
                new_client = TelegramClient(session, api_id, api_hash)
                await new_client.connect()
                
                if await new_client.is_user_authorized():
                    logger.info("Successfully reconnected to Telegram.")
                    shared_state.client = new_client
                    
                    if shared_state.connection_emitter:
                        shared_state.connection_emitter('restored')
                    return new_client
                else:
                    logger.error("Reconnection failed: user authorization is invalid. Re-login is required.")
                    break

            except Exception as e:
                logger.error(f"Telegram reconnection attempt failed: {e}. Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
    except asyncio.CancelledError:
        logger.info("Telegram reconnection cancelled.")
        if shared_state.connection_emitter:
            shared_state.connection_emitter('restored')
        raise
    
    if shared_state.connection_emitter:
        shared_state.connection_emitter('restored')
    return None


def check_path_exists(path: str) -> bool:
    return os.path.exists(path)


def get_resource_path(relative_path: Union[str, Path]) -> Path:
    """Resolve an application resource path across source runs and PyInstaller bundles."""
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parents[2]
    return base_path / relative_path


VENDOR_DIR = str(get_resource_path(Path("core_app") / "vendor"))
_extracted_cache = {}

def ensure_extracted(name: str) -> str:
    """Extract name.7z -> name/ on first call. Returns path to extracted dir."""
    if name in _extracted_cache:
        return _extracted_cache[name]
    archive = os.path.join(VENDOR_DIR, f'{name}.7z')
    target = os.path.join(VENDOR_DIR, name)
    if not os.path.isdir(target) and os.path.isfile(archive):
        with py7zr.SevenZipFile(archive, mode='r') as z:
            z.extractall(path=target)
    _extracted_cache[name] = target
    return target

