import asyncio
import time
import logging
import random
from typing import List, Callable, Set
import os

from core_app.infrastructure.local_fs import file_processor as fp
from core_app.core import crypto_handler as cr
from core_app.infrastructure.telegram import telegram_comms

logger = logging.getLogger(__name__)

CALLBACK_ELAPSED = 0.5

class TransferDispatcher:
    @staticmethod
    async def dispatch_upload(clients_pool: list, group_id: int, file_path: str, original_file_hash: str, 
                              progress_callback: Callable | None = None, resume_context: List = None,
                              chunk_callback: Callable[[int, int, str], None] = None) -> list:
        
        split_files_info = list(resume_context) if resume_context else []
        completed_parts = {item[0] for item in split_files_info}
        
        loop = asyncio.get_running_loop()
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        total_size = os.path.getsize(file_path)
        
        completed_bytes = sum(fp.CHUNK_SIZE for _ in completed_parts)
        if completed_bytes > total_size:
            completed_bytes = total_size

        active_chunk_progress = {}
        last_update_time = 0

        def get_progress_callback(part_num):
            def chunk_progress(current, total):
                nonlocal last_update_time
                active_chunk_progress[part_num] = current
                
                total_current = completed_bytes + sum(active_chunk_progress.values())
                if total_current > total_size: 
                    total_current = total_size
                
                now = time.time()
                if now - last_update_time > CALLBACK_ELAPSED:
                    last_update_time = now
                    if progress_callback:
                        progress_callback(total_current, total_size)
            return chunk_progress

        queue = asyncio.Queue(maxsize=len(clients_pool) * 2)

        async def producer():
            generator = fp.stream_split_and_encrypt(file_path, key, completed_parts)
            while True:
                try:
                    result = await loop.run_in_executor(None, next, generator, None)
                    if result is None:
                        break
                    part_num, part_bytes = result
                    if part_num in completed_parts:
                        continue
                    await queue.put((part_num, part_bytes))
                except StopIteration:
                    break
                except Exception as e:
                    logger.error(f"Error in encryption stream: {e}")
                    raise
            
            for _ in clients_pool:
                await queue.put(None)

        async def worker(client):
            nonlocal completed_bytes
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                
                part_num, part_bytes = item
                active_chunk_progress[part_num] = 0
                
                try:
                    cb = get_progress_callback(part_num)
                    part_hash = await loop.run_in_executor(None, cr.hash_bytes, part_bytes)
                    
                    # Humanized delay to prevent Telegram Anti-Spam FloodWait
                    await asyncio.sleep(random.uniform(0.1, 0.5))
                    
                    message = await telegram_comms.upload_single_chunk(client, group_id, part_bytes, cb)
                    
                    split_files_info.append([part_num, message.id, part_hash])
                    
                    del active_chunk_progress[part_num]
                    completed_bytes += len(part_bytes)
                    if completed_bytes > total_size: completed_bytes = total_size
                    
                    if chunk_callback:
                        chunk_callback(part_num, message.id, part_hash)
                except Exception as e:
                    logger.error(f"Worker upload failed for part {part_num}: {e}")
                    raise
                finally:
                    if part_num in active_chunk_progress:
                        del active_chunk_progress[part_num]
                    queue.task_done()

        prod_task = asyncio.create_task(producer())
        worker_tasks = [asyncio.create_task(worker(client)) for client in clients_pool]
        
        try:
            await asyncio.gather(prod_task, *worker_tasks)
        except Exception:
            prod_task.cancel()
            for w in worker_tasks:
                w.cancel()
            raise
        
        split_files_info.sort(key=lambda x: x[0])
        return split_files_info

    @staticmethod
    async def dispatch_download(clients_pool: list, group_id: int, file_details: dict, download_dir: str, 
                                progress_callback: Callable | None = None, completed_parts: Set[int] = None,
                                chunk_callback: Callable[[int], None] = None):
        
        if completed_parts is None:
            completed_parts = set()

        file_name = file_details['name']
        chunks = file_details['chunks']
        total_size = int(file_details['size'])
        final_path = os.path.join(download_dir, file_name)
        key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fp.prepare_download_file, final_path, total_size)

        completed_bytes = sum(fp.CHUNK_SIZE for c in chunks if c['part_num'] in completed_parts)
        if completed_bytes > total_size: completed_bytes = total_size

        active_chunk_progress = {}
        last_update_time = 0

        def get_progress_callback(part_num):
            def chunk_progress(current, total):
                nonlocal last_update_time
                active_chunk_progress[part_num] = current
                
                total_current = completed_bytes + sum(active_chunk_progress.values())
                if total_current > total_size: 
                    total_current = total_size
                
                now = time.time()
                if now - last_update_time > CALLBACK_ELAPSED:
                    last_update_time = now
                    if progress_callback:
                        progress_callback(total_current, total_size)
            return chunk_progress

        queue = asyncio.Queue()
        message_ids = []
        for chunk in chunks:
            if chunk['part_num'] not in completed_parts:
                message_ids.append(chunk['message_id'])
                await queue.put(chunk)
                
        if not message_ids and len(completed_parts) == len(chunks):
             logger.info("All parts marked as completed. Skipping download loop.")
             return

        # Prepare messages in batch using main client to get message objects
        # Note: iter_messages or get_messages is fast and gets the objects.
        # However, to download concurrently, we can just use the message_id!
        # Telethon's download_media supports iter_download by message_id if we fetch the message first,
        # but client.iter_download requires a Message object or InputFileLocation.
        # Actually, get_messages can be called by each client separately or by one client.
        
        messages = await clients_pool[0].get_messages(group_id, ids=message_ids)
        msg_map = {m.id: m for m in messages if m}
        
        if len(msg_map) != len(message_ids):
            raise ValueError(f"Requested {len(message_ids)} chunks but only got {len(msg_map)}. Cloud messages missing.")

        for _ in clients_pool:
            await queue.put(None)

        async def worker(client):
            nonlocal completed_bytes
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                
                part_num = item['part_num']
                msg_id = item['message_id']
                part_hash = item['part_hash']
                
                message = msg_map.get(msg_id)
                if not message:
                    queue.task_done()
                    continue
                    
                active_chunk_progress[part_num] = 0
                
                try:
                    cb = get_progress_callback(part_num)
                    
                    encrypted_bytes = await telegram_comms.download_single_chunk(client, message, cb)
                    
                    if not encrypted_bytes:
                        raise ValueError(f"Download failed for chunk {part_num}")
                    
                    actual_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_bytes)
                    if actual_hash != part_hash:
                        raise ValueError(f"Hash mismatch for chunk {part_num}. Expected {part_hash}, got {actual_hash}")
                    
                    decrypted_chunk = cr.decrypt(encrypted_bytes, key)
                    offset = (part_num - 1) * fp.CHUNK_SIZE
                    await loop.run_in_executor(None, fp.write_chunk_to_file, final_path, offset, decrypted_chunk)

                    del active_chunk_progress[part_num]
                    completed_bytes += fp.CHUNK_SIZE
                    if completed_bytes > total_size: completed_bytes = total_size
                    
                    if chunk_callback:
                        chunk_callback(part_num)

                except Exception as e:
                    logger.error(f"Worker download failed for part {part_num}: {e}")
                    raise
                finally:
                    if part_num in active_chunk_progress:
                        del active_chunk_progress[part_num]
                    queue.task_done()

        worker_tasks = [asyncio.create_task(worker(client)) for client in clients_pool]
        try:
            await asyncio.gather(*worker_tasks)
        except Exception:
            for w in worker_tasks:
                w.cancel()
            raise
