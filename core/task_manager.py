"""
ZEUS Task Manager
Tracks the lifecycle of multi-step tasks so the UI can show progress
and background work is never invisible.
"""

import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("tasks")

_id_counter = itertools.count(1)


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Task:
    id: int
    description: str
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0
    steps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    result: Optional[Any] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    _cancel_flag: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "description": self.description, "status": self.status.value,
            "progress": self.progress, "steps": self.steps, "errors": self.errors,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def is_cancelled(self) -> bool:
        return self._cancel_flag.is_set()


class TaskManager:
    def __init__(self):
        self._tasks: Dict[int, Task] = {}
        self._lock = threading.Lock()

    def create(self, description: str) -> Task:
        with self._lock:
            task = Task(id=next(_id_counter), description=description)
            self._tasks[task.id] = task
        logger.info(f"TASK_CREATED #{task.id}: {description}")
        return task

    def get(self, task_id: int) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_active(self) -> List[Task]:
        return [t for t in self._tasks.values()
                if t.status in (TaskStatus.QUEUED, TaskStatus.PLANNING,
                                TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER)]

    def list_all(self, limit: int = 50) -> List[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]

    def update(self, task_id: int, **kwargs):
        task = self._tasks.get(task_id)
        if not task:
            return
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        if kwargs.get("status") == TaskStatus.RUNNING and task.started_at is None:
            task.started_at = time.time()
        if kwargs.get("status") in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            task.completed_at = time.time()
        logger.debug(f"TASK_UPDATED #{task_id}: {kwargs}")

    def add_step(self, task_id: int, step: str):
        task = self._tasks.get(task_id)
        if task:
            task.steps.append(step)

    def cancel(self, task_id: int):
        task = self._tasks.get(task_id)
        if task:
            task._cancel_flag.set()
            self.update(task_id, status=TaskStatus.CANCELLED)
            logger.info(f"TASK_CANCELLED #{task_id}")


task_manager = TaskManager()
