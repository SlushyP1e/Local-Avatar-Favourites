"""Background job tracking for long-running operations.

Bulk metadata fetches are slow by design: the API client enforces a hard rate
limit because VRChat terminates accounts for abuse, so refreshing a few hundred
avatars takes minutes. That is only acceptable if the user can see what is
happening and stop it.

A job is deliberately tiny and JSON-serialisable, since it is handed to the
frontend as-is. Jobs are held in memory only -- they describe work started in
this session, and there is nothing to resume after a restart.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Jobs are dropped once finished and collected, so a long session cannot
# accumulate them. Callers poll get_job() frequently enough.
JOB_TTL_SECONDS = 300.0
MAX_JOBS = 20


@dataclass
class Job:
    kind: str
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    cancelled: bool = False
    finished: bool = False
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float = 0.0
    id: str = ""

    def as_dict(self) -> dict:
        elapsed = (self.ended_at or time.monotonic()) - self.started_at
        percent = 0.0
        if self.total > 0:
            percent = min(100.0, (self.done / self.total) * 100.0)
        elif self.finished:
            percent = 100.0
        return {
            "id": self.id,
            "kind": self.kind,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "current": self.current,
            "message": self.message,
            "cancelled": self.cancelled,
            "finished": self.finished,
            "percent": round(percent, 1),
            "elapsed": round(elapsed, 1),
        }


class JobRegistry:
    """Thread-safe collection of jobs, keyed by short id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    def create(self, kind: str, total: int = 0, message: str = "") -> tuple[str, Job]:
        with self._lock:
            self._counter += 1
            job_id = f"{kind}-{self._counter}"
            job = Job(kind=kind, total=total, message=message, id=job_id)
            self._jobs[job_id] = job
            self._collect_locked()
            return job_id, job

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            return job.as_dict() if job else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if not job or job.finished:
                return False
            job.cancelled = True
            job.message = "Cancelling..."
            return True

    def active(self) -> dict | None:
        """The most recent job that has not finished, if any."""
        with self._lock:
            running = [j for j in self._jobs.values() if not j.finished]
            if not running:
                return None
            job = max(running, key=lambda j: j.started_at)
            return job.as_dict()

    def _collect_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key for key, job in self._jobs.items()
            if job.finished and (now - job.ended_at) > JOB_TTL_SECONDS
        ]
        for key in stale:
            del self._jobs[key]
        if len(self._jobs) > MAX_JOBS:
            finished = sorted(
                (j for j in self._jobs.values() if j.finished),
                key=lambda j: j.ended_at,
            )
            for job in finished[: len(self._jobs) - MAX_JOBS]:
                self._jobs.pop(_id_of(self._jobs, job), None)


def _id_of(jobs: dict[str, Job], target: Job) -> str:
    for key, job in jobs.items():
        if job is target:
            return key
    return ""


class JobRunner:
    """Runs a worker loop over a list of ids, reporting progress and honouring cancel."""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def start(self, kind: str, ids: list[str], worker, message: str = "") -> str:
        job_id, job = self._registry.create(kind, total=len(ids), message=message)

        def run() -> None:
            try:
                for avatar_id in ids:
                    if job.cancelled:
                        break
                    job.current = avatar_id
                    try:
                        if worker(avatar_id):
                            job.ok += 1
                        else:
                            job.failed += 1
                    except Exception:
                        job.failed += 1
                    job.done += 1
            finally:
                job.finished = True
                job.current = ""
                job.ended_at = time.monotonic()
                if job.cancelled:
                    job.message = f"Cancelled after {job.done} of {job.total}."
                else:
                    job.message = _summarise(job)

        threading.Thread(target=run, daemon=True, name=f"job-{job_id}").start()
        return job_id


def _summarise(job: Job) -> str:
    if job.total == 0:
        return "Nothing to do."
    parts = [f"{job.ok} updated"]
    if job.failed:
        parts.append(f"{job.failed} failed")
    return " · ".join(parts) + f" ({job.total} total)."


__all__ = ["Job", "JobRegistry", "JobRunner"]
