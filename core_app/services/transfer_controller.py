import json
import os
import time
import threading
import logging
from typing import Dict, Any, List, Optional, Set

logger = logging.getLogger(__name__)

class TransferController:
    """
    Manages the persistent state of file transfers (uploads/downloads) to support
    resume functionality (斷點續傳).
    
    Features:
    - Thread-safe access using RLock.
    - Atomic writes to prevent JSON corruption.
    - REAL-TIME SAVING: Every state change is immediately flushed to disk.
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
                    # Basic schema validation
                    self._state["uploads"] = data.get("uploads", {})
                    self._state["downloads"] = data.get("downloads", {})
                
                # Patch legacy data: ensure transferred_parts exists
                for t in self._state["uploads"].values():
                    if "transferred_parts" not in t: t["transferred_parts"] = []
                for t in self._state["downloads"].values():
                    if "transferred_parts" not in t: t["transferred_parts"] = []

                logger.info(f"Loaded {len(self._state['uploads'])} uploads and {len(self._state['downloads'])} downloads from state file.")
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load transfer state file: {e}. Starting with empty state.")
                # If file is corrupted, we might want to backup it or just reset.
                # Here we strictly stick to memory reset to avoid crashing.

    def _save_state(self):
        """
        Saves the current state to disk atomically.
        No throttling is applied; writes happen immediately.
        """
        with self._lock:
            try:
                temp_file = self.STATE_FILE + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self._state, f, indent=2, ensure_ascii=False)
                
                # Atomic replacement
                os.replace(temp_file, self.STATE_FILE)
            except IOError as e:
                logger.error(f"Failed to save transfer state: {e}")

    # --- Task Management ---

    def add_upload_task(self, task_id: str, file_path: str, parent_id: int, 
                        total_size: int, file_hash: str, split_files_info: List = None):
        """Registers a new upload task."""
        with self._lock:
            now = time.time()
            existing = self._state["uploads"].get(task_id)
            transferred_parts = existing["transferred_parts"] if existing else []
            # Merge split_files_info if existing
            current_split_info = split_files_info or []
            if existing and existing.get("split_files_info"):
                 # Simple merge strategy: prefer existing if not provided
                 if not current_split_info:
                     current_split_info = existing["split_files_info"]

            self._state["uploads"][task_id] = {
                "type": "upload",
                "file_path": file_path,
                "parent_id": parent_id,
                "total_size": total_size,
                "transferred_parts": transferred_parts,
                "status": "queued",
                "file_hash": file_hash, # Essential for key generation on resume
                "split_files_info": current_split_info,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now
            }
            self._save_state()

    def add_download_task(self, task_id: str, db_id: int, save_path: str, 
                          total_size: int, file_details: Dict):
        """Registers a new download task."""
        with self._lock:
            now = time.time()
            existing = self._state["downloads"].get(task_id)
            transferred_parts = existing["transferred_parts"] if existing else []

            self._state["downloads"][task_id] = {
                "type": "download",
                "db_id": db_id,
                "save_path": save_path,
                "total_size": total_size,
                "transferred_parts": transferred_parts,
                "status": "queued",
                "file_details": file_details, # Essential to avoid DB lookup on resume
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now
            }
            self._save_state()

    def update_progress(self, task_id: str, part_num: int, extra_info: Any = None):
        """
        Updates the progress of a task and SAVES IMMEDIATELY.
        
        Args:
            task_id: The UUID of the task.
            part_num: The part number that was just completed.
            extra_info: For uploads, this is the [part_num, msg_id, hash] list item.
        """
        with self._lock:
            # Check uploads
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            
            if not task:
                logger.warning(f"Attempted to update progress for unknown task: {task_id}")
                return

            if part_num not in task["transferred_parts"]:
                task["transferred_parts"].append(part_num)
                # Keep it sorted for easier debugging/logic
                task["transferred_parts"].sort()

            # For uploads, we need to accumulate the Telegram message info
            if task["type"] == "upload" and extra_info:
                # extra_info expected to be [part_num, message_id, part_hash]
                # Avoid duplicates
                exists = False
                for item in task["split_files_info"]:
                    if item[0] == extra_info[0]: # Same part num
                        exists = True
                        break
                if not exists:
                    task["split_files_info"].append(extra_info)
                    # Sort by part number
                    task["split_files_info"].sort(key=lambda x: x[0])

            task["updated_at"] = time.time()
            if task.get("status") in ["queued", "transferring"]:
                task["status"] = "transferring"
            
            # Save immediately (Real-time requirement)
            self._save_state()

    def mark_paused(self, task_id: str):
        """Marks a task as paused. Saved immediately."""
        with self._lock:
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            if task:
                task["status"] = "paused"
                task["updated_at"] = time.time()
                self._save_state()
                logger.info(f"Task {task_id} marked as paused.")

    def mark_failed(self, task_id: str, error_msg: str = ""):
        """Marks a task as failed. Saved immediately."""
        with self._lock:
            task = self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)
            if task:
                task["status"] = "failed"
                task["error_message"] = error_msg
                task["updated_at"] = time.time()
                self._save_state()

    def mark_resumed(self, task_id: str):
        """
        Explicitly marks a task as queued/resumed.
        This is required so that update_progress knows it's allowed to update the status.
        """
        with self._lock:
            # 檢查 Uploads
            if task_id in self._state["uploads"]:
                self._state["uploads"][task_id]["status"] = "queued"
                self._save_state()
            # 檢查 Downloads
            elif task_id in self._state["downloads"]:
                self._state["downloads"][task_id]["status"] = "queued"
                self._save_state()

    def remove_task(self, task_id: str):
        """Permanently removes a task (e.g., completed or cancelled). Saved immediately."""
        with self._lock:
            if task_id in self._state["uploads"]:
                del self._state["uploads"][task_id]
            elif task_id in self._state["downloads"]:
                del self._state["downloads"][task_id]
            
            self._save_state()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves task details by ID."""
        with self._lock:
            return self._state["uploads"].get(task_id) or self._state["downloads"].get(task_id)

    def get_incomplete_transfers(self) -> Dict[str, Dict]:
        """Returns all tasks that are not implicitly 'removed' (so effectively all in the file)."""
        with self._lock:
            # Return a copy to prevent external modification
            return {
                "uploads": json.loads(json.dumps(self._state["uploads"])),
                "downloads": json.loads(json.dumps(self._state["downloads"]))
            }

    def reset_zombie_tasks(self):
        """
        Called on startup. Marks 'transferring' tasks as 'paused'.
        This handles cases where the app crashed or was killed.
        """
        with self._lock:
            changed = False
            for group in [self._state["uploads"], self._state["downloads"]]:
                for task in group.values():
                    if task["status"] == "transferring":
                        task["status"] = "paused"
                        changed = True
            if changed:
                self._save_state()
                logger.info("Reset zombie tasks to 'paused' state.")