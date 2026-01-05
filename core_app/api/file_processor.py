import logging
import os
from typing import Generator, Any, Set, Optional
from . import crypto_handler as cr

logger = logging.getLogger(__name__)

# The size of each file chunk in bytes.
CHUNK_SIZE = int(1024 * 1024 * 8)

def stream_split_and_encrypt(file_path: str, key: bytes, completed_parts: Optional[Set[int]] = None) -> Generator[tuple[int, bytes], Any, None]:
    """Reads file stream, encrypts chunks, yields (part_num, bytes). Skips completed parts."""
    if completed_parts is None:
        completed_parts = set()

    logger.debug(f"Starting stream split for '{file_path}'. Skipping parts: {len(completed_parts)}")
    
    with open(file_path, 'rb') as f_in:
        i = 1
        while True:
            if i in completed_parts:
                f_in.seek(CHUNK_SIZE, 1)
                i += 1
                continue

            chunk = f_in.read(CHUNK_SIZE)
            if not chunk:
                break

            yield i, cr.encrypt(chunk, key)
            i += 1

    logger.debug(f"Finished stream splitting for '{file_path}'.")

def decrypt_bytes_and_write(encrypted_bytes: bytes, output_path: str, key: bytes, offset: int):
    """Decrypts bytes and writes to offset in output_file (Thread-safe)."""
    try:
        decrypted_content = cr.decrypt(encrypted_bytes, key)
        
        with open(output_path, 'r+b') as f_out:
            f_out.seek(offset)
            f_out.write(decrypted_content)
    except IOError as e:
        raise IOError(f"An error occurred while writing to output file '{output_path}': {e}") from e

def get_unique_filepath(directory: str, filename: str) -> str:
    """Generates unique path by appending (N) if needed."""
    base_name, ext = os.path.splitext(filename)
    counter = 0
    unique_filename = filename
    final_path = os.path.join(directory, unique_filename)

    while os.path.exists(final_path):
        counter += 1
        unique_filename = f"{base_name} ({counter}){ext}"
        final_path = os.path.join(directory, unique_filename)
        
    return final_path

def prepare_download_file(file_path: str, expected_size: int):
    """Pre-allocates file with zeros or checks existing size for resume."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    if os.path.exists(file_path):
        current_size = os.path.getsize(file_path)
        if current_size == expected_size:
            logger.info(f"File '{file_path}' exists with correct size ({expected_size} bytes). Ready for resume.")
            return
        else:
            logger.warning(f"File '{file_path}' exists but size mismatch ({current_size} vs {expected_size}). Resetting file for full download.")
            # If size mismatch, truncate to 0 and re-create for a fresh download.
            with open(file_path, 'wb') as f:
                f.truncate(0)
    
    try:
        # Pre-allocate file with zeros
        with open(file_path, 'wb') as f:
            if expected_size > 0:
                f.seek(expected_size - 1)
                f.write(b'\0')
        logger.info(f"Pre-allocated file '{file_path}' with size {expected_size}.")
    except IOError as e:
        logger.error(f"Failed to prepare download file '{file_path}': {e}")
        raise