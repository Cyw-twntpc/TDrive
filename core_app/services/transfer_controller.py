import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from collections import defaultdict
import asyncio

from core_app.data.transfer_db_handler import TransferDBHandler

logger = logging.getLogger(__name__)

class TransferController:
    def __init__(self):
        self.db = TransferDBHandler()
        self._today_traffic = 0
        self._unsaved_traffic = 0
        self._traffic_save_threshold = 500 * 1024 # 500 KB
        self._last_date_str = self._get_today_str()
        self._load_initial_traffic()
        self._traffic_lock = asyncio.Lock()
        self._main_update_counters = defaultdict(int)

    def _get_today_str(self) -> str:
        return datetime.now().strftime('%Y-%m-%d')

    def _load_initial_traffic(self):
        self._today_traffic = self.db.get_traffic_by_date(self._last_date_str)

    def get_today_traffic(self) -> int:
        current_date = self._get_today_str()
        if current_date != self._last_date_str:
            self._last_date_str = current_date
            self._today_traffic = 0
            self._unsaved_traffic = 0
            
        return self._today_traffic

    async def update_transferred_bytes(self, delta: int):
        if delta <= 0:
            return

        async with self._traffic_lock:
            current_date = self._get_today_str()
            
            if current_date != self._last_date_str:
                self._last_date_str = current_date
                self._today_traffic = 0
                self._unsaved_traffic = 0
            
            self._today_traffic += delta
            self._unsaved_traffic += delta
            
            if self._unsaved_traffic >= self._traffic_save_threshold:
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._persist_traffic_chunk)
                except RuntimeError:
                    pass

    def _persist_traffic_chunk(self):
        if self._unsaved_traffic > 0:
            to_save = self._unsaved_traffic
            self.db.update_traffic(self._last_date_str, to_save)
            self._unsaved_traffic -= to_save

    def save_pending_traffic_stats(self):
        self._persist_traffic_chunk()
        logger.info("Pending traffic statistics have been persisted.")

    # --- Task Registration ---

    def add_upload_task(self, task_id: str, file_path: str, parent_id: int, 
                        total_size: int, is_folder: bool = False, 
                        file_hash: str = None):
        now = time.time()
        
        main_task_data = {
            "task_id": task_id,
            "type": "upload",
            "is_folder": is_folder,
            "status": "queued",
            "total_size": total_size,
            "created_at": now,
            "updated_at": now,
            "local_path": file_path,
            "remote_id": parent_id
        }
        self.db.create_main_task(main_task_data)

        if not is_folder:
            sub_tasks = [{
                "sub_task_id": task_id,
                "main_task_id": task_id,
                "status": "queued",
                "local_path": file_path,
                "remote_id": parent_id,
                "total_size": total_size,
                "file_hash": file_hash
            }]
            self.db.create_sub_tasks_bulk(sub_tasks)

    def add_download_task(self, task_id: str, db_id: int, save_path: str, 
                          total_size: int, is_folder: bool = False,
                          file_details: Dict = None):
        now = time.time()
        
        main_task_data = {
            "task_id": task_id,
            "type": "download",
            "is_folder": is_folder,
            "status": "queued",
            "total_size": total_size,
            "created_at": now,
            "updated_at": now,
            "local_path": save_path,
            "remote_id": db_id
        }
        self.db.create_main_task(main_task_data)

        if not is_folder:
            sub_tasks = [{
                "sub_task_id": task_id,
                "main_task_id": task_id,
                "status": "queued",
                "local_path": save_path,
                "remote_id": db_id,
                "total_size": total_size,
                "file_details": file_details
            }]
            self.db.create_sub_tasks_bulk(sub_tasks)

    def add_child_tasks_bulk(self, main_task_id: str, child_tasks_map: Dict[str, Dict]):
        sub_tasks_to_create = []
        for sub_id, data in child_tasks_map.items():
            st_data = {
                "sub_task_id": sub_id,
                "main_task_id": main_task_id,
                "status": data.get("status", "queued"),
                "local_path": data.get("file_path") or data.get("save_path"),
                "remote_id": data.get("parent_id") or data.get("db_id"),
                "total_size": data.get("total_size", 0),
                "file_hash": data.get("file_hash"),
                "file_details": data.get("file_details")
            }
            sub_tasks_to_create.append(st_data)
        
        self.db.create_sub_tasks_bulk(sub_tasks_to_create)

    # --- Progress Updates ---

    def update_progress(self, main_task_id: str, sub_task_id: str, part_num: int, extra_info: Any = None):
        msg_id = extra_info[0] if extra_info and len(extra_info) >= 1 else None
        p_hash = extra_info[1] if extra_info and len(extra_info) >= 2 else None
        
        self.db.add_progress_part(sub_task_id, part_num, msg_id, p_hash)
        
        current_status = self.db.get_main_task_status(main_task_id)
        if current_status == "queued":
            self.db.update_main_task_status(main_task_id, "transferring", time.time())
        else:
            self.db.update_main_task_status(main_task_id, current_status or "transferring", time.time())
        
        self.db.update_sub_task_status(sub_task_id, "transferring")

    def mark_sub_task_completed(self, main_task_id: str, sub_task_id: str):
        self.db.update_sub_task_status(sub_task_id, "completed")
        
        if main_task_id == sub_task_id:
            self.db.update_main_task_status(main_task_id, "completed", time.time())
        else:
            current_status = self.db.get_main_task_status(main_task_id)
            self.db.update_main_task_status(main_task_id, current_status or "transferring", time.time())

    def mark_sub_task_failed(self, main_task_id: str, sub_task_id: str, error_msg: str):
        self.db.update_sub_task_status(sub_task_id, "failed")
        self.db.update_main_task_status(main_task_id, "failed", time.time(), error_msg=error_msg)

    # --- State Management ---

    def mark_paused(self, task_id: str):
        self.db.update_main_task_status(task_id, "paused", time.time())

    def mark_failed(self, task_id: str, error_msg: str = ""):
        self.db.update_main_task_status(task_id, "failed", time.time(), error_msg=error_msg)

    def mark_resumed(self, task_id: str):
        self.db.update_main_task_status(task_id, "queued", time.time())

    def update_task_total_size(self, task_id: str, new_size: int):
        self.db.update_main_task_total_size(task_id, new_size)

    def remove_task(self, task_id: str):
        self.db.delete_task(task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_task(task_id)

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        return self.db.get_all_tasks()

    def set_file_hash(self, sub_task_id: str, file_hash: str):
        self.db.update_sub_task_hash(sub_task_id, file_hash)

    def reset_zombie_tasks(self):
        self.db.reset_zombie_tasks()
        logger.info("Reset zombie tasks in SQL database to 'paused' state.")

    async def pause_all_sub_tasks(self, main_task_id: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.db.pause_active_sub_tasks, main_task_id)

    # --- Artifact Tracking ---

    def record_created_artifact(self, task_id: str, artifact_type: str, db_id: int):
        self.db.add_created_artifact(task_id, artifact_type, db_id)

    def get_created_artifacts(self, task_id: str) -> list:
        return self.db.get_created_artifacts(task_id)
