"""ledger.db — SQLite canonical store. Facts only; money is derived in views.sql."""

from __future__ import annotations

import json
import os
import sqlite3
from importlib import resources
from pathlib import Path

from . import config


def db_path() -> Path:
    return config.home() / "ledger.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    p = path or db_path()
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(resources.files("metsuke").joinpath("schema.sql").read_text())
    _migrate_indexes(conn)
    try:
        conn.execute("ALTER TABLE prompt ADD COLUMN task_label TEXT")
    except sqlite3.OperationalError:
        pass
    for column, kind in (
        ("file_path", "TEXT"),
        ("lines_changed", "INTEGER"),
        ("result_ts", "REAL"),
        ("workflow_run_id", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE tool_call ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE marker ADD COLUMN saving_usd REAL")
    except sqlite3.OperationalError:
        pass
    for column, kind in (
        ("saving_low_usd", "REAL"),
        ("saving_high_usd", "REAL"),
        ("saving_basis", "TEXT"),
        ("verdict_note", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE marker ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError:
            pass
    for column, kind in (
        ("outcome", "TEXT"),
        ("outcome_reason", "TEXT"),
        ("observed_json", "TEXT"),
        ("experiment_group", "TEXT NOT NULL DEFAULT 'treatment'"),
    ):
        try:
            conn.execute(f"ALTER TABLE nudge ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError:
            pass
    for column, kind in (
        ("query_source", "TEXT"),
        ("effort", "TEXT"),
        ("cost_usd_sdk", "REAL"),
        ("end_ts", "REAL"),
        ("api_duration_ms", "REAL"),
    ):
        try:
            conn.execute(f"ALTER TABLE request ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE agent ADD COLUMN workflow_run_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE price ADD COLUMN source_url TEXT")
    except sqlite3.OperationalError:
        pass
    _seed_prices(conn)
    conn.executescript(resources.files("metsuke").joinpath("views.sql").read_text())
    os.chmod(p, config.FILE_MODE)
    return conn


def _migrate_indexes(conn: sqlite3.Connection) -> None:
    """Idempotently bring pre-existing ledgers to the current index set."""

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_session_ts ON request(session_id, ts)"
    )


def connect_readonly(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_prices(conn: sqlite3.Connection) -> None:
    data = json.loads(resources.files("metsuke").joinpath("prices.json").read_text())
    d = data["defaults"]
    source_url = data.get("source_url")
    _validate_price_ranges(data["models"], "model")
    _validate_price_ranges(data.get("server_tools", []), "tool")
    legacy_cleanup = conn.execute(
        "SELECT 1 FROM meta WHERE key='legacy_price_cleanup_v1'"
    ).fetchone()
    if legacy_cleanup is None:
        # This exact row was bundled before price rows carried a source marker.
        # Remove it once; source-less rows created later remain operator-owned.
        conn.execute(
            """DELETE FROM price
               WHERE model='claude-sonnet-5' AND valid_from='2025-01-01'
                 AND valid_to IS NULL AND in_usd=3 AND out_usd=15
                 AND fast_x=2 AND source_url IS NULL"""
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES ('legacy_price_cleanup_v1','done')"
        )
    previous = conn.execute(
        "SELECT value FROM meta WHERE key='bundled_price_version'"
    ).fetchone()
    previous_source = conn.execute(
        "SELECT value FROM meta WHERE key='bundled_price_source_url'"
    ).fetchone()
    models = sorted({row["model"] for row in data["models"]})
    if previous is None and models:
        # Pre-version ledgers contain only the old bundled seed for known models.
        # Remove those rows once so obsolete periods cannot overlap the SCD2 data.
        placeholders = ",".join("?" for _ in models)
        conn.execute(
            f"DELETE FROM price WHERE source_url IS NULL AND model IN ({placeholders})",
            models,
        )
    else:
        # The bundled file is the SSOT for rows carrying its source marker. This
        # also removes periods retired by a later bundled version.
        bundled_sources = {
            value
            for value in (previous_source[0] if previous_source else None, source_url)
            if value
        }
        for bundled_source in bundled_sources:
            conn.execute("DELETE FROM price WHERE source_url=?", (bundled_source,))
            conn.execute(
                "DELETE FROM price_server_tool WHERE source_url=?", (bundled_source,)
            )
    for m in data["models"]:
        conn.execute(
            """INSERT INTO price
               (model, valid_from, valid_to, in_usd, out_usd,
                cache_read_x, cache_w5m_x, cache_w1h_x, batch_x, fast_x, geo_us_x,
                source_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(model,valid_from) DO UPDATE SET
                 valid_to=excluded.valid_to,
                 in_usd=excluded.in_usd,
                 out_usd=excluded.out_usd,
                 cache_read_x=excluded.cache_read_x,
                 cache_w5m_x=excluded.cache_w5m_x,
                 cache_w1h_x=excluded.cache_w1h_x,
                 batch_x=excluded.batch_x,
                 fast_x=excluded.fast_x,
                 geo_us_x=excluded.geo_us_x,
                 source_url=excluded.source_url""",
            (
                m["model"],
                m["valid_from"],
                m.get("valid_to"),
                m["in_usd"],
                m["out_usd"],
                m.get("cache_read_x", d["cache_read_x"]),
                m.get("cache_w5m_x", d["cache_w5m_x"]),
                m.get("cache_w1h_x", d["cache_w1h_x"]),
                m.get("batch_x", d["batch_x"]),
                m.get("fast_x", d["fast_x"]),
                m.get("geo_us_x", d["geo_us_x"]),
                m.get("source_url", source_url),
            ),
        )
    for tool in data.get("server_tools", []):
        conn.execute(
            """INSERT INTO price_server_tool
               (tool,valid_from,valid_to,usd_per_unit,source_url)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tool,valid_from) DO UPDATE SET
                 valid_to=excluded.valid_to,
                 usd_per_unit=excluded.usd_per_unit,
                 source_url=excluded.source_url""",
            (
                tool["tool"], tool["valid_from"], tool.get("valid_to"),
                tool["usd_per_unit"], tool.get("source_url", source_url),
            ),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('bundled_price_version',?)",
        (str(data.get("version", "unknown")),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('bundled_price_source_url',?)",
        (source_url or "",),
    )
    conn.commit()


def _validate_price_ranges(rows: list[dict], identity: str) -> None:
    """Reject ambiguous bundled SCD2 data before it can duplicate cost rows."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = row[identity]
        start = row["valid_from"]
        end = row.get("valid_to")
        if end is not None and end <= start:
            raise ValueError(f"invalid price range for {key}: {start}..{end}")
        grouped.setdefault(key, []).append(row)
    for key, periods in grouped.items():
        periods.sort(key=lambda row: row["valid_from"])
        for left, right in zip(periods, periods[1:]):
            if left.get("valid_to") is None or left["valid_to"] > right["valid_from"]:
                raise ValueError(f"overlapping price ranges for {key}")


def known_models(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT model FROM price")}
