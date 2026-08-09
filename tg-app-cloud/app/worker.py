import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class JobKind(str, Enum):
    AUTHORIZE = "authorize"
    UPLOAD = "upload"
    DOWNLOAD = "download"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    kind: JobKind
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    payload: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    error: str = ""
    # For multi-file upload batches, parent tracks children
    file_name: str = ""
    file_size: int = 0


class JobQueue:
    """Single-worker FIFO queue — one Telegram/rclone job at a time."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._cv = threading.Condition()
        self._handlers: dict[JobKind, Callable[[Job], None]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = False

    def register(self, kind: JobKind, handler: Callable[[Job], None]) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def enqueue(self, kind: JobKind, payload: dict, file_name: str = "", file_size: int = 0) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            payload=payload,
            file_name=file_name,
            file_size=file_size,
        )
        with self._cv:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._cv.notify()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 100) -> list[Job]:
        items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return items[:limit]

    def update(self, job_id: str, **kwargs: Any) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = time.time()

    def _next_queued(self) -> Optional[Job]:
        for job_id in self._order:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.QUEUED:
                return job
        return None

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stop:
                    job = self._next_queued()
                    if job:
                        break
                    self._cv.wait(timeout=1.0)
                else:
                    return
                job.status = JobStatus.RUNNING
                job.message = "Running"
                job.updated_at = time.time()

            handler = self._handlers.get(job.kind)
            try:
                if not handler:
                    raise RuntimeError(f"No handler for {job.kind}")
                handler(job)
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.DONE
                    job.progress = 100.0
                    if not job.message or job.message == "Running":
                        job.message = "Done"
            except Exception as exc:
                job.status = JobStatus.ERROR
                job.error = str(exc)
                job.message = str(exc)
            finally:
                job.updated_at = time.time()


job_queue = JobQueue()
