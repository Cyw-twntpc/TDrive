from abc import ABC, abstractmethod
from typing import Dict, Any

class TransferStrategy(ABC):
    def __init__(self, service_context):
        """
        :param service_context: Reference to TransferService to access shared resources 
                                (db, shared_state, controller, etc.)
        """
        self.context = service_context

    @abstractmethod
    async def start(self, *args, **kwargs):
        pass

    @abstractmethod
    async def cleanup(self, task_info: Dict[str, Any]):
        pass
