import asyncio
import os
import logging
from typing import Dict, Callable, Optional, List, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class FileStatusWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, db_handler: 'DatabaseHandler', status_change_callback: Callable[[List[Dict]], None], check_interval: float = 0.5):
        self._loop = loop
        self._db = db_handler
        self._callback = status_change_callback
        self._interval = check_interval
        
        # task_id -> { 'type': 'local'|'remote', 'target': path|id }
        self._watch_list: Dict[str, Dict[str, Any]] = {}
        
        # task_id -> last_known_existence_state (True/False)
        self._status_cache: Dict[str, bool] = {}
        
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None

    def start(self):
        if not self._running:
            self._running = True
            self._loop_task = self._loop.create_task(self._check_loop())
            logger.info("FileStatusWatcher started.")

    def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()

    def add_watch(self, task_id: str, target: Any, check_type: str = 'local'):
        if not target: return
        self._watch_list[task_id] = {'type': check_type, 'target': target}

    def remove_watch(self, task_id: str):
        self._watch_list.pop(task_id, None)
        self._status_cache.pop(task_id, None)

    def load_initial_watches(self, uploads: Dict[str, Dict], downloads: Dict[str, Dict]):
        for task_id, task in downloads.items():
            if task.get('status') == 'completed':
                path = task.get('save_path') or task.get('local_path')
                if path:
                    self.add_watch(task_id, path, 'local')

        for task_id, task in uploads.items():
            if task.get('status') == 'completed':
                remote_id = task.get('parent_id') or task.get('remote_id')
                if remote_id is not None:
                    self.add_watch(task_id, remote_id, 'remote')

    async def _check_loop(self):
        while self._running:
            try:
                changes = []
                items = list(self._watch_list.items())
                
                for task_id, info in items:
                    exists = False
                    
                    if info['type'] == 'local':
                        exists = os.path.exists(info['target'])
                    elif info['type'] == 'remote':
                        folder_id = info['target']
                        exists = await self._loop.run_in_executor(None, self._check_remote_exists, folder_id)

                    previous_state = self._status_cache.get(task_id)
                    
                    if previous_state != exists:
                        self._status_cache[task_id] = exists
                        changes.append({
                            "id": task_id,
                            "exists": exists
                        })

                if changes:
                    try:
                        self._callback(changes)
                    except Exception as e:
                        logger.error(f"Error in FileStatusWatcher callback: {e}")

            except Exception as e:
                logger.error(f"Error in FileStatusWatcher loop: {e}")
            
            await asyncio.sleep(self._interval)

    def _check_remote_exists(self, folder_id: int) -> bool:
        try:
            return self._db.check_folder_exists(folder_id)
        except Exception:
            return False

    def get_all_statuses(self) -> Dict[str, bool]:
        return self._status_cache.copy()
