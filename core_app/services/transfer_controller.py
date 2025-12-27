import json
import os
import time
import threading
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class TransferController:
    """
    Manages the persistent state of file transfers (uploads/downloads).
    Refactored to support nested folder structures and atomic state updates.
    """
    
    STATE_FILE = os.path.join("file", "transfer_state.json")

    def __init__(self):
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = {
            "uploads": {},
            "downloads": {}
        }
        self._ensure_file_directory()
        self._load_state()

    def _ensure_file_directory(self):
        os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)

    def _load_state(self):
        """Loads the transfer state from disk."""
        with self._lock:
            if not os.path.exists(self.STATE_FILE):
                return

            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._state["uploads"] = data.get("uploads", {})
                    self._state["downloads"] = data.get("downloads", {})
                
                logger.info(f"Loaded {len(self._state['uploads'])} uploads and {len(self._state['downloads'])} downloads.")
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load transfer state file: {e}. Starting with empty state.")

    def _save_state(self):
        """
        Saves the current state to disk atomically.
        """
        with self._lock:
            try:
                temp_file = self.STATE_FILE + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self._state, f, indent=2, ensure_ascii=False)
                
                os.replace(temp_file, self.STATE_FILE)
            except IOError as e:
                logger.error(f"Failed to save transfer state: {e}")

    # --- Task Registration ---

    def add_upload_task(self, task_id: str, file_path: str, parent_id: int, 
                        total_size: int, is_folder: bool = False, 
                        file_hash: str = None):
        """
        Registers a new upload task (File or Folder).
        """
        with self._lock:
            now = time.time()
            task_data = {
                "type": "upload",
                "is_folder": is_folder,
                "file_path": file_path,
                "parent_id": parent_id,
                "total_size": total_size,
                "status": "queued",
                "created_at": now,
                "updated_at": now
            }

            if is_folder:
                task_data["child_tasks"] = {} # Structure: {sub_task_id: {sub_task_data}}
            else:
                task_data["file_hash"] = file_hash
                task_data["transferred_parts"] = []
                task_data["split_files_info"] = []

            self._state["uploads"][task_id] = task_data
            self._save_state()

    def add_download_task(self, task_id: str, db_id: int, save_path: str, 
                          total_size: int, is_folder: bool = False,
                          file_details: Dict = None):
        """
        Registers a new download task (File or Folder).
        """
        with self._lock:
            now = time.time()
            task_data = {
                "type": "download",
                "is_folder": is_folder,
                "db_id": db_id, # Source Cloud ID
                "save_path": save_path, # Local Destination
                "total_size": total_size,
                "status": "queued",
                "created_at": now,
                "updated_at": now
            }

            if is_folder:
                task_data["child_tasks"] = {}
            else:
                task_data["file_details"] = file_details # Contains hash, chunks info
                task_data["transferred_parts"] = []
            
            self._state["downloads"][task_id] = task_data
            self._save_state()

    def add_child_tasks_bulk(self, main_task_id: str, direction: str, child_tasks_map: Dict[str, Dict]):
        """
        Bulk adds child tasks to a folder task.
        
        Args:
            main_task_id: The UUID of the main folder task.
            direction: 'upload' or 'download'.
            child_tasks_map: A dictionary mapping sub_task_id to child task properties.
                             Example: { 'uuid-1': { 'file_path': '...', 'status': 'queued', ... } }
        """
        with self._lock:
            group = self._state["uploads"] if direction == "upload" else self._state["downloads"]
            task = group.get(main_task_id)
            
            if not task or not task.get("is_folder"):
                logger.warning(f"Cannot add child tasks: Main task {main_task_id} not found or is not a folder.")
                return

            # Initialize defaults for children if not present
            for sub_id, sub_data in child_tasks_map.items():
                if "transferred_parts" not in sub_data:
                    sub_data["transferred_parts"] = []
                if "status" not in sub_data:
                    sub_data["status"] = "queued"
                
                # For uploads, ensure split_files_info exists
                if direction == "upload" and "split_files_info" not in sub_data:
                    sub_data["split_files_info"] = []

            task["child_tasks"].update(child_tasks_map)
            self._save_state()

    # --- Progress Updates ---

    def update_progress(self, main_task_id: str, sub_task_id: str, part_num: int, extra_info: Any = None):
        """
        Updates the progress of a specific sub-task (or main task if single file).
        
        Args:
            main_task_id: The top-level task ID.
            sub_task_id: The actual task ID being updated (same as main_task_id for single files).
            part_num: The completed part number.
            extra_info: [message_id, part_hash] for uploads.
        """
        with self._lock:
            # Find the task
            task = self._state["uploads"].get(main_task_id) or self._state["downloads"].get(main_task_id)
            if not task:
                return

            target_data = None
            
            # Determine if we are updating the main task or a child task
            if main_task_id == sub_task_id:
                target_data = task
            elif task.get("is_folder") and "child_tasks" in task:
                target_data = task["child_tasks"].get(sub_task_id)

            if not target_data:
                logger.warning(f"Target task data not found for update: Main={main_task_id}, Sub={sub_task_id}")
                return

            # Update transferred parts
            if part_num not in target_data["transferred_parts"]:
                target_data["transferred_parts"].append(part_num)
                # target_data["transferred_parts"].sort() # Optional: Sort only when needed to save perf

            # Update metadata (Upload specific)
            if task["type"] == "upload" and extra_info:
                # extra_info is expected to be [message_id, part_hash]
                # We store [part_num, message_id, part_hash]
                record = [part_num, extra_info[0], extra_info[1]]
                
                # Deduplication check
                exists = False
                for item in target_data["split_files_info"]:
                    if item[0] == part_num:
                        exists = True
                        break
                if not exists:
                    # Optimize: Only sort if necessary
                    if not target_data["split_files_info"] or part_num > target_data["split_files_info"][-1][0]:
                        target_data["split_files_info"].append(record)
                    else:
                        target_data["split_files_info"].append(record)
                        target_data["split_files_info"].sort(key=lambda x: x[0])

            # Update status markers
            task["updated_at"] = time.time()
            if task["status"] == "queued":
                task["status"] = "transferring"
            
            # For child tasks, ensure they are marked as completed if needed? 
            # Ideally, the service layer handles completion logic, but we can set 'transferring' here.
            if target_data.get("status") == "queued":
                target_data["status"] = "transferring"

            self._save_state()

    def mark_sub_task_completed(self, main_task_id: str, sub_task_id: str):
        """Marks a specific sub-task (or single file task) as completed."""
        with self._lock:
            task = self._state["uploads"].get(main_task_id) or self._state["downloads"].get(main_task_id)
            if not task: return

            if main_task_id == sub_task_id:
                task["status"] = "completed"
            elif task.get("is_folder"):
                child = task["child_tasks"].get(sub_task_id)
                if child: child["status"] = "completed"
            
            task["updated_at"] = time.time()
            self._save_state()

    def mark_sub_task_failed(self, main_task_id: str, sub_task_id: str, error_msg: str):
        with self._lock:
            task = self._state["uploads"].get(main_task_id) or self._state["downloads"].get(main_task_id)
            if not task: return

            if main_task_id == sub_task_id:
                task["status"] = "failed"
                task["error_message"] = error_msg
            elif task.get("is_folder"):
                child = task["child_tasks"].get(sub_task_id)
                if child: 
                    child["status"] = "failed"
                    child["error_message"] = error_msg
            
            # If a child fails, we might generally mark the main task as 'failed' or keep it 'transferring'
            # depending on policy. For now, we update the main task timestamp.
            task["updated_at"] = time.time()
            self._save_state()

    # --- State Management ---

    def mark_paused(self, task_id: str):
        """Marks a main task as paused."""
        with self._lock:
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            if task:
                task["status"] = "paused"
                task["updated_at"] = time.time()
                self._save_state()

    def mark_failed(self, task_id: str, error_msg: str = ""):
        """Marks a main task as failed."""
        with self._lock:
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            if task:
                task["status"] = "failed"
                task["error_message"] = error_msg
                task["updated_at"] = time.time()
                self._save_state()

    def mark_resumed(self, task_id: str):
        """Marks a main task as queued (ready to resume)."""
        with self._lock:
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            if task:
                task["status"] = "queued"
                self._save_state()

    def remove_task(self, task_id: str):
        """Permanently removes a task."""
        with self._lock:
            if task_id in self._state["uploads"]:
                del self._state["uploads"][task_id]
            elif task_id in self._state["downloads"]:
                del self._state["downloads"][task_id]
            self._save_state()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        with self._lock:
            return {
                "uploads": json.loads(json.dumps(self._state["uploads"])),
                "downloads": json.loads(json.dumps(self._state["downloads"]))
            }

    def set_file_hash(self, main_task_id: str, sub_task_id: str, file_hash: str):
        """Updates the file_hash for an upload task or sub-task."""
        with self._lock:
            task = self._state["uploads"].get(main_task_id)
            if not task: return

            if main_task_id == sub_task_id:
                task["file_hash"] = file_hash
            elif task.get("is_folder") and "child_tasks" in task:
                child = task["child_tasks"].get(sub_task_id)
                if child:
                    child["file_hash"] = file_hash
            
            self._save_state()

    def reset_zombie_tasks(self):
        """
        Called on startup. Marks 'transferring' tasks as 'paused'.
        """
        with self._lock:
            changed = False
            for group in [self._state["uploads"], self._state["downloads"]]:
                for task in group.values():
                    if task["status"] == "transferring":
                        task["status"] = "paused"
                        changed = True
                    
                    # Also reset active child tasks if necessary
                    if task.get("is_folder"):
                        for child in task.get("child_tasks", {}).values():
                             if child.get("status") == "transferring":
                                 # We don't necessarily need a 'paused' state for children, 
                                 # but setting it helps logic consistency.
                                 # Or we can just leave them, as the main task pause is enough.
                                 pass 

            if changed:
                self._save_state()
                logger.info("Reset zombie tasks to 'paused' state.")