"""
Provides core utilities for file processing, specifically for splitting files
into chunks for upload and reassembling them after download. This module
focuses on stream-based processing to handle large files efficiently without
consuming excessive memory.
"""
import os
import logging
from . import crypto_handler as cr

logger = logging.getLogger(__name__)

# The size of each file chunk in bytes.
# Set to 1.5 GB. This value is a balance between having too many small parts
# and consuming too much memory for a single part. It should be less than
# Telegram's maximum file size limit (currently 2 GB for free users).
CHUNK_SIZE = int(1024 * 1024 * 1024 * 1.5)

def stream_split_and_encrypt(file_path: str, key: bytes, file_hash: str, temp_dir: str):
    """
    Reads a file in a stream, encrypts it chunk by chunk, and writes each
    encrypted chunk to a temporary file.

    This is a generator function that yields the part number and the path to
    the temporary encrypted chunk file, allowing the caller to process each
    chunk (e.g., by uploading it) as it's created.

    Args:
        file_path: The absolute path to the source file.
        key: The encryption key to use.
        file_hash: The hash of the original file, used for naming temp files.
        temp_dir: The directory where temporary chunk files will be stored.
    
    Yields:
        A tuple of (part_number, temp_chunk_path).
    """
    file_name_base = file_hash[:20]
    logger.debug(f"Starting stream split for '{file_path}' into temp dir '{temp_dir}'.")
    with open(file_path, 'rb') as f_in:
        i = 1
        while True:
            chunk = f_in.read(CHUNK_SIZE)
            if not chunk:
                break
            
            output_path = os.path.join(temp_dir, f"{file_name_base}.part_{i}")
            logger.debug(f"Encrypting chunk {i} to '{output_path}'...")
            with open(output_path, 'wb') as f_out:
                f_out.write(cr.encrypt(chunk, key))
            
            yield i, output_path
            
            i += 1
    logger.debug(f"Finished stream splitting for '{file_path}'.")

def decrypt_and_write_chunk(part_path: str, output_path: str, key: bytes, offset: int):
    """
    Decrypts a single file chunk and writes its content to a specific offset
    in the final output file.

    This function is designed to work on a pre-allocated file, writing the
    decrypted data directly to its final position, which is crucial for
    reassembling large files without high memory usage.

    Args:
        part_path: The path to the encrypted chunk file.
        output_path: The path to the final, reassembled file.
        key: The decryption key.
        offset: The byte offset in the output file where writing should start.
    """
    try:
        # Open in 'r+b' mode to write to an existing file at a specific offset.
        with open(output_path, 'r+b') as f_out:
            with open(part_path, 'rb') as f_in:
                encrypted_content = f_in.read()
            
            decrypted_content = cr.decrypt(encrypted_content, key)
            f_out.seek(offset)
            f_out.write(decrypted_content)
    except IOError as e:
        raise IOError(f"An error occurred while writing to output file '{output_path}': {e}") from e