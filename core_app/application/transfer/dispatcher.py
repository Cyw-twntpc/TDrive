import asyncio
import itertools
import logging
import os
import random
import time
import weakref
from collections import defaultdict
from typing import Callable, List, Set

from core_app.core import crypto_handler as cr
from core_app.core import utils
from core_app.core.shared_state import SharedState
from core_app.infrastructure.local_fs import file_processor as fp
from core_app.infrastructure.telegram import telegram_comms

logger = logging.getLogger(__name__)

CALLBACK_ELAPSED = 0.5
MAX_INFLIGHT_UPLOAD_CHUNKS = 12
MAX_CHUNK_FAILOVER_ATTEMPTS = 3
PART_SUFFIX = ".tdrive.part"


class TransferDispatcher:
    """Dispatches parallel encrypted chunk uploads and downloads with bot failover and memory backpressure."""

    _upload_rr_counter = itertools.count()
    _chunk_buffer_sems: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    @classmethod
    def _get_chunk_buffer_sem(cls, loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
        """Return a per-event-loop semaphore capping global in-flight encrypted upload chunks."""
        sem = cls._chunk_buffer_sems.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(MAX_INFLIGHT_UPLOAD_CHUNKS)
            cls._chunk_buffer_sems[loop] = sem
        return sem

    @staticmethod
    def _select_failover_clients(shared_state: 'SharedState', start_index: int = 0) -> list:
        """Build ordered failover list: rotated worker bots first, main client as final fallback."""
        worker_bots = [
            bot for bot in getattr(shared_state, 'clients_pool', [])
            if bot and bot.is_connected()
        ]
        ordered = []
        if worker_bots:
            offset = start_index % len(worker_bots)
            ordered.extend(worker_bots[offset:] + worker_bots[:offset])

        main_client = getattr(shared_state, 'client', None)
        if main_client and main_client.is_connected() and main_client not in ordered:
            ordered.append(main_client)

        if not ordered:
            raise ConnectionError("No connected clients available for transfer.")
        return ordered[:MAX_CHUNK_FAILOVER_ATTEMPTS]

    @staticmethod
    def _make_progress_tracker(
        total_size: int,
        completed_bytes_ref: list[int],
        active_chunk_progress: dict[int, int],
        progress_callback: Callable | None,
    ) -> Callable[[int], Callable[[int, int], None]]:
        """Create throttled per-part progress callbacks shared across upload and download workers."""
        last_update_time = 0.0

        def get_progress_callback(part_num: int):
            def chunk_progress(current: int, _total: int):
                nonlocal last_update_time
                active_chunk_progress[part_num] = current
                total_current = min(total_size, completed_bytes_ref[0] + sum(active_chunk_progress.values()))
                now = time.time()
                if now - last_update_time > CALLBACK_ELAPSED:
                    last_update_time = now
                    if progress_callback:
                        progress_callback(total_current, total_size)
            return chunk_progress

        return get_progress_callback

    @staticmethod
    async def _upload_chunk_with_failover(
        shared_state: 'SharedState',
        group_id: int,
        part_num: int,
        part_bytes: bytes,
        progress_cb: Callable,
    ) -> int:
        """Upload one encrypted chunk, rotating to remaining healthy bots/main client on failure."""
        rr_idx = next(TransferDispatcher._upload_rr_counter)
        candidates = TransferDispatcher._select_failover_clients(shared_state, start_index=rr_idx)
        last_exc: Exception | None = None

        for attempt_idx, client in enumerate(candidates):
            client_name = getattr(client, 'tdrive_worker_name', 'Main_Account')
            target_idx, target_upload_group_id = utils.get_upload_target(shared_state, group_id)
            try:
                message = await telegram_comms.upload_single_chunk(client, target_upload_group_id, part_bytes, progress_cb)
                global_msg_id = utils.encode_global_msg_id(target_idx, message.id)
                logger.debug(f"Upload chunk {part_num} ({client_name}): target_group={target_upload_group_id} (idx={target_idx}), global_id={global_msg_id}")
                shared_state.current_group_msg_count += 1
                await telegram_comms.check_and_rotate_group_if_needed(shared_state)
                return global_msg_id
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Upload chunk {part_num} failed on {client_name} (attempt {attempt_idx + 1}/{len(candidates)}): {exc}")
                if attempt_idx + 1 < len(candidates):
                    await asyncio.sleep(0.1)

        raise last_exc  # type: ignore[misc]

    @staticmethod
    async def _download_chunk_with_failover(
        shared_state: 'SharedState',
        loop: asyncio.AbstractEventLoop,
        key: bytes,
        final_path: str,
        target_group_id: int,
        raw_msg_id: int,
        part_num: int,
        part_hash: str,
        progress_cb: Callable,
    ) -> None:
        """Download, verify hash, and write one chunk with automatic bot failover on RPC/hash errors."""
        candidates = TransferDispatcher._select_failover_clients(shared_state, start_index=part_num - 1)
        last_exc: Exception | None = None

        for attempt_idx, client in enumerate(candidates):
            client_name = getattr(client, 'tdrive_worker_name', 'Main_Account')
            try:
                client_msgs = await client.get_messages(target_group_id, ids=[raw_msg_id])
                if not client_msgs or not client_msgs[0]:
                    raise ValueError(f"Worker {client_name} could not access message {raw_msg_id} in group {target_group_id}")
                worker_message = client_msgs[0]

                encrypted_bytes = await telegram_comms.download_single_chunk(client, worker_message, progress_cb)
                if not encrypted_bytes:
                    raise ValueError(f"Download failed for chunk {part_num}")

                actual_hash = await loop.run_in_executor(None, cr.hash_bytes, encrypted_bytes)
                if actual_hash != part_hash:
                    raise ValueError(f"Hash mismatch for chunk {part_num}. Expected {part_hash}, got {actual_hash}")

                offset = (part_num - 1) * fp.CHUNK_SIZE
                await loop.run_in_executor(None, fp.decrypt_bytes_and_write, encrypted_bytes, final_path, key, offset)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Download chunk {part_num} failed on {client_name} (attempt {attempt_idx + 1}/{len(candidates)}): {exc}")
                if attempt_idx + 1 < len(candidates):
                    await asyncio.sleep(0.1)

        raise last_exc  # type: ignore[misc]

    @staticmethod
    async def _fetch_download_messages_batched(client, by_group_msg_ids: dict, batch_size: int = 100) -> dict:
        """Fetch Telegram messages per group in slices of batch_size to avoid large-file truncation."""
        msg_map = {}
        for target_gid, raw_ids in by_group_msg_ids.items():
            for i in range(0, len(raw_ids), batch_size):
                batch_ids = raw_ids[i:i + batch_size]
                messages = await client.get_messages(target_gid, ids=batch_ids)
                for m in (messages or []):
                    if m:
                        msg_map[(target_gid, m.id)] = m
        return msg_map

    @staticmethod
    async def dispatch_upload(
        shared_state: 'SharedState',
        group_id: int,
        file_path: str,
        original_file_hash: str,
        progress_callback: Callable | None = None,
        resume_context: List = None,
        chunk_callback: Callable[[int, int, str], None] = None,
    ) -> list:
        """Stream-encrypt a file and upload parts concurrently with bot failover and bounded RAM."""
        split_files_info = list(resume_context) if resume_context else []
        completed_parts = {item[0] for item in split_files_info}

        loop = asyncio.get_running_loop()
        chunk_buf_sem = TransferDispatcher._get_chunk_buffer_sem(loop)
        key = cr.generate_key(original_file_hash[:32], original_file_hash[-32:])
        total_size = await loop.run_in_executor(None, os.path.getsize, file_path)

        completed_bytes_ref = [min(total_size, sum(fp.CHUNK_SIZE for _ in completed_parts))]
        active_chunk_progress: dict[int, int] = {}
        get_progress_cb = TransferDispatcher._make_progress_tracker(
            total_size, completed_bytes_ref, active_chunk_progress, progress_callback
        )

        active_bots = [b for b in getattr(shared_state, 'clients_pool', []) if b and b.is_connected()]
        estimated_parts = max(1, (total_size + fp.CHUNK_SIZE - 1) // fp.CHUNK_SIZE) if total_size > 0 else 1
        max_workers = min(len(active_bots), estimated_parts) if active_bots else 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=max(2, max_workers))

        async def producer():
            generator = fp.stream_split_and_encrypt(file_path, key, completed_parts)
            while True:
                await chunk_buf_sem.acquire()
                enqueued = False
                try:
                    result = await loop.run_in_executor(None, next, generator, None)
                    if result is None:
                        chunk_buf_sem.release()
                        break
                    part_num, part_bytes = result
                    if part_num in completed_parts:
                        chunk_buf_sem.release()
                        continue
                    await queue.put((part_num, part_bytes))
                    enqueued = True
                except Exception as e:
                    if not enqueued:
                        chunk_buf_sem.release()
                    logger.error(f"Error in encryption stream: {e}")
                    raise
                except BaseException:
                    if not enqueued:
                        chunk_buf_sem.release()
                    raise

            for _ in range(max_workers):
                await queue.put(None)

        async def worker():
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break

                part_num, part_bytes = item
                active_chunk_progress[part_num] = 0
                try:
                    part_hash = await loop.run_in_executor(None, cr.hash_bytes, part_bytes)
                    await asyncio.sleep(random.uniform(0.02, 0.08))
                    global_msg_id = await TransferDispatcher._upload_chunk_with_failover(
                        shared_state, group_id, part_num, part_bytes, get_progress_cb(part_num)
                    )
                    split_files_info.append([part_num, global_msg_id, part_hash])

                    active_chunk_progress.pop(part_num, None)
                    completed_bytes_ref[0] = min(total_size, completed_bytes_ref[0] + len(part_bytes))
                    if chunk_callback:
                        chunk_callback(part_num, global_msg_id, part_hash)
                except Exception as e:
                    logger.error(f"Worker upload failed for part {part_num}: {e}")
                    raise
                finally:
                    active_chunk_progress.pop(part_num, None)
                    chunk_buf_sem.release()
                    queue.task_done()

        prod_task = asyncio.create_task(producer())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(max_workers)]

        try:
            await asyncio.gather(prod_task, *worker_tasks)
        except asyncio.CancelledError:
            logger.info("Upload dispatch cancelled.")
            prod_task.cancel()
            for w in worker_tasks:
                w.cancel()
            raise
        except Exception as e:
            logger.error("Upload dispatch failed: %s", e)
            prod_task.cancel()
            for w in worker_tasks:
                w.cancel()
            raise
        finally:
            while not queue.empty():
                leftover = queue.get_nowait()
                if leftover is not None:
                    chunk_buf_sem.release()
                queue.task_done()

        split_files_info.sort(key=lambda x: x[0])
        return split_files_info

    @staticmethod
    async def dispatch_download(
        shared_state: 'SharedState',
        file_details: dict,
        save_path: str,
        progress_callback: Callable | None = None,
        completed_parts: Set[int] = None,
        chunk_callback: Callable[[int], None] = None,
    ) -> None:
        """Fetch message metadata in batches and download/decrypt file chunks concurrently."""
        if completed_parts is None:
            completed_parts = set()

        chunks = file_details['chunks']
        total_size = int(file_details['size'])
        key = cr.generate_key(file_details['hash'][:32], file_details['hash'][-32:])
        part_path = f"{save_path}{PART_SUFFIX}"

        loop = asyncio.get_running_loop()
        if len(completed_parts) == len(chunks):
            logger.info("All parts marked as completed. Skipping download loop.")
            if await loop.run_in_executor(None, os.path.exists, part_path):
                await loop.run_in_executor(None, os.replace, part_path, save_path)
            return

        await loop.run_in_executor(None, fp.prepare_download_file, part_path, total_size)

        completed_bytes_ref = [min(total_size, sum(fp.CHUNK_SIZE for c in chunks if c['part_num'] in completed_parts))]
        active_chunk_progress: dict[int, int] = {}
        get_progress_cb = TransferDispatcher._make_progress_tracker(
            total_size, completed_bytes_ref, active_chunk_progress, progress_callback
        )

        queue: asyncio.Queue = asyncio.Queue()
        by_group_msg_ids = defaultdict(list)
        for chunk in chunks:
            if chunk['part_num'] not in completed_parts:
                g_idx, raw_id = utils.decode_global_msg_id(chunk['message_id'])
                target_gid = shared_state.get_telegram_group_id(g_idx)
                by_group_msg_ids[target_gid].append(raw_id)
                await queue.put(chunk)

        logger.debug(f"Download: decoded {len(chunks)} chunks across {len(by_group_msg_ids)} groups")
        total_pending = sum(len(ids) for ids in by_group_msg_ids.values())
        if total_pending == 0:
            logger.info("No pending chunks left to download.")
            if await loop.run_in_executor(None, os.path.exists, part_path):
                await loop.run_in_executor(None, os.replace, part_path, save_path)
            return

        msg_map = await TransferDispatcher._fetch_download_messages_batched(shared_state.client, by_group_msg_ids)
        if len(msg_map) != total_pending:
            raise ValueError(f"Requested {total_pending} chunks but only got {len(msg_map)}. Cloud messages missing.")

        active_bots = [b for b in getattr(shared_state, 'clients_pool', []) if b and b.is_connected()]
        max_workers = len(active_bots) if active_bots else 1
        for _ in range(max_workers):
            await queue.put(None)

        async def worker():
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break

                part_num = item['part_num']
                msg_id = item['message_id']
                part_hash = item['part_hash']
                g_idx, raw_msg_id = utils.decode_global_msg_id(msg_id)
                target_group_id = shared_state.get_telegram_group_id(g_idx)

                if not msg_map.get((target_group_id, raw_msg_id)):
                    logger.warning(f"Download: chunk {part_num} message {raw_msg_id} not found in msg_map, skipping")
                    queue.task_done()
                    continue

                active_chunk_progress[part_num] = 0
                try:
                    await asyncio.sleep(random.uniform(0.1, 0.5))
                    await TransferDispatcher._download_chunk_with_failover(
                        shared_state, loop, key, part_path, target_group_id, raw_msg_id,
                        part_num, part_hash, get_progress_cb(part_num)
                    )
                    active_chunk_progress.pop(part_num, None)
                    completed_bytes_ref[0] = min(total_size, completed_bytes_ref[0] + fp.CHUNK_SIZE)
                    if chunk_callback:
                        chunk_callback(part_num)
                except Exception as e:
                    logger.error(f"Worker download failed for part {part_num}: {e}")
                    raise
                finally:
                    active_chunk_progress.pop(part_num, None)
                    queue.task_done()

        worker_tasks = [asyncio.create_task(worker()) for _ in range(max_workers)]
        try:
            await asyncio.gather(*worker_tasks)
        except asyncio.CancelledError:
            logger.info("Download dispatch cancelled.")
            for w in worker_tasks:
                w.cancel()
            raise
        except Exception as e:
            logger.error("Download dispatch failed: %s", e)
            for w in worker_tasks:
                w.cancel()
            raise

        # Atomically promote .tdrive.part to final path only after all chunks succeed and verify
        if await loop.run_in_executor(None, os.path.exists, part_path):
            await loop.run_in_executor(None, os.replace, part_path, save_path)
