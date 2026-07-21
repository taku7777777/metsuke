from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from metsuke import config, ingest, ledger
from metsuke.redaction import REDACTION_VERSION

from . import v1_period, v2_trend, v3_cache, v4_dist
from .render import shell
from .window import Window, data_max_date

VIEWS = {
    "dist": v4_dist.build,
    "period": v1_period.build,
    "cache": v3_cache.build,
    "trend": v2_trend.build,
}


def _purge_old() -> None:
    directory = config.views_dir()
    if not directory.exists():
        return
    for path in directory.glob("*.html"):
        try:
            match = re.search(r"redaction_version(?:=|\":)(\d+)", path.read_text(errors="ignore"))
            if match is None or int(match.group(1)) < REDACTION_VERSION:
                path.unlink()
        except OSError:
            path.unlink(missing_ok=True)


def _record_generation(name: str, window: Window) -> None:
    spool = config.hooks_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    os.chmod(spool, config.DIR_MODE)
    path = spool / f"view-{time.time_ns()}-{os.getpid()}.ndjson"
    payload = {
        "metsuke_event": "view_html_generated",
        "metsuke_ts": time.time(),
        "payload": {
            "view": name,
            "days": (window.end - window.start).days + 1,
            "project": window.project,
        },
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def generate(name: str, window: Window, *, conn=None) -> Path | None:
    if name not in VIEWS:
        return None
    owned = conn is None
    if owned:
        try:
            conn = ledger.connect_readonly()
        except sqlite3.OperationalError as exc:
            raise RuntimeError("ledgerを開けません。metsuke sync を先に実行してください") from exc
    try:
        try:
            title, period, total, body = VIEWS[name](conn, window)
            maximum = data_max_date(conn)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such view" in str(exc):
                raise RuntimeError(
                    "必要なDBビューがありません。metsuke sync を先に実行してください"
                ) from exc
            raise
    finally:
        if owned:
            conn.close()
    generated = dt.datetime.now(dt.UTC).isoformat()
    freshness = f" · データ最終: {maximum.isoformat()}" if maximum and maximum < window.end else ""
    stamp = (
        f"redaction_version={REDACTION_VERSION} parser_version={ingest.PARSER_VERSION} "
        f"generated={generated}{freshness}"
    )
    document = shell(title=title, period=period, total=total, body=body, stamp=stamp)
    directory = config.views_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, config.DIR_MODE)
    _purge_old()
    target = directory / f"{name}.html"
    tmp = directory / f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(document)
    os.replace(tmp, target)
    os.chmod(target, config.FILE_MODE)
    _record_generation(name, window)
    return target
