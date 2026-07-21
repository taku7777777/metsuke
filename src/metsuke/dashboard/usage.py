"""PII-free local usage events for dashboard rollout decisions."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .. import config

VIEW_EVENT_NAME = "dashboard_view_opened"
TRACE_EVENT_NAME = "dashboard_trace_opened"
ALLOWED_VIEWS = frozenset({"overview", "period", "trend", "cache", "dist"})
ALLOWED_LAUNCH_METHODS = frozenset({"dashboard_server"})
PAYLOAD_KEYS = frozenset(
    {"view", "result", "duration_ms", "launch_method", "trace_cache"}
)


def record_view_opened(
    spool: Path,
    *,
    view: str,
    duration_ms: float,
    launch_method: str,
) -> None:
    """Write one allowlisted event; the API cannot accept IDs, text, or filters."""

    if view not in ALLOWED_VIEWS or launch_method not in ALLOWED_LAUNCH_METHODS:
        raise ValueError("dashboard usage value is not allowlisted")
    spool.mkdir(parents=True, exist_ok=True)
    os.chmod(spool, config.DIR_MODE)
    payload = {
        "metsuke_event": VIEW_EVENT_NAME,
        "metsuke_ts": time.time(),
        "payload": {
            "view": view,
            "result": "success",
            "duration_ms": round(max(0.0, duration_ms), 3),
            "launch_method": launch_method,
            "trace_cache": "not_applicable",
        },
    }
    if set(payload["payload"]) != PAYLOAD_KEYS:
        raise AssertionError("dashboard usage schema changed")
    path = spool / f"dashboard-{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.ndjson"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_trace_opened(
    spool: Path,
    *,
    cache_result: str,
    launch_method: str,
) -> None:
    """Record a trace choice through an API that cannot accept any PII field."""

    if cache_result not in {"hit", "miss"} or launch_method not in ALLOWED_LAUNCH_METHODS:
        raise ValueError("dashboard usage value is not allowlisted")
    spool.mkdir(parents=True, exist_ok=True)
    os.chmod(spool, config.DIR_MODE)
    payload = {
        "metsuke_event": TRACE_EVENT_NAME,
        "metsuke_ts": time.time(),
        "payload": {
            "result": "selected",
            "launch_method": launch_method,
            "trace_cache": cache_result,
        },
    }
    if set(payload["payload"]) != {"result", "launch_method", "trace_cache"}:
        raise AssertionError("dashboard trace usage schema changed")
    path = spool / f"dashboard-{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.ndjson"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
