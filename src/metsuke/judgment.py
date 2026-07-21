"""Append human/approved-AI judgments to the rebuildable archive intake spool."""

from __future__ import annotations

import json
import os
import time

from . import config


def record(kind: str, payload: dict, *, ts: float | None = None) -> float:
    """Write one judgment envelope and return its event timestamp."""
    fired = time.time() if ts is None else float(ts)
    body = {
        "metsuke_event": "judgment",
        "metsuke_ts": fired,
        "payload": {"kind": kind, **payload},
    }
    spool = config.hooks_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    ns = time.time_ns()
    path = spool / f"{ns}-{os.getpid()}-judgment-{kind}.ndjson"
    tmp = spool / f".tmp-{ns}-{os.getpid()}-judgment-{kind}"
    tmp.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(tmp, config.FILE_MODE)
    os.replace(tmp, path)
    return fired
