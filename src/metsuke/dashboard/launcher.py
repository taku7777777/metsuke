"""Reuse-or-start entry point behind ``metsuke dashboard open`` and Metsuke.app.

The bootstrap nonce is stateless-signed from the per-install secret plus the
running server's ``server_instance_id``; the server keeps only a used-set for
replay defense. A launcher that can read the 0600 secret file is already the
local user, so it mints its own nonce instead of asking a running server for
one. This module therefore never opens a socket itself: liveness is decided by
``server.server_status``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .auth import AuthManager, DashboardAuthError, load_or_create_secret
from .server import (
    LOOPBACK_HOST,
    DashboardServerError,
    ServerState,
    _health_is_ok,
    _read_state,
    server_status,
)

STARTUP_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.05
# The child re-raises nothing: every dashboard failure becomes one stderr line so
# that a Dock launch leaves an explanation instead of a traceback in the log.
_CHILD_SCRIPT = """
import sys
from pathlib import Path

from metsuke.dashboard import server

state_path = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
try:
    server.serve(state_path=state_path)
except server.DashboardServerError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
"""
_SYNC_SCRIPT = """
from metsuke import cli

raise SystemExit(cli.main(["sync"]))
"""


class DashboardLaunchError(RuntimeError):
    """The dashboard could not be opened; the message is safe to show a user."""


@dataclass(frozen=True)
class LaunchResult:
    url: str
    port: int
    reused: bool


def secret_path_for(state_path: Path) -> Path:
    """Mirror ``create_server``'s secret location so minted nonces validate."""

    return state_path.with_name("dashboard-secret")


def log_path_for(state_path: Path) -> Path:
    return state_path.with_name("dashboard-errors.log")


def mint_bootstrap_nonce(state_path: Path, server_instance_id: str) -> str:
    try:
        secret = load_or_create_secret(secret_path_for(state_path))
    except DashboardAuthError as exc:
        raise DashboardLaunchError(
            "dashboard authentication is unavailable; run metsuke doctor"
        ) from exc
    return AuthManager(secret, server_instance_id).issue_bootstrap_nonce()


def bootstrap_url(state_path: Path, state: ServerState) -> str:
    """Mint a nonce for ``state`` and confirm that instance is still the live one.

    Minting reads the secret file, which can be slow enough for a restart to slip
    in between. Re-reading the state afterwards turns that race into one sentence
    instead of an opaque 401 in the browser.
    """

    nonce = mint_bootstrap_nonce(state_path, state.server_instance_id)
    current = _read_state(state_path)
    if current is None or current.server_instance_id != state.server_instance_id:
        raise DashboardLaunchError(
            "the dashboard server restarted while opening; run metsuke dashboard open again"
        )
    return f"http://{LOOPBACK_HOST}:{state.port}/bootstrap?nonce={nonce}"


def start_server_process(state_path: Path | None, log_path: Path) -> subprocess.Popen:
    """Detach a dashboard server that outlives this launcher.

    stdout/stderr go to a file rather than a pipe: nobody drains a pipe once the
    launcher exits, and a full pipe would wedge the server.
    """

    log_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(log_path.parent, config.DIR_MODE)
    stream = open(log_path, "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-c", _CHILD_SCRIPT, str(state_path) if state_path else ""],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stream.close()


def start_background_sync(log_path: Path) -> None:
    """Refresh the ledger without holding up the browser.

    The dashboard renders its own staleness banner, so a slow sync must never be
    the reason the Dock icon looks dead.
    """

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "ab")
    except OSError:
        return
    try:
        subprocess.Popen(
            [sys.executable, "-c", _SYNC_SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        pass
    finally:
        stream.close()


def _startup_failure(process: subprocess.Popen, log_path: Path) -> DashboardLaunchError:
    reason = _last_log_line(log_path)
    detail = f": {reason}" if reason else f" (exit code {process.returncode})"
    return DashboardLaunchError(f"the dashboard server could not start{detail}")


def _last_log_line(log_path: Path, limit: int = 4096) -> str:
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            tail = stream.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _wait_until_ready(
    state_path: Path,
    process: subprocess.Popen,
    log_path: Path,
    timeout: float,
    sleep: Callable[[float], None],
) -> ServerState:
    deadline = time.monotonic() + timeout
    while True:
        status = server_status(state_path)
        if status.running and status.state is not None:
            return status.state
        if process.poll() is not None:
            raise _startup_failure(process, log_path)
        if time.monotonic() >= deadline:
            raise DashboardLaunchError(
                "the dashboard server did not become ready; run metsuke doctor"
            )
        sleep(POLL_INTERVAL_SECONDS)


def _server_answering_on_record(state_path: Path) -> ServerState | None:
    """Re-probe by liveness alone, after starting a server has failed.

    ``server_status`` reports this as stale-but-``serving``, because the reason we
    are here may be that identity detection is wrong. Re-reading the state file is
    deliberate: it may have been rewritten by the server we raced against between
    that status call and now. Only a metsuke dashboard replies ``ok`` on ``/healthz``,
    so an answering recorded port is proof enough that reusing it is right --
    certainly better than telling the user the lock is held by the very server
    they are trying to reach.

    The state file is deliberately *not* repaired here. It is written only by the
    server, under the lock; a launcher rewriting it could overwrite a newer
    server's valid state with a dead one's during a restart.
    """

    state = _read_state(state_path)
    if state is None or not _health_is_ok(state.port):
        return None
    return state


def open_dashboard(
    state_path: Path | None = None,
    opener: Callable[[str], bool] = webbrowser.open,
    starter: Callable[[Path | None, Path], subprocess.Popen] = start_server_process,
    syncer: Callable[[Path], None] | None = start_background_sync,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> LaunchResult:
    """Reuse a healthy dashboard server or start one, then open the browser."""

    path = state_path or config.dashboard_state_path()
    log_path = log_path_for(path)
    try:
        status = server_status(path)
    except DashboardServerError as exc:
        raise DashboardLaunchError(str(exc)) from exc
    if status.running and status.state is not None:
        state, reused = status.state, True
    else:
        # A stale state file (dead PID, recycled PID, or an unhealthy port) is not
        # an error: create_server takes the lock and overwrites it.
        try:
            state = _wait_until_ready(
                path, starter(state_path, log_path), log_path, timeout, sleep
            )
            reused = False
        except DashboardLaunchError:
            # Starting failed -- typically because the single-instance lock is
            # held by a server we misjudged as stale. If that server is in fact
            # answering, reuse it; "could not start: lock is held" is never the
            # right thing to show while the dashboard is up.
            recovered = _server_answering_on_record(path)
            if recovered is None:
                raise
            state, reused = recovered, True
    url = bootstrap_url(path, state)
    if syncer is not None:
        syncer(log_path)
    opener(url)
    return LaunchResult(url=url, port=state.port, reused=reused)
