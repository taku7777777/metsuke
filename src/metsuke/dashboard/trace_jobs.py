"""In-memory, bounded trace generation jobs for the dashboard."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from .. import trace_html
from .db import connect_dashboard
from .trace_cache import TraceCache, TraceFingerprint, current_fingerprint

MAX_CONCURRENT_JOBS = 2
MAX_CONCURRENT_PER_SESSION = 1
JOB_TIMEOUT_SECONDS = 30.0


class TraceJobError(RuntimeError):
    """A path-free trace job error suitable for conversion to an HTTP response."""


class TraceSessionNotFoundError(TraceJobError):
    pass


@dataclass(frozen=True)
class TraceJob:
    job_id: str
    status: str
    cache_result: str
    created_at: float
    updated_at: float
    error: str | None = None


Generator = Callable[[str, object], Path | None]
Opener = Callable[[Path, str], bool]


class TraceJobManager:
    """Deduplicate per session and run at most two trace generations globally."""

    def __init__(
        self,
        database_path: Path,
        cache: TraceCache,
        *,
        generator: Generator | None = None,
        opener: Opener = trace_html.open_browser,
        clock: Callable[[], float] = time.time,
        timeout: float = JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.database_path = database_path
        self.cache = cache
        self.generator = generator or self._generate
        self.opener = opener
        self.clock = clock
        self.timeout = timeout
        self._condition = threading.Condition()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
        self._jobs: dict[str, TraceJob] = {}
        self._active_sessions: dict[str, str] = {}
        self._threads: set[threading.Thread] = set()
        self._stopping = False

    def _generate(self, session_id: str, conn: object) -> Path | None:
        # Passing the request-local query-only connection is intentional: trace
        # generation may write its derived HTML cache, but never the ledger DB.
        return trace_html.generate(
            session_id,
            conn=conn,
            record=False,
            purge=False,
            directory=self.cache.directory,
        )

    def _fingerprint(self, session_id: str) -> TraceFingerprint:
        with closing(connect_dashboard(self.database_path)) as conn:
            fingerprint = current_fingerprint(conn, session_id)
        if fingerprint is None:
            raise TraceSessionNotFoundError("trace session was not found")
        return fingerprint

    def _new_job(self, status: str, cache_result: str) -> TraceJob:
        import secrets

        now = self.clock()
        return TraceJob(secrets.token_urlsafe(24), status, cache_result, now, now)

    def submit(self, session_id: str, *, fragment: str = "") -> TraceJob:
        fingerprint = self._fingerprint(session_id)
        with self._condition:
            if self._stopping:
                raise TraceJobError("trace jobs are stopping")
            existing_id = self._active_sessions.get(session_id)
            if existing_id is not None:
                return self._jobs[existing_id]
            cached = self.cache.lookup(fingerprint)
            if cached is not None:
                job = self._new_job("ready", "hit")
                self._jobs[job.job_id] = job
                with self.cache.protect(cached):
                    self._open(cached, fragment)
                return job
            job = self._new_job("queued", "miss")
            self._jobs[job.job_id] = job
            self._active_sessions[session_id] = job.job_id
            worker = threading.Thread(
                target=self._run,
                args=(job.job_id, session_id, fingerprint, fragment),
                name=f"metsuke-trace-{job.job_id[:8]}",
                daemon=True,
            )
            self._threads.add(worker)
            worker.start()
            return job

    def _open(self, path: Path, fragment: str) -> None:
        try:
            self.opener(path, fragment)
        except Exception:
            # Browser launch is a convenience after a completed generation. Its
            # failure must not invalidate an otherwise reusable trace artifact.
            pass

    def _set_status(self, job_id: str, status: str, error: str | None = None) -> bool:
        with self._condition:
            job = self._jobs[job_id]
            if job.status == "failed" and status == "ready":
                return False
            self._jobs[job_id] = replace(
                job,
                status=status,
                updated_at=self.clock(),
                error=error,
            )
            self._condition.notify_all()
            return True

    def _run(
        self,
        job_id: str,
        session_id: str,
        fingerprint: TraceFingerprint,
        fragment: str,
    ) -> None:
        timer: threading.Timer | None = None
        try:
            self._slots.acquire()
            self._set_status(job_id, "running")
            timer = threading.Timer(
                self.timeout,
                self._set_status,
                args=(job_id, "failed", "trace generation timed out"),
            )
            timer.daemon = True
            timer.start()
            target = self.cache.path_for(session_id)
            with self.cache.protect(target):
                with closing(connect_dashboard(self.database_path)) as conn:
                    generated = self.generator(session_id, conn)
                if generated is None:
                    raise TraceSessionNotFoundError("trace session was not found")
                if generated.resolve() != target.resolve():
                    raise TraceJobError("trace generator returned an unexpected target")
                self.cache.register(fingerprint, generated)
                self.cache.purge()
                if self._set_status(job_id, "ready"):
                    self._open(generated, fragment)
        except Exception as exc:
            self._set_status(job_id, "failed", type(exc).__name__)
        finally:
            if timer is not None:
                timer.cancel()
            self._slots.release()
            with self._condition:
                if self._active_sessions.get(session_id) == job_id:
                    self._active_sessions.pop(session_id, None)
                self._threads.discard(threading.current_thread())
                self._condition.notify_all()

    def get(self, job_id: str) -> TraceJob | None:
        with self._condition:
            return self._jobs.get(job_id)

    def shutdown(self, timeout: float = JOB_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            self._stopping = True
            while self._threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
