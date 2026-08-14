"""
Background execution for pipeline runs.

A run takes 10-90s (crawl + LLM + sandbox). Running it inside the HTTP request
held the connection open for the whole thing, so a browser reload lost the
result and two runs could not overlap.

This is a bounded thread pool rather than a Celery/RQ setup on purpose: the
pipeline already records its progress in MySQL at every stage, so the only
thing actually missing was somewhere for the work to live. Adding a broker
would mean new infrastructure for every teammate to run.

Concurrency is deliberately small. Each run drives a real browser through
Playwright, and docker-compose caps the backend at 4 GB.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

ACTIVE_STATES = {QUEUED, RUNNING}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobAlreadyActive(RuntimeError):
    """Raised when a report already has a queued or running job."""


class RunJob:
    def __init__(self, report_id: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.report_id = report_id
        self.status = QUEUED
        self.queued_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "jobId": self.id,
            "reportId": self.report_id,
            "status": self.status,
            "queuedAt": self.queued_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "error": self.error,
            "result": self.result,
        }


class JobQueue:
    """
    Deliberately daemon threads plus a semaphore rather than a
    ThreadPoolExecutor. The executor's workers are non-daemon and
    concurrent.futures registers an atexit hook that joins them, so a run in
    flight blocks interpreter shutdown. Under `flask --debug` that means an
    edit triggers a reload, the old process cannot exit until a 90-second
    crawl finishes, and it sits on port 5000 refusing connections the whole
    time. Daemon threads let the process die immediately; a killed run just
    leaves its ticket in-process, which the UI already flags as stalled.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._slots = threading.BoundedSemaphore(max_workers)
        self._jobs: Dict[str, RunJob] = {}
        self._lock = threading.Lock()
        self.max_workers = max_workers

    def active_for(self, report_id: str) -> Optional[RunJob]:
        with self._lock:
            job = self._jobs.get(report_id)
            return job if job and job.status in ACTIVE_STATES else None

    def submit(self, report_id: str, work: Callable[[], Dict[str, Any]]) -> RunJob:
        """
        Queue a run. Refuses if this report is already queued or running, so a
        double-click cannot start two browsers against the same report.
        """
        with self._lock:
            existing = self._jobs.get(report_id)
            if existing and existing.status in ACTIVE_STATES:
                raise JobAlreadyActive(
                    f"{report_id} is already {existing.status}"
                )
            job = RunJob(report_id)
            self._jobs[report_id] = job

        thread = threading.Thread(
            target=self._run,
            args=(job, work),
            name=f"pipeline-{job.id}",
            daemon=True,
        )
        thread.start()
        logger.info("Job %s queued for %s", job.id, report_id)
        return job

    def _run(self, job: RunJob, work: Callable[[], Dict[str, Any]]) -> None:
        # Blocks here when max_workers runs are already going, which is what
        # keeps concurrent browsers bounded.
        self._slots.acquire()
        job.status = RUNNING
        job.started_at = _now()
        logger.info("Job %s running for %s", job.id, job.report_id)

        try:
            job.result = work()
            job.status = SUCCEEDED
        except Exception as exc:
            # The work callable is responsible for recording the failure
            # against the ticket; this only keeps the job record honest.
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Job %s failed for %s", job.id, job.report_id)
        finally:
            job.finished_at = _now()
            self._slots.release()

    def get(self, report_id: str) -> Optional[RunJob]:
        with self._lock:
            return self._jobs.get(report_id)

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.as_dict() for j in jobs]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ACTIVE_STATES)


def _max_workers() -> int:
    try:
        return max(1, int(os.getenv("PIPELINE_MAX_WORKERS", "2")))
    except ValueError:
        return 2


JOBS = JobQueue(max_workers=_max_workers())
