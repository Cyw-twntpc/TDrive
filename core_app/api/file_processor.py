"""
Provides core utilities for file processing, specifically for splitting files
into chunks for upload and reassembling them after download. This module
focuses on stream-based processing to handle large files efficiently without
consuming excessive memory.
"""
import logging
import os
from typing import Generator, Any, Set, Optional
from . import crypto_handler as cr

logger = logging.getLogger(__name__)

# The size of each file chunk in bytes.
CHUNK_SIZE = int(1024 * 1024 * 32)

def stream_split_and_encrypt(file_path: str, key: bytes, completed_parts: Optional[Set[int]] = None) -> Generator[tuple[int, bytes], Any, None]:
    """
    Reads a file in a stream, encrypts it chunk by chunk, and yields them.
    Supports skipping already uploaded parts for resume functionality.

    Args:
        file_path: The absolute path to the source file.
        key: The encryption key to use.
        completed_parts: A set of part numbers (1-based) that should be skipped.
    
    Yields:
        A tuple of (part_number, encrypted_chunk_bytes).
    """
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
    """
    Decrypts bytes from memory and writes them to a specific offset in the output file.
    Designed to run in a background thread.
    
    Note: The output_path MUST exist and have sufficient size before calling this.
    Use prepare_download_file() beforehand.
    """
    try:
        # Decrypt (CPU bound)
        decrypted_content = cr.decrypt(encrypted_bytes, key)
        
        # Write to specific offset (I/O bound)
        # 'r+b' opens for reading and writing without truncating the file
        with open(output_path, 'r+b') as f_out:
            f_out.seek(offset)
            f_out.write(decrypted_content)
    except IOError as e:
        raise IOError(f"An error occurred while writing to output file '{output_path}': {e}") from e

def get_unique_filepath(directory: str, filename: str) -> str:
    """
    Generates a unique file path by appending (N) if a file with the same name
    already exists in the directory.

    Example: "file.txt" -> "file (1).txt" -> "file (2).txt"
    """
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
    """
    Ensures the destination file exists and has the correct size before downloading.
    This function expects 'file_path' to be the final, unique path determined by the caller.
    
    - If file exists and size matches: Leaves it as is (Resume mode).
    - If file exists but size mismatches: Resets (overwrites) the file.
    """
    # Create directory if needed
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    # If file exists and size matches, it's ready for resume.
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