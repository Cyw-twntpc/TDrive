"""
Provides core utilities for file processing, specifically for splitting files
into chunks for upload and reassembling them after download. This module
focuses on stream-based processing to handle large files efficiently without
consuming excessive memory.
"""
import logging
from typing import Generator, Any
from . import crypto_handler as cr

logger = logging.getLogger(__name__)

# The size of each file chunk in bytes.
CHUNK_SIZE = int(1024 * 1024 * 32)

def stream_split_and_encrypt(file_path: str, key: bytes) -> Generator[tuple[int, bytes], Any, None]:
    """
    Reads a file in a stream, encrypts it chunk by chunk, and writes each
    encrypted chunk to a temporary file.

    This is a generator function that yields the part number and the bytes of
    the temporary encrypted chunk file, allowing the caller to process each
    chunk (e.g., by uploading it) as it's created.

    Args:
        file_path: The absolute path to the source file.
        key: The encryption key to use.
    
    Yields:
        A tuple of (part_number, chunk_bytes).
    """
    logger.debug(f"Starting stream split for '{file_path}'.")
    with open(file_path, 'rb') as f_in:
        i = 1
        while True:
            chunk = f_in.read(CHUNK_SIZE)
            if not chunk:
                break

            logger.debug(f"Encrypting chunk {i} ...")
            yield i, cr.encrypt(chunk, key)
            i += 1

    logger.debug(f"Finished stream splitting for '{file_path}'.")
    
def decrypt_bytes_and_write(encrypted_bytes: bytes, output_path: str, key: bytes, offset: int):
    """
    Decrypts bytes from memory and writes them to a specific offset in the output file.
    Designed to run in a background thread.
    """
    try:
        # 解密 (CPU 密集)
        decrypted_content = cr.decrypt(encrypted_bytes, key)
        
        # 寫入指定位置 (I/O 阻塞)
        with open(output_path, 'r+b') as f_out:
            f_out.seek(offset)
            f_out.write(decrypted_content)
    except IOError as e:
        raise IOError(f"An error occurred while writing to output file '{output_path}': {e}") from e