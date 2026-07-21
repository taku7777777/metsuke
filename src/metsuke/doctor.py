"""Operational diagnosis: every silent-failure boundary in one command."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config, ledger
from .dashboard import launcher
from .dashboard import server as dashboard_server
from .dashboard.trace_cache import MANIFEST_SCHEMA_VERSION, TraceCache

ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}
# Kept in step with scripts/install-app.sh, which generates the bundle.
APP_BUNDLE_NAME = "Metsuke.app"
APP_EXECUTABLE_NAME = "Metsuke"
APP_TARGET_MARKER = "# metsuke-target: "
CORE_LABELS = (
    "com.metsuke.archiver",
    "com.metsuke.tick",
    "com.metsuke.analyst",
    "com.metsuke.deadman",
    "com.metsuke.otelcol",
)
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "PreCompact",
    "PostCompact",
    "PostToolUse",
    "Notification",
)


def _item(items: list[dict], name: str, status: str, value, detail: str = "") -> None:
    items.append(
        {"check_name": name, "status": status, "value": str(value), "detail": detail}
    )


def _launchd(items: list[dict]) -> None:
    uid = os.getuid()
    labels = list(CORE_LABELS)
    if config.value("METSUKE_RESTIC_REPO"):
        labels.append("com.metsuke.backup")
    for label in labels:
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = (
                "ok"
                if result.returncode == 0
                else ("warn" if label == "com.metsuke.otelcol" else "fail")
            )
            value = "loaded" if result.returncode == 0 else "not loaded"
        except (OSError, subprocess.TimeoutExpired):
            status, value = "warn", "unable to check"
        _item(items, f"launchd:{label}", status, value)


def _state(items: list[dict]) -> None:
    try:
        state = json.loads(config.state_json_path().read_text())
        generated = state["generated_at"]
        age = time.time() - float(generated)
        reasons = state.get("health", {}).get("stale_reasons") or []
        status = "ok" if age < 900 and not state.get("stale") else "fail"
        _item(
            items, "state_freshness", status, f"{age:.0f}s",
            ",".join(reasons),
        )
    except (OSError, ValueError, KeyError, TypeError):
        _item(items, "state_freshness", "fail", "missing or invalid")


def _health(items: list[dict]) -> None:
    if not ledger.db_path().exists():
        _item(items, "v_health", "fail", "ledger missing")
        return
    try:
        conn = ledger.connect_readonly()
        rows = conn.execute(
            "SELECT check_name,status,value,detail FROM v_health WHERE status IN ('warn','fail')"
        ).fetchall()
        conn.close()
        for row in rows:
            _item(items, f"health:{row['check_name']}", row["status"], row["value"], row["detail"])
    except Exception as exc:  # diagnosis must report a broken ledger, not crash
        _item(items, "v_health", "fail", "query failed", str(exc))


def _spool(items: list[dict]) -> None:
    count = len(list(config.hooks_spool_dir().glob("*.ndjson")))
    _item(items, "hook_spool", "ok" if count < 100 else "warn", count, "pending files")


def _backup(items: list[dict]) -> None:
    if not config.value("METSUKE_RESTIC_REPO"):
        _item(items, "restic_backup", "skip", "disabled", "METSUKE_RESTIC_REPO is unset")
        return
    marker = config.state_dir() / "last_backup.json"
    try:
        age_h = (time.time() - json.loads(marker.read_text())["ts"]) / 3600
        status = "ok" if age_h < 26 else ("warn" if age_h < 50 else "fail")
        _item(items, "restic_backup", status, f"{age_h:.1f}h ago")
    except (OSError, ValueError, KeyError, TypeError):
        _item(items, "restic_backup", "warn", "not running")


def _settings(items: list[dict]) -> None:
    path = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text())
        statusline = "statusline.sh" in str((data.get("statusLine") or {}).get("command", ""))
        hooks = data.get("hooks") or {}
        missing = []
        for event in HOOK_EVENTS:
            commands = [
                hook.get("command", "")
                for group in hooks.get(event, [])
                for hook in group.get("hooks", [])
            ]
            if not any("hook-sensor.sh" in command and event in command for command in commands):
                missing.append(event)
        if not statusline:
            missing.append("statusLine")
        _item(
            items,
            "claude_hooks",
            "ok" if not missing else "fail",
            "installed" if not missing else "missing: " + ",".join(missing),
        )
        legacy = sorted(set((data.get("env") or {})) & set(config.CONFIG_KEYS))
        _item(
            items,
            "config_single_source",
            "ok" if not legacy else "warn",
            "central" if not legacy else "legacy Claude env: " + ",".join(legacy),
        )
        otel = data.get("env") or {}
        required_otel = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": (
                f"http://localhost:{config.int_value('METSUKE_OTEL_PORT', 4319)}"
            ),
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }
        missing_otel = [
            key for key, expected in required_otel.items() if otel.get(key) != expected
        ]
        _item(
            items,
            "claude_otel_env",
            "ok" if not missing_otel else "warn",
            "configured" if not missing_otel else "missing: " + ",".join(missing_otel),
        )
    except (OSError, ValueError, TypeError):
        _item(items, "claude_hooks", "warn", "settings unavailable")


def _config(items: list[dict]) -> None:
    path = config.config_path()
    try:
        values = config.file_settings()
        mode = path.stat().st_mode & 0o777
        missing = [key for key in ("METSUKE_HOME", "METSUKE_SOURCE") if not values.get(key)]
        status = "ok" if mode == 0o600 and not missing else "fail"
        detail = []
        if mode != 0o600:
            detail.append(f"mode={mode:o}, expected=600")
        if missing:
            detail.append("missing=" + ",".join(missing))
        _item(items, "central_config", status, path, "; ".join(detail))
    except FileNotFoundError:
        _item(items, "central_config", "warn", "missing", str(path))
    except (OSError, ValueError) as exc:
        _item(items, "central_config", "fail", "invalid", str(exc))


def _manifest(items: list[dict]) -> None:
    path = config.manifest_path()
    if not path.exists():
        _item(items, "archive_manifest", "fail", "missing")
        return
    try:
        lines = sum(1 for line in path.open() if line.strip())
        conn = ledger.connect_readonly()
        row = conn.execute("SELECT value FROM meta WHERE key='manifest_pos'").fetchone()
        conn.close()
        pos = int(row[0]) if row else 0
        status = "ok" if pos <= lines else "fail"
        _item(items, "archive_manifest", status, f"position {pos}/{lines}")
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        _item(items, "archive_manifest", "fail", "invalid", str(exc))


def _disk(items: list[dict]) -> None:
    try:
        free_gb = shutil.disk_usage(config.home()).free / 1024**3
        _item(items, "disk_free", "ok" if free_gb > 10 else "fail", f"{free_gb:.1f}GB")
    except OSError as exc:
        _item(items, "disk_free", "warn", "unable to check", str(exc))


def _dashboard(items: list[dict]) -> None:
    """Report the dashboard lifecycle without ever starting a server."""

    state_path = config.dashboard_state_path()
    try:
        status = dashboard_server.server_status(state_path)
    except OSError as exc:
        _item(items, "dashboard_server", "warn", "unable to check", type(exc).__name__)
        return
    port = status.state.port if status.state is not None else "-"
    if status.running:
        _item(items, "dashboard_server", "ok", f"running on port {port}")
    elif status.stale:
        _item(
            items,
            "dashboard_server",
            "warn",
            f"stale state (port {port})",
            (
                "a server is still answering on this port; metsuke dashboard stop ends it"
                if status.serving
                else "no healthy server owns this state; metsuke dashboard open recovers it"
            ),
        )
    else:
        _item(items, "dashboard_server", "ok", "stopped", "start with metsuke dashboard open")

    secret = launcher.secret_path_for(state_path)
    try:
        mode = secret.stat().st_mode & 0o777
    except FileNotFoundError:
        _item(
            items,
            "dashboard_auth_secret",
            "skip",
            "absent",
            "created when the dashboard first starts",
        )
    except OSError as exc:
        _item(items, "dashboard_auth_secret", "warn", "unable to check", type(exc).__name__)
    else:
        _item(
            items,
            "dashboard_auth_secret",
            "ok" if mode == config.FILE_MODE else "fail",
            f"mode {mode:o}",
            "" if mode == config.FILE_MODE else f"expected {config.FILE_MODE:o}",
        )


def _app(items: list[dict]) -> None:
    """A moved or renamed checkout leaves the bundle pointing at nothing."""

    app = Path.home() / "Applications" / APP_BUNDLE_NAME
    executable = app / "Contents" / "MacOS" / APP_EXECUTABLE_NAME
    if not executable.is_file():
        _item(
            items,
            "metsuke_app",
            "warn",
            "missing",
            f"{app} is absent; re-run scripts/install.sh",
        )
        return
    try:
        script = executable.read_text()
    except (OSError, UnicodeError) as exc:
        _item(items, "metsuke_app", "fail", "unreadable", type(exc).__name__)
        return
    target = ""
    for line in script.splitlines():
        if line.startswith(APP_TARGET_MARKER):
            target = line[len(APP_TARGET_MARKER) :].strip()
            break
    if not target:
        _item(items, "metsuke_app", "fail", "launcher has no target", "re-run scripts/install.sh")
    elif not Path(target).exists():
        _item(
            items,
            "metsuke_app",
            "fail",
            "launcher target missing",
            f"{target} no longer exists; re-run scripts/install.sh from the checkout",
        )
    else:
        _item(items, "metsuke_app", "ok", target)


def _trace_cache(items: list[dict]) -> None:
    manifest_path = config.trace_cache_manifest_path()
    try:
        raw = json.loads(manifest_path.read_text())
        version = raw["schema_version"] if isinstance(raw, dict) else None
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if version != MANIFEST_SCHEMA_VERSION or not isinstance(entries, dict):
            _item(
                items,
                "trace_cache_manifest",
                "warn",
                "unusable",
                "rebuilt from the cached HTML on next use",
            )
        else:
            _item(items, "trace_cache_manifest", "ok", f"{len(entries)} tracked")
    except FileNotFoundError:
        _item(items, "trace_cache_manifest", "skip", "absent", "no trace has been cached yet")
    except (OSError, ValueError, KeyError, TypeError):
        _item(
            items,
            "trace_cache_manifest",
            "warn",
            "unusable",
            "rebuilt from the cached HTML on next use",
        )
    try:
        stats = TraceCache(config.traces_dir(), manifest_path).stats()
        oldest = (
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stats.oldest_access))
            if stats.oldest_access is not None
            else "none"
        )
        _item(items, "trace_cache_count", "ok", stats.count, "derived HTML files")
        _item(items, "trace_cache_bytes", "ok", stats.total_bytes)
        _item(items, "trace_cache_oldest_access", "ok", oldest)
        _item(
            items,
            "trace_cache_purge_failures",
            "warn" if stats.purge_failures else "ok",
            stats.purge_failures,
        )
    except (OSError, ValueError) as exc:
        _item(items, "trace_cache", "warn", "unavailable", type(exc).__name__)


def run(as_json: bool = False) -> int:
    items: list[dict] = []
    _launchd(items)
    _state(items)
    _health(items)
    _spool(items)
    _backup(items)
    _config(items)
    _settings(items)
    _manifest(items)
    _dashboard(items)
    _app(items)
    _trace_cache(items)
    _disk(items)
    if as_json:
        print(json.dumps(items, ensure_ascii=False))
    else:
        for item in items:
            detail = f" — {item['detail']}" if item["detail"] else ""
            print(
                f"{ICONS[item['status']]} {item['check_name']}: {item['value']}{detail}"
            )
    return 1 if any(item["status"] == "fail" for item in items) else 0
