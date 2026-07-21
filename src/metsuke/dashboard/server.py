"""Loopback-only HTTP server and lifecycle for the local dashboard.

Future DB-backed handlers must open ``connect_dashboard()`` once per request and
close it before returning. ``ThreadingHTTPServer`` request threads must never share
a sqlite3 connection; P2's ``mode=ro`` connection makes per-request close safe.
"""

from __future__ import annotations

import fcntl
import datetime as dt
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .. import config
from . import pages
from .auth import (
    AuthClaims,
    AuthManager,
    COOKIE_NAME,
    COOKIE_TTL_SECONDS,
    DashboardAuthError,
    load_or_create_secret,
)
from .routes import ID_PATTERN, dashboard_response, detail_response, detail_target_is_valid
from .trace_cache import TraceCache
from .trace_jobs import TraceJobError, TraceJobManager, TraceSessionNotFoundError
from .usage import record_trace_opened, record_view_opened

# socket/http.server are deliberately the only network-capable imports in the
# dashboard package: they bind and probe the IPv4 loopback service itself. They
# are never used for AI APIs, external hosts, telemetry, or content downloads.
LOOPBACK_HOST = "127.0.0.1"
STATE_KEYS = frozenset({"pid", "process_start_time", "port", "server_instance_id"})
JOB_PATH = re.compile(r"/trace-jobs/([A-Za-z0-9_-]{32})\Z")
TRACE_PREFIX = "/traces/"
TRACE_PATH = re.compile(r"/traces/([^/]{1,160})\.html\Z")
# Only the fragment the dashboard itself produces may reach a Refresh header.
TRACE_FRAGMENT = re.compile(r"#prompt=[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")

# Applied to every dashboard-owned response. The trace document is deliberately the
# only exception: its inline scripts would be blocked here, so it gets an opaque
# origin instead of the dashboard's own.
DEFAULT_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# `sandbox` without `allow-same-origin` forces an opaque origin, so trace scripts
# cannot read the dashboard origin or its cookie. Adding `allow-same-origin` here
# would defeat the isolation entirely.
TRACE_CSP = "sandbox allow-scripts"

# Largest request body the dashboard will read. Bounds both form parsing and the
# drain in ``_respond`` so a hostile Content-Length cannot make the server read
# unbounded data.
MAX_BODY_BYTES = 64 * 1024


class DashboardServerError(RuntimeError):
    """Base lifecycle error with a path-free user-facing message."""


class DashboardAlreadyRunningError(DashboardServerError):
    pass


class DashboardPortInUseError(DashboardServerError):
    pass


@dataclass(frozen=True)
class ServerState:
    pid: int
    process_start_time: str
    port: int
    server_instance_id: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "port": self.port,
            "server_instance_id": self.server_instance_id,
        }


@dataclass(frozen=True)
class ServerStatus:
    state: ServerState | None
    running: bool
    stale: bool
    #: The recorded port answered ``/healthz``. ``running`` implies this; the
    #: reverse does not. A stale state file with ``serving`` set means a real
    #: dashboard is still on the port -- exactly the case where the user needs
    #: ``stop`` to work, and where telling them "not running" would be a lie.
    serving: bool = False


class DashboardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Per-request state. ``http.server`` builds one handler instance per *connection*
    # and reuses it for every request on it, so anything cached here must be cleared
    # in ``handle_one_request`` -- see the comment there.
    request_body: bytes | None = None
    _body_framing_ok: bool = True

    def handle_one_request(self) -> None:
        """Reset per-request state before each request on a reused connection.

        One handler instance serves every request on a keep-alive connection, so a
        body cached on ``self`` would otherwise be replayed for the next request --
        which both returned the wrong fields and left the real body unread in the
        socket, desyncing the connection. Clearing here rather than in ``do_GET`` /
        ``do_POST`` makes the reset structural: a future handler method cannot
        forget it.
        """

        self.request_body = None
        self._body_framing_ok = True
        super().handle_one_request()

    def do_GET(self) -> None:
        if not self._request_origin_is_allowed():
            self._respond(403, b"forbidden")
            return
        request_path = self.path.split("?", 1)[0]
        if request_path == "/healthz":
            self._respond(200, b"ok")
            return
        if request_path == "/dashboard.css":
            self._respond(
                200,
                pages.stylesheet().encode(),
                content_type="text/css; charset=utf-8",
            )
            return
        if request_path == "/bootstrap":
            self._bootstrap()
            return
        if request_path == "/dashboard":
            claims = self._authenticated_claims()
            if claims is None:
                self._unauthorized()
                return
            query = self.path.partition("?")[2]
            started = time.perf_counter()
            response = dashboard_response(
                query,
                self._dashboard_server.database_path,
                self._dashboard_server.today(),
                now=self._dashboard_server.clock(),
                diagnostic_path=self._dashboard_server.diagnostic_path,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status == 200 and response.view is not None:
                try:
                    record_view_opened(
                        self._dashboard_server.usage_spool_path,
                        view=response.view,
                        duration_ms=elapsed_ms,
                        launch_method=self._dashboard_server.launch_method,
                    )
                except (OSError, ValueError):
                    # Measurement is fail-open and must never break a rendered page.
                    pass
            self._respond(
                response.status,
                response.body,
                {**response.headers, "X-CSRF-Token": claims.csrf_token},
                response.content_type,
            )
            return
        job_match = JOB_PATH.fullmatch(request_path)
        if job_match is not None:
            claims = self._authenticated_claims()
            if claims is None:
                self._unauthorized()
                return
            job = self._dashboard_server.trace_jobs.get(job_match.group(1))
            if job is None:
                self._not_found()
                return
            headers = {"X-CSRF-Token": claims.csrf_token}
            if job.status in {"queued", "running"}:
                headers["Refresh"] = "1"
            trace_target = ""
            if job.status == "ready" and ID_PATTERN.fullmatch(job.session_id) is not None:
                fragment = job.fragment if TRACE_FRAGMENT.fullmatch(job.fragment) else ""
                trace_target = pages.trace_url(job.session_id, fragment)
                # A zero-delay refresh replaces this job page in session history, so
                # the browser's back button returns to the detail page, not here.
                headers["Refresh"] = f"0; url={trace_target}"
            self._respond(
                200,
                pages.trace_job_page(job.status, job_match.group(1), trace_target).encode(),
                headers,
                "text/html; charset=utf-8",
            )
            return
        if request_path.startswith(TRACE_PREFIX):
            self._trace(request_path)
            return
        if request_path.startswith(("/prompts/", "/sessions/")):
            if not detail_target_is_valid(self.path):
                self._respond(
                    404,
                    pages.state_page("not_found").encode(),
                    content_type="text/html; charset=utf-8",
                )
                return
            claims = self._authenticated_claims()
            if claims is None:
                self._unauthorized()
                return
            response = detail_response(
                self.path,
                self._dashboard_server.database_path,
                now=self._dashboard_server.clock(),
                diagnostic_path=self._dashboard_server.diagnostic_path,
                csrf_token=claims.csrf_token,
            )
            self._respond(
                response.status,
                response.body,
                {**response.headers, "X-CSRF-Token": claims.csrf_token},
                response.content_type,
            )
            return
        self._respond(
            404,
            pages.state_page("not_found").encode(),
            content_type="text/html; charset=utf-8",
        )

    def do_POST(self) -> None:
        if not self._request_origin_is_allowed():
            self._respond(403, b"forbidden")
            return
        if self.path.split("?", 1)[0] != "/trace-jobs":
            self._respond(404, b"not found")
            return
        claims = self._authenticated_claims()
        if claims is None:
            self._unauthorized()
            return
        if not self._dashboard_server.auth.validate_csrf(claims, self._supplied_csrf_token()):
            self._respond(
                403,
                pages.state_page("stale").encode(),
                content_type="text/html; charset=utf-8",
            )
            return
        fields = self._form_fields()
        session_values = fields.get("session_id", []) if fields is not None else []
        # Keep the pre-P5 empty request as a harmless CSRF probe used by the
        # authentication tests. Production forms always contain session_id.
        if not session_values:
            self._respond(204, b"")
            return
        prompt_values = fields.get("prompt_id", []) if fields is not None else []
        if (
            len(session_values) != 1
            or ID_PATTERN.fullmatch(session_values[0]) is None
            or len(prompt_values) > 1
            or (prompt_values and ID_PATTERN.fullmatch(prompt_values[0]) is None)
        ):
            self._respond(400, b"invalid request")
            return
        fragment = f"#prompt={prompt_values[0]}" if prompt_values else ""
        try:
            job = self._dashboard_server.trace_jobs.submit(
                session_values[0], fragment=fragment
            )
        except TraceSessionNotFoundError:
            self._respond(
                404,
                pages.state_page("not_found").encode(),
                content_type="text/html; charset=utf-8",
            )
            return
        except TraceJobError:
            self._respond(503, b"trace unavailable")
            return
        try:
            record_trace_opened(
                self._dashboard_server.usage_spool_path,
                cache_result=job.cache_result,
                launch_method=self._dashboard_server.launch_method,
            )
        except (OSError, ValueError):
            pass
        self._respond(303, b"", {"Location": f"/trace-jobs/{job.job_id}"})

    @property
    def _dashboard_server(self) -> DashboardHTTPServer:
        if not isinstance(self.server, DashboardHTTPServer):
            raise RuntimeError("dashboard handler attached to unexpected server")
        return self.server

    def _request_origin_is_allowed(self) -> bool:
        expected_host = f"{LOOPBACK_HOST}:{self._dashboard_server.port}"
        hosts = self.headers.get_all("Host", failobj=[]) or []
        if len(hosts) != 1 or hosts[0] != expected_host:
            return False
        origins = self.headers.get_all("Origin", failobj=[]) or []
        if len(origins) > 1:
            return False
        if not origins:
            # Address-bar navigation sends no Origin at all.
            return True
        if origins[0] == f"http://{expected_host}":
            return True
        if origins[0] != "null":
            return False
        # A non-cors request from a document served with Referrer-Policy: no-referrer
        # -- which every dashboard response sets -- has its Origin serialised as the
        # literal string "null", so the dashboard's own form POSTs arrive that way.
        # Sec-Fetch-Site is a forbidden header name that page content cannot forge,
        # and browsers report same-origin only for genuinely same-origin requests.
        sites = self.headers.get_all("Sec-Fetch-Site", failobj=[]) or []
        return len(sites) == 1 and sites[0] == "same-origin"

    def _trace(self, request_path: str) -> None:
        """Serve a generated trace from the cache directory under an opaque origin."""

        if self._authenticated_claims() is None:
            self._unauthorized()
            return
        match = TRACE_PATH.fullmatch(request_path)
        session_id = match.group(1) if match is not None else ""
        # The same allowlist the trace job POST validates against. Nothing that fails
        # it ever reaches the filesystem, so no separator, dot segment, or escape can.
        if ID_PATTERN.fullmatch(session_id) is None:
            self._not_found()
            return
        directory = self._dashboard_server.trace_jobs.cache.directory
        try:
            root = directory.resolve()
            target = (root / f"{session_id}.html").resolve()
        except OSError:
            self._not_found()
            return
        # Re-assert containment after resolution so a symlinked cache entry cannot
        # redirect the read outside the traces directory.
        if target.parent != root or not target.is_file():
            self._not_found()
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._not_found()
            return
        self._respond(
            200,
            body,
            {"X-Frame-Options": "DENY"},
            "text/html; charset=utf-8",
            csp=TRACE_CSP,
        )

    def _not_found(self) -> None:
        self._respond(
            404,
            pages.state_page("not_found").encode(),
            content_type="text/html; charset=utf-8",
        )

    def _bootstrap(self) -> None:
        prefix = "/bootstrap?nonce="
        if not self.path.startswith(prefix):
            self._unauthorized()
            return
        nonce = self.path[len(prefix) :]
        if not nonce or "&" in nonce or not self._dashboard_server.auth.consume_bootstrap_nonce(nonce):
            self._unauthorized()
            return
        if self._authenticated_claims() is not None:
            self._respond(303, b"", {"Location": "/dashboard"})
            return
        cookie = self._dashboard_server.auth.issue_cookie()
        self._respond(
            303,
            b"",
            {
                "Location": "/dashboard",
                "Set-Cookie": (
                    f"{COOKIE_NAME}={cookie}; HttpOnly; SameSite=Strict; Path=/; "
                    f"Max-Age={COOKIE_TTL_SECONDS}"
                ),
            },
        )

    def _authenticated_claims(self) -> AuthClaims | None:
        values = []
        for header in self.headers.get_all("Cookie", failobj=[]) or []:
            for item in header.split(";"):
                name, separator, value = item.strip().partition("=")
                if separator and name == COOKIE_NAME:
                    values.append(value)
        if len(values) != 1:
            return None
        return self._dashboard_server.auth.validate_cookie(values[0])

    def _supplied_csrf_token(self) -> str | None:
        header_values = self.headers.get_all("X-CSRF-Token", failobj=[]) or []
        if len(header_values) > 1:
            return None
        if header_values:
            return header_values[0]
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return None
        fields = self._form_fields()
        values = fields.get("csrf_token", []) if fields is not None else []
        return values[0] if len(values) == 1 else None

    def _declared_body_length(self) -> int | None:
        """Bytes this request says it carries, or ``None`` when framing is untrustworthy.

        ``None`` means the body cannot be located safely -- an unparseable, negative,
        or oversized Content-Length, contradictory repeated ones, or a chunked body
        ``http.server`` does not decode. The caller must close the connection rather
        than guess where the next request begins.
        """

        headers = self.headers
        if headers is None:
            return 0
        if headers.get_all("Transfer-Encoding", failobj=[]):
            return None
        values = headers.get_all("Content-Length", failobj=[]) or []
        if not values:
            return 0
        if len(values) != 1:
            return None
        try:
            length = int(values[0])
        except ValueError:
            return None
        if not 0 <= length <= MAX_BODY_BYTES:
            return None
        return length

    def _consume_request_body(self) -> bytes | None:
        """Read this request's body exactly once, or ``None`` if it cannot be read.

        The result is cached for the current request only, so repeated callers
        (``_supplied_csrf_token`` then ``_form_fields``) share one read.
        """

        if not self._body_framing_ok:
            return None
        if self.request_body is not None:
            return self.request_body
        length = self._declared_body_length()
        if length is None:
            self._body_framing_ok = False
            return None
        body = self.rfile.read(length) if length else b""
        self.request_body = body
        if len(body) != length:
            # A truncated body leaves the connection at an unknown offset.
            self._body_framing_ok = False
            return None
        return body

    def _form_fields(self) -> dict[str, list[str]] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return {}
        body = self._consume_request_body()
        if body is None:
            return None
        try:
            return parse_qs(
                body.decode("ascii"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
        except (UnicodeDecodeError, ValueError):
            return None

    def _unauthorized(self) -> None:
        self._respond(
            401,
            "Metsuke.appから開き直してください".encode(),
            content_type="text/html; charset=utf-8",
        )

    def _respond(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
        content_type: str = "text/plain; charset=utf-8",
        csp: str = DEFAULT_CSP,
    ) -> None:
        # Any response that returns before reading the body -- the origin 403, the
        # 404 for another POST path, the 401 for a missing cookie -- would otherwise
        # leave the body in the socket, and the next request on this connection
        # would be parsed starting from those bytes. Draining here covers every
        # response path at once instead of relying on each one to remember.
        drained = self._consume_request_body() is not None
        # send_response_only avoids the default Server/Python and Date fingerprint headers.
        self.send_response_only(status)
        if not drained:
            # The body is unread and cannot be located, so the connection can no
            # longer be framed. send_header also flips close_connection for us.
            self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: Any) -> None:
        # Never put request paths, queries, or cookies in the default access log.
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return


class DashboardHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True
    block_on_close = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state_path: Path,
        lock_fd: int,
        database_path: Path,
        today: Callable[[], dt.date],
        clock: Callable[[], float],
        usage_spool_path: Path,
    ) -> None:
        self.state_path = state_path
        self.lock_fd = lock_fd
        self.instance_state: ServerState | None = None
        self.auth: AuthManager
        self.database_path = database_path
        self.today = today
        self.clock = clock
        self.diagnostic_path = state_path.with_name("dashboard-errors.log")
        self.usage_spool_path = usage_spool_path
        self.trace_jobs: TraceJobManager
        self.launch_method = "dashboard_server"
        self._lifecycle_closed = False
        super().__init__(server_address, DashboardRequestHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def close_lifecycle(self) -> None:
        if self._lifecycle_closed:
            return
        self._lifecycle_closed = True
        try:
            self.trace_jobs.shutdown()
            self.server_close()
        finally:
            try:
                if self.instance_state is not None:
                    current = _read_state(self.state_path)
                    if (
                        current is not None
                        and current.server_instance_id == self.instance_state.server_instance_id
                    ):
                        self.state_path.unlink(missing_ok=True)
            finally:
                _release_lock(self.lock_fd)


def _no_browser_opener(_path: Path, _fragment: str = "") -> bool:
    """The dashboard hands traces to the requesting tab over HTTP.

    A server cannot choose which browser, window, or tab receives an ``open``
    subprocess, so the dashboard path never spawns one. ``trace_html.open_browser``
    stays untouched for the ``metsuke trace`` CLI, which has no requesting tab.
    """

    return False


def _process_start_time(pid: int) -> str | None:
    """Identify a process by its start time, independently of who is asking.

    ``ps -o lstart=`` renders a human date in the *caller's* locale, so the same
    pid at the same instant reads as ``火 7/21 20:47:20 2026`` from a ja_JP shell
    and ``Tue Jul 21 20:47:20 2026`` from a Dock launch, which inherits no LANG at
    all. Comparing those for equality declared a perfectly healthy server stale.
    Forcing the C locale makes the value a property of the process instead of a
    property of the environment that happened to look at it.
    """

    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "LC_TIME": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    return value or None


def _read_state(path: Path) -> ServerState | None:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or set(raw) != STATE_KEYS:
        return None
    pid = raw.get("pid")
    started = raw.get("process_start_time")
    port = raw.get("port")
    instance_id = raw.get("server_instance_id")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(started, str)
        or not started
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(instance_id, str)
        or not instance_id
    ):
        return None
    return ServerState(pid, started, port, instance_id)


def _write_state(path: Path, state: ServerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, config.DIR_MODE)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    payload = json.dumps(state.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        config.FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, config.FILE_MODE)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _lock_path(state_path: Path) -> Path:
    return state_path.with_name("dashboard.lock")


def _acquire_lock(state_path: Path) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(state_path.parent, config.DIR_MODE)
    descriptor = os.open(
        _lock_path(state_path),
        os.O_RDWR | os.O_CREAT,
        config.FILE_MODE,
    )
    os.chmod(_lock_path(state_path), config.FILE_MODE)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise DashboardAlreadyRunningError("dashboard instance lock is held") from exc
    return descriptor


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _health_is_ok(port: int, timeout: float = 0.3) -> bool:
    request = (
        f"GET /healthz HTTP/1.0\r\nHost: {LOOPBACK_HOST}:{port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((LOOPBACK_HOST, port), timeout=timeout) as connection:
            connection.sendall(request)
            response = b""
            while len(response) <= 4096:
                chunk = connection.recv(4096 - len(response))
                if not chunk:
                    break
                response += chunk
    except OSError:
        return False
    head, separator, body = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    return separator == b"\r\n\r\n" and status_line.endswith(b" 200 OK") and body == b"ok"


def server_status(state_path: Path | None = None) -> ServerStatus:
    path = state_path or config.dashboard_state_path()
    state = _read_state(path)
    if state is None:
        return ServerStatus(None, running=False, stale=path.exists(), serving=False)
    # The port is probed even when process identity does not match. Identity
    # detection is the part that can be wrong (a locale-formatted string written
    # by an older metsuke, a ps that failed); an answering /healthz is not.
    serving = _health_is_ok(state.port)
    actual_start = _process_start_time(state.pid)
    if actual_start != state.process_start_time or not serving:
        return ServerStatus(state, running=False, stale=True, serving=serving)
    return ServerStatus(state, running=True, stale=False, serving=True)


def create_server(
    port: int | None = None,
    state_path: Path | None = None,
    secret_path: Path | None = None,
    clock: Callable[[], float] | None = None,
    database_path: Path | None = None,
    today: Callable[[], dt.date] | None = None,
    trace_generator=None,
    trace_opener=None,
) -> DashboardHTTPServer:
    path = state_path or config.dashboard_state_path()
    selected_port = config.dashboard_port() if port is None else port
    if not 0 <= selected_port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    lock_fd = _acquire_lock(path)
    server: DashboardHTTPServer | None = None
    try:
        existing = server_status(path)
        if existing.running:
            raise DashboardAlreadyRunningError("dashboard is already running")
        try:
            request_clock = clock if clock is not None else time.time
            usage_spool_path = (
                config.hooks_spool_dir()
                if state_path is None
                else path.parent.parent / "spool" / "hooks"
            )
            server = DashboardHTTPServer(
                (LOOPBACK_HOST, selected_port),
                path,
                lock_fd,
                database_path or config.home() / "ledger.db",
                today or dt.date.today,
                request_clock,
                usage_spool_path,
            )
        except OSError as exc:
            raise DashboardPortInUseError(
                "dashboard port is unavailable; run metsuke doctor"
            ) from exc
        started = _process_start_time(os.getpid())
        if started is None:
            raise DashboardServerError("cannot determine dashboard process start time")
        state = ServerState(
            pid=os.getpid(),
            process_start_time=started,
            port=server.port,
            server_instance_id=secrets.token_hex(16),
        )
        install_secret_path = secret_path or path.with_name("dashboard-secret")
        try:
            install_secret = load_or_create_secret(install_secret_path)
        except DashboardAuthError as exc:
            raise DashboardServerError("dashboard authentication is unavailable") from exc
        server.auth = AuthManager(
            install_secret,
            state.server_instance_id,
            clock=request_clock,
        )
        trace_directory = (
            config.traces_dir()
            if state_path is None
            else path.parent.parent / "traces"
        )
        trace_cache = TraceCache(
            trace_directory,
            path.with_name("trace-cache.json"),
            clock=request_clock,
        )
        trace_cache.purge()
        job_options = {"clock": request_clock, "opener": _no_browser_opener}
        if trace_generator is not None:
            job_options["generator"] = trace_generator
        if trace_opener is not None:
            job_options["opener"] = trace_opener
        server.trace_jobs = TraceJobManager(
            server.database_path,
            trace_cache,
            **job_options,
        )
        _write_state(path, state)
        server.instance_state = state
        return server
    except BaseException:
        if server is not None:
            server.server_close()
        _release_lock(lock_fd)
        raise


def serve(
    port: int | None = None,
    state_path: Path | None = None,
    on_started: Callable[[DashboardHTTPServer], None] | None = None,
) -> None:
    server = create_server(port=port, state_path=state_path)
    old_handlers: dict[int, Any] = {}

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.signal(signum, request_shutdown)
        if on_started is not None:
            on_started(server)
        server.serve_forever()
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        server.close_lifecycle()


def stop(state_path: Path | None = None) -> bool:
    """Terminate the recorded server whenever the recorded port is answering.

    Gating on process identity alone made a server unstoppable the moment that
    identity check went wrong: it still held the single-instance lock, so it
    could be neither stopped nor replaced, and the CLI reported it as "not
    running" while it served traffic.

    The health probe -- not the identity match -- is now what keeps the signal
    safe. ``create_server`` binds the port and writes the state file with its own
    pid under the instance lock, so when the answerer is a metsuke dashboard the
    recorded pid is that dashboard: any server that took the port rewrote the
    state file first. That is a real trade, not an equivalence. Dropping the
    identity check loses one protection: a recycled pid whose unrelated new owner
    happens to answer this exact /healthz contract on loopback would be signalled.
    Nobody chooses which pid the OS recycles, so that is an accident rather than a
    usable attack, and anyone who can write the state file can name any pid they
    like regardless of what we check here.
    """

    status = server_status(state_path)
    if status.state is None or not status.serving:
        return False
    os.kill(status.state.pid, signal.SIGTERM)
    return True
