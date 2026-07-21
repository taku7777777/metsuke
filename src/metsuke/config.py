"""Paths and constants. Data lives under METSUKE_HOME (default ~/.metsuke), code in this repo.

Layout:
  ~/.metsuke/
    archive/segments/YYYY-MM/   raw byte segments (zstd), append-only, permanent
    archive/manifest.jsonl      one line per segment (sha256, provenance), append-only
    state/                      cursors, locks, last-run info (disposable)
    logs/
"""

from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600
CONFIG_KEYS = (
    "METSUKE_HOME",
    "METSUKE_SOURCE",
    "METSUKE_BUDGET_DAY",
    "METSUKE_BUDGET_WEEK",
    "METSUKE_BUDGET_MONTH",
    "METSUKE_BUDGET_WARN_ENABLED",
    "METSUKE_BURN_WINDOW_S",
    "METSUKE_BURN_WARN_USD_H",
    "METSUKE_BURN_CRIT_USD_H",
    "METSUKE_PROMPT_WARN_USD",
    "METSUKE_PROMPT_CRIT_USD",
    "METSUKE_CONTEXT_WARN_TOKENS",
    "METSUKE_CONTEXT_CRIT_TOKENS",
    "METSUKE_RECEIPT_NOTIFY_ENABLED",
    "METSUKE_RUNAWAY_USD",
    "METSUKE_COLDCACHE_MIN_USD",
    "METSUKE_TTL_PRENOTIFY_GAP_S",
    "METSUKE_NUDGE_DAILY_CAP",
    "METSUKE_HOURLY_VALUE_USD",
    "METSUKE_DASHBOARD_PORT",
    "METSUKE_OTEL_PORT",
    "METSUKE_TRACE_CACHE_MAX_MB",
    "METSUKE_TRACE_CACHE_MAX_AGE_DAYS",
    "METSUKE_RESTIC_REPO",
)


def config_path() -> Path:
    """Stable config location; it must not depend on METSUKE_HOME."""
    return Path(
        os.environ.get(
            "METSUKE_CONFIG", str(Path.home() / ".metsuke" / "config.env")
        )
    )


def file_settings() -> dict[str, str]:
    try:
        lines = config_path().read_text().splitlines()
    except FileNotFoundError:
        return {}
    result = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid config line {number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in CONFIG_KEYS:
            raise ValueError(f"invalid config line {number}: unknown key {key}")
        result[key] = value.strip()
    return result


def value(name: str, default: str | int | float | None = None) -> str | None:
    if name not in CONFIG_KEYS:
        raise KeyError(name)
    if name in os.environ:
        return os.environ[name]
    return file_settings().get(name, None if default is None else str(default))


def _float_env(name: str, default: float) -> float:
    try:
        return float(value(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(value(name, default))
    except (TypeError, ValueError):
        return default


def float_value(name: str, default: float) -> float:
    return _float_env(name, default)


def int_value(name: str, default: int) -> int:
    return _int_env(name, default)


def optional_float_value(name: str, default: float | None = None) -> float | None:
    raw = value(name, default)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


BUDGET_DAY = optional_float_value("METSUKE_BUDGET_DAY")
BUDGET_WEEK = optional_float_value("METSUKE_BUDGET_WEEK")
BUDGET_MONTH = optional_float_value("METSUKE_BUDGET_MONTH")
BURN_WINDOW_S = _int_env("METSUKE_BURN_WINDOW_S", 1800)
BURN_WARN_USD_H = _float_env("METSUKE_BURN_WARN_USD_H", 45)
BURN_CRIT_USD_H = _float_env("METSUKE_BURN_CRIT_USD_H", 90)
PROMPT_WARN_USD = _float_env("METSUKE_PROMPT_WARN_USD", 3.0)
PROMPT_CRIT_USD = _float_env("METSUKE_PROMPT_CRIT_USD", 7.5)
CONTEXT_WARN_TOKENS = _int_env("METSUKE_CONTEXT_WARN_TOKENS", 200_000)
CONTEXT_CRIT_TOKENS = _int_env("METSUKE_CONTEXT_CRIT_TOKENS", 500_000)
RUNAWAY_USD = _float_env("METSUKE_RUNAWAY_USD", 5.0)
COLDCACHE_MIN_USD = _float_env("METSUKE_COLDCACHE_MIN_USD", 0.5)
TTL_PRENOTIFY_GAP_S = _int_env("METSUKE_TTL_PRENOTIFY_GAP_S", 3000)
NUDGE_DAILY_CAP = _int_env("METSUKE_NUDGE_DAILY_CAP", 3)
HOURLY_VALUE_USD = _float_env("METSUKE_HOURLY_VALUE_USD", 0)


def home() -> Path:
    return Path(value("METSUKE_HOME", str(Path.home() / ".metsuke")))


def source_dir() -> Path:
    return Path(value("METSUKE_SOURCE", str(Path.home() / ".claude" / "projects")))


def archive_dir() -> Path:
    return home() / "archive"


def segments_dir() -> Path:
    return archive_dir() / "segments"


def manifest_path() -> Path:
    return archive_dir() / "manifest.jsonl"


def state_dir() -> Path:
    return home() / "state"


def state_json_path() -> Path:
    return home() / "state.json"


def dashboard_port() -> int:
    port = int_value("METSUKE_DASHBOARD_PORT", 48127)
    return port if 1 <= port <= 65535 else 48127


def dashboard_state_path() -> Path:
    return state_dir() / "dashboard-state.json"


def trace_cache_manifest_path() -> Path:
    return state_dir() / "trace-cache.json"


def trace_cache_max_bytes() -> int:
    value_mb = int_value("METSUKE_TRACE_CACHE_MAX_MB", 256)
    return max(1, value_mb) * 1024 * 1024


def trace_cache_max_age_days() -> int:
    return max(1, int_value("METSUKE_TRACE_CACHE_MAX_AGE_DAYS", 30))


def hooks_spool_dir() -> Path:
    return home() / "spool" / "hooks"


def proposals_dir() -> Path:
    return home() / "spool" / "proposals"


def reports_dir() -> Path:
    return home() / "reports"


def traces_dir() -> Path:
    return home() / "traces"


def views_dir() -> Path:
    return home() / "views"


def otel_dir() -> Path:
    return home() / "otel"


def logs_dir() -> Path:
    return home() / "logs"


def cursors_path() -> Path:
    return state_dir() / "archiver_cursors.json"


def last_run_path() -> Path:
    return state_dir() / "archiver_last_run.json"


def lock_path() -> Path:
    return state_dir() / "archiver.lock"


def active_task_path() -> Path:
    return state_dir() / "active-task"


def ntfy_url_path() -> Path:
    return state_dir() / "ntfy.url"


def sync_lock_path() -> Path:
    return state_dir() / "sync.lock"


def last_sync_error_path() -> Path:
    return state_dir() / "last_sync_error.json"


def handoffs_dir() -> Path:
    return home() / "handoffs"


def ensure_dirs() -> None:
    for d in (
        home(), archive_dir(), segments_dir(), state_dir(), logs_dir(), otel_dir(),
        hooks_spool_dir(), proposals_dir(), reports_dir(), traces_dir(), views_dir(),
        handoffs_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, DIR_MODE)
