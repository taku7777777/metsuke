from __future__ import annotations

import ast
import base64
import datetime as dt
import http.client
import inspect
import json
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from metsuke import ledger, trace_html
from metsuke.dashboard import auth, server, usage

AUTH_SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AUTH_PROMPT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@dataclass
class MutableClock:
    value: float = 1_800_000_000

    def __call__(self) -> float:
        return self.value


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes, list[tuple[str, str]]]:
    connection = http.client.HTTPConnection(server.LOOPBACK_HOST, port, timeout=1)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        connection.close()


def _headers(items: list[tuple[str, str]]) -> dict[str, str]:
    return dict(items)


def _request_without_host(port: int) -> int:
    connection = http.client.HTTPConnection(server.LOOPBACK_HOST, port, timeout=1)
    try:
        connection.putrequest("GET", "/healthz", skip_host=True)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _request_with_repeated_headers(
    port: int,
    path: str,
    headers: list[tuple[str, str]],
) -> int:
    """Issue a request whose header list may repeat a name (dicts cannot)."""
    connection = http.client.HTTPConnection(server.LOOPBACK_HOST, port, timeout=1)
    try:
        connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _wait_until_healthy(port: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if _request(port, "/healthz")[:2] == (200, b"ok"):
                return
        except OSError:
            pass
        time.sleep(0.01)
    raise AssertionError("dashboard server did not become healthy")


def _seed_dashboard_data(connection, stamp: float) -> None:
    connection.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        (AUTH_SESSION_ID, "auth-fixture", stamp, stamp),
    )
    connection.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES (?,?,?,?)",
        (AUTH_PROMPT_ID, AUTH_SESSION_ID, stamp, "auth fixture"),
    )
    connection.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,?,?,?,?,'claude-sonnet-5',1,1,0,0,0,0,0,'transcript')""",
        (
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            AUTH_SESSION_ID,
            AUTH_SESSION_ID,
            AUTH_PROMPT_ID,
            stamp,
        ),
    )
    connection.execute(
        "INSERT INTO ingest_log(ts,manifest_pos,segments,records,quarantined,parser_version) VALUES (?,?,?,?,?,?)",
        (stamp, 1, 1, 1, 0, 1),
    )
    connection.commit()


@pytest.fixture
def auth_server(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    clock = MutableClock()
    state_path = tmp_path / "state" / "dashboard-state.json"
    secret_path = tmp_path / "state" / "dashboard-secret"
    database_path = tmp_path / "ledger.db"
    connection = ledger.connect(database_path)
    _seed_dashboard_data(connection, clock.value - 86400)
    connection.close()
    dashboard = server.create_server(
        port=0,
        state_path=state_path,
        secret_path=secret_path,
        clock=clock,
        database_path=database_path,
        today=lambda: dt.date(2027, 1, 15),
    )
    thread = threading.Thread(target=dashboard.serve_forever)
    thread.start()
    try:
        _wait_until_healthy(dashboard.port)
        yield dashboard, clock, state_path, secret_path
    finally:
        dashboard.shutdown()
        thread.join(timeout=3)
        dashboard.close_lifecycle()
        assert not thread.is_alive()


def _bootstrap(dashboard: server.DashboardHTTPServer) -> tuple[str, str, list[tuple[str, str]]]:
    nonce = dashboard.auth.issue_bootstrap_nonce()
    status, _, response_headers = _request(dashboard.port, f"/bootstrap?nonce={nonce}")
    assert status == 303
    set_cookie = _headers(response_headers)["Set-Cookie"]
    cookie = set_cookie.split(";", 1)[0]
    return nonce, cookie, response_headers


def _authenticated_dashboard(
    dashboard: server.DashboardHTTPServer,
) -> tuple[str, str, str]:
    nonce, cookie, _ = _bootstrap(dashboard)
    status, _, response_headers = _request(
        dashboard.port, "/dashboard", headers={"Cookie": cookie}
    )
    assert status == 303
    status, _, response_headers = _request(
        dashboard.port,
        _headers(response_headers)["Location"],
        headers={"Cookie": cookie},
    )
    assert status == 200
    csrf = _headers(response_headers)["X-CSRF-Token"]
    return nonce, cookie, csrf


def _tamper_signature(token: str) -> str:
    body, signature = token.split(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    return f"{body}.{replacement}{signature[1:]}"


def _cookie_token(cookie: str) -> str:
    return cookie.split("=", 1)[1]


def _payload(token: str) -> dict:
    body = token.split(".", 1)[0]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


def test_cookie_missing_is_401_and_valid_cookie_is_200(auth_server):
    dashboard, _, _, _ = auth_server
    status, body, _ = _request(dashboard.port, "/dashboard")
    assert status == 401
    assert "Metsuke.appから開き直してください".encode() in body

    _, cookie, _ = _authenticated_dashboard(dashboard)
    status, _, headers = _request(dashboard.port, "/dashboard", headers={"Cookie": cookie})
    assert status == 303
    status, body, _ = _request(
        dashboard.port, _headers(headers)["Location"], headers={"Cookie": cookie}
    )
    assert status == 200
    assert b"metsuke dashboard" in body


def test_detail_routes_require_cookie_while_traversal_is_safe_404(auth_server):
    dashboard, _, _, _ = auth_server
    assert _request(dashboard.port, f"/prompts/{AUTH_PROMPT_ID}")[0] == 401
    status, body, _ = _request(dashboard.port, "/prompts/../../etc/passwd")
    assert status == 404
    assert "dashboardへ戻る".encode() in body

    _, cookie, _ = _authenticated_dashboard(dashboard)
    status, body, _ = _request(
        dashboard.port,
        f"/prompts/{AUTH_PROMPT_ID}",
        headers={"Cookie": cookie},
    )
    assert status == 200
    assert "prompt詳細".encode() in body
    status, body, _ = _request(
        dashboard.port,
        f"/sessions/{AUTH_SESSION_ID}",
        headers={"Cookie": cookie},
    )
    assert status == 200
    assert "session詳細".encode() in body


def test_expired_cookie_is_rejected_while_unexpired_cookie_passes(auth_server):
    dashboard, clock, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    assert _request(dashboard.port, "/dashboard", headers={"Cookie": cookie})[0] == 303
    clock.value += auth.COOKIE_TTL_SECONDS + 1
    status, body, _ = _request(dashboard.port, "/dashboard", headers={"Cookie": cookie})
    assert status == 401
    assert _cookie_token(cookie).encode() not in body


def test_tampered_cookie_is_rejected_while_original_passes(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    name, token = cookie.split("=", 1)
    tampered = f"{name}={_tamper_signature(token)}"
    assert _request(dashboard.port, "/dashboard", headers={"Cookie": tampered})[0] == 401
    assert _request(dashboard.port, "/dashboard", headers={"Cookie": cookie})[0] == 303


def test_cookie_from_another_install_is_rejected(auth_server):
    dashboard, clock, _, _ = auth_server
    assert dashboard.instance_state is not None
    foreign = auth.AuthManager(
        b"x" * auth.SECRET_BYTES,
        dashboard.instance_state.server_instance_id,
        clock,
    ).issue_cookie()
    assert (
        _request(
            dashboard.port,
            "/dashboard",
            headers={"Cookie": f"{auth.COOKIE_NAME}={foreign}"},
        )[0]
        == 401
    )
    valid = dashboard.auth.issue_cookie()
    assert (
        _request(
            dashboard.port,
            "/dashboard",
            headers={"Cookie": f"{auth.COOKIE_NAME}={valid}"},
        )[0]
        == 303
    )


def test_bootstrap_nonce_is_one_time_and_valid_nonce_redirects_without_query(auth_server):
    dashboard, _, _, _ = auth_server
    nonce, _, response_headers = _bootstrap(dashboard)
    headers = _headers(response_headers)
    assert headers["Location"] == "/dashboard"
    assert "?" not in headers["Location"]
    assert nonce not in headers["Location"]
    assert _request(dashboard.port, f"/bootstrap?nonce={nonce}")[0] == 401


def test_expired_nonce_is_rejected_while_fresh_nonce_passes(auth_server):
    dashboard, clock, _, _ = auth_server
    expired = dashboard.auth.issue_bootstrap_nonce()
    clock.value += auth.BOOTSTRAP_TTL_SECONDS + 1
    assert _request(dashboard.port, f"/bootstrap?nonce={expired}")[0] == 401
    fresh = dashboard.auth.issue_bootstrap_nonce()
    assert _request(dashboard.port, f"/bootstrap?nonce={fresh}")[0] == 303


def test_bootstrap_reuses_valid_cookie_and_without_cookie_still_sets_one(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)

    nonce = dashboard.auth.issue_bootstrap_nonce()
    status, _, response_headers = _request(
        dashboard.port,
        f"/bootstrap?nonce={nonce}",
        headers={"Cookie": cookie},
    )
    assert status == 303
    assert _headers(response_headers)["Location"] == "/dashboard"
    assert all(name.lower() != "set-cookie" for name, _ in response_headers)
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )[0]
        == 204
    )

    fresh_nonce = dashboard.auth.issue_bootstrap_nonce()
    status, _, response_headers = _request(
        dashboard.port, f"/bootstrap?nonce={fresh_nonce}"
    )
    assert status == 303
    assert any(name.lower() == "set-cookie" for name, _ in response_headers)


def test_nonce_binds_instance_but_cookie_survives_server_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    clock = MutableClock()
    state_path = tmp_path / "state" / "dashboard-state.json"
    secret_path = tmp_path / "state" / "dashboard-secret"
    database_path = tmp_path / "ledger.db"
    connection = ledger.connect(database_path)
    _seed_dashboard_data(connection, clock.value - 86400)
    connection.close()

    first = server.create_server(0, state_path, secret_path, clock, database_path)
    first_thread = threading.Thread(target=first.serve_forever)
    first_thread.start()
    _wait_until_healthy(first.port)
    old_instance = first.instance_state.server_instance_id
    old_nonce = first.auth.issue_bootstrap_nonce()
    _, cookie, _ = _authenticated_dashboard(first)
    secret_before = secret_path.read_bytes()
    first.shutdown()
    first_thread.join(timeout=3)
    first.close_lifecycle()

    second = server.create_server(0, state_path, secret_path, clock, database_path)
    second_thread = threading.Thread(target=second.serve_forever)
    second_thread.start()
    try:
        _wait_until_healthy(second.port)
        assert second.instance_state.server_instance_id != old_instance
        assert secret_path.read_bytes() == secret_before
        assert _request(second.port, f"/bootstrap?nonce={old_nonce}")[0] == 401
        assert _request(second.port, "/dashboard", headers={"Cookie": cookie})[0] == 303
    finally:
        second.shutdown()
        second_thread.join(timeout=3)
        second.close_lifecycle()
        assert not second_thread.is_alive()


@pytest.mark.parametrize(
    "host",
    ["localhost:{port}", "192.168.1.25:{port}", "example.com:{port}"],
)
def test_host_allowlist_rejects_non_literal_loopback_and_accepts_correct_host(auth_server, host):
    dashboard, _, _, _ = auth_server
    expected = f"{server.LOOPBACK_HOST}:{dashboard.port}"
    assert _request(dashboard.port, "/healthz", headers={"Host": expected})[0] == 200
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={"Host": host.format(port=dashboard.port)},
        )[0]
        == 403
    )


def test_host_header_is_mandatory_while_exact_host_passes(auth_server):
    dashboard, _, _, _ = auth_server
    assert _request_without_host(dashboard.port) == 403
    expected = f"{server.LOOPBACK_HOST}:{dashboard.port}"
    assert _request(dashboard.port, "/healthz", headers={"Host": expected})[0] == 200


def test_origin_allowlist_rejects_cross_origin_and_accepts_same_origin(auth_server):
    dashboard, _, _, _ = auth_server
    same_origin = f"http://{server.LOOPBACK_HOST}:{dashboard.port}"
    assert _request(dashboard.port, "/healthz", headers={"Origin": same_origin})[0] == 200
    assert _request(dashboard.port, "/healthz", headers={"Origin": "https://example.com"})[0] == 403


# Chrome serialises Origin as the literal string "null" for a non-cors navigation
# request when the document's referrer policy is no-referrer -- which the dashboard
# sets on every response. These cases model the headers captured from real Chrome
# on the "traceで見る" form POST, which the idealised Origin used elsewhere misses.
def test_null_origin_with_same_origin_fetch_metadata_is_accepted(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
            },
        )[0]
        == 200
    )


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_null_origin_without_same_origin_fetch_metadata_is_rejected(auth_server, site):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
                "Origin": "null",
                "Sec-Fetch-Site": site,
            },
        )[0]
        == 403
    )


def test_null_origin_without_any_fetch_metadata_is_rejected(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
                "Origin": "null",
            },
        )[0]
        == 403
    )


def test_null_origin_with_repeated_fetch_metadata_is_rejected(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request_with_repeated_headers(
            dashboard.port,
            "/healthz",
            [
                ("Host", f"{server.LOOPBACK_HOST}:{dashboard.port}"),
                ("Origin", "null"),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Site", "cross-site"),
            ],
        )
        == 403
    )


def test_repeated_origin_headers_are_rejected(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request_with_repeated_headers(
            dashboard.port,
            "/healthz",
            [
                ("Host", f"{server.LOOPBACK_HOST}:{dashboard.port}"),
                ("Origin", f"http://{server.LOOPBACK_HOST}:{dashboard.port}"),
                ("Origin", "null"),
                ("Sec-Fetch-Site", "same-origin"),
            ],
        )
        == 403
    )


@pytest.mark.parametrize("fetch_metadata", [{}, {"Sec-Fetch-Site": "same-origin"}])
def test_foreign_origin_is_rejected_even_with_same_origin_fetch_metadata(
    auth_server, fetch_metadata
):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
                "Origin": "http://evil.example",
                **fetch_metadata,
            },
        )[0]
        == 403
    )


def test_null_origin_with_mismatched_host_is_rejected(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"evil.example:{dashboard.port}",
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
            },
        )[0]
        == 403
    )


def test_address_bar_navigation_without_origin_is_accepted(auth_server):
    dashboard, _, _, _ = auth_server
    assert (
        _request(
            dashboard.port,
            "/healthz",
            headers={
                "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )[0]
        == 200
    )


def test_chrome_form_post_with_null_origin_reaches_trace_job_redirect(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)

    def generate(session_id, _conn):
        path = dashboard.trace_jobs.cache.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated trace")
        return path

    dashboard.trace_jobs.generator = generate
    dashboard.trace_jobs.opener = lambda *_: True
    body = f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={AUTH_PROMPT_ID}".encode()
    # Header set captured verbatim from Chrome submitting the prompt detail form.
    status, _, headers = _request(
        dashboard.port,
        "/trace-jobs",
        method="POST",
        headers={
            "Host": f"{server.LOOPBACK_HOST}:{dashboard.port}",
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Cookie": cookie,
        },
        body=body,
    )
    assert status == 303
    assert _headers(headers)["Location"].startswith("/trace-jobs/")


def test_csrf_gate_rejects_missing_and_mismatch_but_accepts_issued_token(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    cookie_header = {"Cookie": cookie}
    assert _request(dashboard.port, "/trace-jobs", method="POST", headers=cookie_header)[0] == 403
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={**cookie_header, "X-CSRF-Token": "wrong-token"},
        )[0]
        == 403
    )
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={**cookie_header, "X-CSRF-Token": csrf},
        )[0]
        == 204
    )
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={**cookie_header, "Content-Type": "application/x-www-form-urlencoded"},
            body=f"csrf_token={csrf}".encode(),
        )[0]
        == 204
    )


def test_stale_csrf_token_returns_recoverable_html(auth_server):
    dashboard, _, _, _ = auth_server
    _, _, csrf_a = _authenticated_dashboard(dashboard)
    _, cookie_b, _ = _authenticated_dashboard(dashboard)

    status, body, response_headers = _request(
        dashboard.port,
        "/trace-jobs",
        method="POST",
        headers={"Cookie": cookie_b, "X-CSRF-Token": csrf_a},
    )
    assert status == 403
    assert _headers(response_headers)["Content-Type"] == "text/html; charset=utf-8"
    assert body != b"forbidden"
    assert b"forbidden" not in body
    assert "再読み込みしてから".encode() in body


def test_missing_or_forged_csrf_never_submits_trace_job(auth_server, monkeypatch):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    submissions = []

    def submit(*args, **kwargs):
        submissions.append((args, kwargs))
        raise AssertionError("trace submission must not run")

    monkeypatch.setattr(dashboard.trace_jobs, "submit", submit)
    body = f"session_id={AUTH_SESSION_ID}".encode()
    base_headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers=base_headers,
            body=body,
        )[0]
        == 403
    )
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={**base_headers, "X-CSRF-Token": "wrong-token"},
            body=body,
        )[0]
        == 403
    )
    assert submissions == []


def test_authenticated_trace_post_runs_job_and_status_page_never_links_file(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    opened = []

    detail_status, detail, _ = _request(
        dashboard.port, f"/sessions/{AUTH_SESSION_ID}", headers={"Cookie": cookie}
    )
    assert detail_status == 200
    assert b'<form method="post" action="/trace-jobs">' in detail
    assert f'name="csrf_token" value="{csrf}"'.encode() in detail
    assert b"<script" not in detail.lower()

    def generate(session_id, conn):
        assert session_id == AUTH_SESSION_ID
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        path = dashboard.trace_jobs.cache.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated trace")
        return path

    dashboard.trace_jobs.generator = generate
    # Left at whatever create_server configured: the dashboard must never open a
    # browser itself, so the production opener is a no-op, not trace_html's.
    assert dashboard.trace_jobs.opener is server._no_browser_opener
    body = (
        f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={AUTH_PROMPT_ID}"
    ).encode()
    status, _, headers = _request(
        dashboard.port,
        "/trace-jobs",
        method="POST",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    assert status == 303
    location = _headers(headers)["Location"]
    expected_target = (
        f"/traces/{AUTH_SESSION_ID}.html#prompt={AUTH_PROMPT_ID}"
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status, page, response_headers = _request(
            dashboard.port, location, headers={"Cookie": cookie}
        )
        refresh = dict(response_headers).get("Refresh")
        if refresh == f"0; url={expected_target}":
            break
        # While generating, the page polls and must not claim the trace was opened
        # somewhere the user cannot see.
        assert refresh == "1"
        assert "別タブ".encode() not in page
        time.sleep(0.01)
    assert status == 200
    assert dict(response_headers).get("Refresh") == f"0; url={expected_target}"
    # The status page still never hands the browser a file: URL; it now points at
    # the authenticated same-origin HTTP route instead.
    assert b"file:" not in page
    assert expected_target.encode() in page
    assert "別タブ".encode() not in page
    assert opened == []
    assert server._no_browser_opener(Path("/any/trace.html"), "#prompt=x") is False

    trace_status, trace, trace_headers = _request(
        dashboard.port, f"/traces/{AUTH_SESSION_ID}.html", headers={"Cookie": cookie}
    )
    assert trace_status == 200
    assert trace == b"generated trace"
    assert dict(trace_headers)["Content-Security-Policy"] == "sandbox allow-scripts"


def _write_trace(dashboard, session_id: str = AUTH_SESSION_ID, body: bytes = b"") -> Path:
    """Place a trace file in the cache directory the way a finished job would."""

    path = dashboard.trace_jobs.cache.path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body or b"<!doctype html><script>trace()</script>")
    return path


def test_trace_route_serves_the_cached_file_bytes_to_an_authenticated_tab(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    path = _write_trace(dashboard)

    status, body, headers = _request(
        dashboard.port, f"/traces/{AUTH_SESSION_ID}.html", headers={"Cookie": cookie}
    )
    assert status == 200
    assert body == path.read_bytes()
    assert _headers(headers)["Content-Type"] == "text/html; charset=utf-8"


def test_trace_response_is_sandboxed_into_an_opaque_origin(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    _write_trace(dashboard)

    status, _, headers = _request(
        dashboard.port, f"/traces/{AUTH_SESSION_ID}.html", headers={"Cookie": cookie}
    )
    assert status == 200
    policy = _headers(headers)["Content-Security-Policy"]
    # `sandbox` without `allow-same-origin` is what makes the origin opaque; adding
    # it back would give trace scripts the dashboard's authenticated origin.
    assert policy == "sandbox allow-scripts"
    assert "allow-same-origin" not in policy
    assert "default-src 'none'" not in policy
    assert _headers(headers)["X-Frame-Options"] == "DENY"
    assert _headers(headers)["Cache-Control"] == "no-store"
    assert _headers(headers)["Referrer-Policy"] == "no-referrer"


def test_every_non_trace_response_keeps_the_default_content_security_policy(auth_server):
    """Guards the _respond CSP override: only the trace route may differ."""

    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    _write_trace(dashboard)
    authenticated = {"Cookie": cookie}
    probes = (
        ("/healthz", {}),
        ("/dashboard.css", {}),
        ("/dashboard", authenticated),
        ("/dashboard", {}),
        (f"/prompts/{AUTH_PROMPT_ID}", authenticated),
        (f"/sessions/{AUTH_SESSION_ID}", authenticated),
        ("/trace-jobs/" + "z" * 32, authenticated),
        ("/nope", authenticated),
        (f"/traces/{AUTH_SESSION_ID}.html", {}),
        ("/traces/../ledger.db", authenticated),
    )
    for path, request_headers in probes:
        _, _, headers = _request(dashboard.port, path, headers=request_headers)
        assert _headers(headers)["Content-Security-Policy"] == server.DEFAULT_CSP, path
        assert "sandbox" not in _headers(headers)["Content-Security-Policy"], path
        assert "X-Frame-Options" not in _headers(headers), path


def test_trace_without_a_cookie_is_unauthorized_and_leaks_no_bytes(auth_server):
    dashboard, _, _, _ = auth_server
    _authenticated_dashboard(dashboard)
    secret = b"<!doctype html><script>secret trace body</script>"
    _write_trace(dashboard, body=secret)

    for request_headers in ({}, {"Cookie": f"{auth.COOKIE_NAME}=forged"}):
        status, body, _ = _request(
            dashboard.port, f"/traces/{AUTH_SESSION_ID}.html", headers=request_headers
        )
        assert status == 401
        assert secret not in body
        assert b"trace" not in body


def test_trace_ids_outside_the_allowlist_never_reach_the_filesystem(auth_server, tmp_path):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    directory = dashboard.trace_jobs.cache.directory
    directory.mkdir(parents=True, exist_ok=True)
    outside = directory.parent / "outside.html"
    outside.write_bytes(b"<!doctype html>OUTSIDE-THE-TRACE-DIRECTORY")
    (directory.parent / "ledger.db").write_bytes(b"LEDGER-BYTES")

    hostile = (
        "/traces/../outside.html",
        "/traces/../ledger.db",
        "/traces/..%2Foutside.html",
        "/traces/%2e%2e%2foutside.html",
        "/traces/subdir/outside.html",
        "/traces/.html",
        "/traces/short.html",  # shorter than the allowlist minimum
        "/traces/-leading.html",  # allowlist requires an alphanumeric first character
        "/traces/" + "a" * 200 + ".html",
        f"/traces/{AUTH_SESSION_ID}.txt",
        f"/traces/{AUTH_SESSION_ID}.html/../outside.html",
    )
    for path in hostile:
        status, body, headers = _request(dashboard.port, path, headers={"Cookie": cookie})
        assert status in {401, 404}, path
        assert b"OUTSIDE-THE-TRACE-DIRECTORY" not in body, path
        assert b"LEDGER-BYTES" not in body, path
        assert _headers(headers)["Content-Security-Policy"] == server.DEFAULT_CSP, path


def test_trace_symlink_escaping_the_directory_is_refused(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    directory = dashboard.trace_jobs.cache.directory
    directory.mkdir(parents=True, exist_ok=True)
    outside = directory.parent / "escaped.html"
    outside.write_bytes(b"ESCAPED-VIA-SYMLINK")
    dashboard.trace_jobs.cache.path_for(AUTH_SESSION_ID).symlink_to(outside)

    status, body, _ = _request(
        dashboard.port, f"/traces/{AUTH_SESSION_ID}.html", headers={"Cookie": cookie}
    )
    assert status == 404
    assert b"ESCAPED-VIA-SYMLINK" not in body


def test_wellformed_but_ungenerated_trace_id_returns_the_not_found_page(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    missing = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

    status, body, headers = _request(
        dashboard.port, f"/traces/{missing}.html", headers={"Cookie": cookie}
    )
    assert status == 404
    assert _headers(headers)["Content-Type"] == "text/html; charset=utf-8"
    assert "詳細が見つかりません".encode() in body


def _seed_job(dashboard, status: str, fragment: str = f"#prompt={AUTH_PROMPT_ID}"):
    job = dashboard.trace_jobs._new_job(status, "miss", AUTH_SESSION_ID, fragment)
    dashboard.trace_jobs._jobs[job.job_id] = job
    return job


@pytest.mark.parametrize("status", ["queued", "running"])
def test_pending_job_page_polls_and_never_claims_another_tab(auth_server, status):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    job = _seed_job(dashboard, status)

    code, body, headers = _request(
        dashboard.port, f"/trace-jobs/{job.job_id}", headers={"Cookie": cookie}
    )
    assert code == 200
    assert _headers(headers)["Refresh"] == "1"
    assert "別タブ".encode() not in body
    assert "開きました".encode() not in body
    assert b"/traces/" not in body
    assert b"<script" not in body.lower()


def test_ready_job_page_navigates_this_tab_and_keeps_the_prompt_fragment(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    job = _seed_job(dashboard, "ready")

    code, body, headers = _request(
        dashboard.port, f"/trace-jobs/{job.job_id}", headers={"Cookie": cookie}
    )
    assert code == 200
    target = f"/traces/{AUTH_SESSION_ID}.html#prompt={AUTH_PROMPT_ID}"
    # Zero-delay refresh replaces this page in history, so back returns to the detail
    # page rather than bouncing through the job page again.
    assert _headers(headers)["Refresh"] == f"0; url={target}"
    assert target.encode() in body
    assert b"<script" not in body.lower()


def test_ready_job_page_without_a_prompt_omits_the_fragment(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    job = _seed_job(dashboard, "ready", fragment="")

    _, _, headers = _request(
        dashboard.port, f"/trace-jobs/{job.job_id}", headers={"Cookie": cookie}
    )
    assert _headers(headers)["Refresh"] == f"0; url=/traces/{AUTH_SESSION_ID}.html"


def test_forged_job_fragment_cannot_be_injected_into_the_refresh_header(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    job = _seed_job(dashboard, "ready", fragment="#prompt=x\r\nX-Injected: 1")

    _, body, headers = _request(
        dashboard.port, f"/trace-jobs/{job.job_id}", headers={"Cookie": cookie}
    )
    assert _headers(headers)["Refresh"] == f"0; url=/traces/{AUTH_SESSION_ID}.html"
    assert "X-Injected" not in _headers(headers)
    assert b"X-Injected" not in body


def test_failed_job_page_offers_a_way_back_without_navigating(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, _ = _authenticated_dashboard(dashboard)
    job = _seed_job(dashboard, "failed")

    code, body, headers = _request(
        dashboard.port, f"/trace-jobs/{job.job_id}", headers={"Cookie": cookie}
    )
    assert code == 200
    assert "Refresh" not in _headers(headers)
    assert "traceを生成できませんでした".encode() in body
    assert b'<a href="/dashboard">' in body


def test_dashboard_trace_flow_spawns_no_browser_subprocess(auth_server, monkeypatch):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    launched = []
    monkeypatch.setattr(
        trace_html.subprocess, "run", lambda *a, **k: launched.append(a) or None
    )
    monkeypatch.setattr(
        trace_html, "open_browser", lambda *a, **k: launched.append(a) or True
    )

    def generate(session_id, _conn):
        return _write_trace(dashboard, session_id, b"<!doctype html>generated")

    dashboard.trace_jobs.generator = generate
    assert dashboard.trace_jobs.opener is server._no_browser_opener
    status, _, headers = _request(
        dashboard.port,
        "/trace-jobs",
        method="POST",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
        body=f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}".encode(),
    )
    assert status == 303
    location = _headers(headers)["Location"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        _, _, response_headers = _request(
            dashboard.port, location, headers={"Cookie": cookie}
        )
        if _headers(response_headers).get("Refresh", "").startswith("0; url="):
            break
        time.sleep(0.01)
    assert launched == []


def test_trace_usage_event_cannot_contain_session_prompt_project_or_filter(auth_server):
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)

    def generate(session_id, _conn):
        path = dashboard.trace_jobs.cache.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trace")
        return path

    dashboard.trace_jobs.generator = generate
    dashboard.trace_jobs.opener = lambda *_: True
    body = (
        f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={AUTH_PROMPT_ID}"
    ).encode()
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )[0]
        == 303
    )
    events = []
    for path in dashboard.usage_spool_path.glob("*.ndjson"):
        events.extend(json.loads(line) for line in path.read_text().splitlines())
    trace_events = [event for event in events if event["metsuke_event"] == "dashboard_trace_opened"]
    assert len(trace_events) == 1
    serialized = json.dumps(trace_events, ensure_ascii=False)
    for forbidden in (
        AUTH_SESSION_ID,
        AUTH_PROMPT_ID,
        "auth-fixture",
        "from",
        "project",
        "filter",
    ):
        assert forbidden not in serialized
    assert set(trace_events[0]["payload"]) == {
        "result",
        "launch_method",
        "trace_cache",
    }


def test_auth_material_never_reaches_logs_or_state_files(auth_server, capsys, caplog, tmp_path):
    dashboard, _, _, _ = auth_server
    nonce, cookie, csrf = _authenticated_dashboard(dashboard)
    assert (
        _request(
            dashboard.port,
            "/trace-jobs",
            method="POST",
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )[0]
        == 204
    )
    captured = capsys.readouterr()
    logs = captured.out + captured.err + caplog.text
    sensitive = (nonce, _cookie_token(cookie), csrf)
    for value in sensitive:
        assert value not in logs
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for value in sensitive:
                assert value.encode() not in content


def test_dashboard_usage_spool_is_success_only_and_structurally_pii_free(auth_server):
    dashboard, _, _, _ = auth_server
    assert tuple(inspect.signature(usage.record_view_opened).parameters) == (
        "spool",
        "view",
        "duration_ms",
        "launch_method",
    )
    _, cookie, _ = _bootstrap(dashboard)
    spool = dashboard.usage_spool_path

    redirect = _request(dashboard.port, "/dashboard", headers={"Cookie": cookie})
    assert redirect[0] == 303
    rejected = _request(
        dashboard.port,
        "/dashboard?view=not-a-view&from=2027-01-14&to=2027-01-14",
        headers={"Cookie": cookie},
    )
    assert rejected[0] == 400
    assert not list(spool.glob("*.ndjson"))

    project_canary = "auth-fixture"
    filter_canary = "from=2027-01-14"
    path = (
        "/dashboard?view=overview&from=2027-01-14&to=2027-01-14"
        f"&project={project_canary}"
    )
    status, body, _ = _request(dashboard.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert b"auth fixture" in body

    files = list(spool.glob("*.ndjson"))
    assert len(files) == 1
    raw = files[0].read_text()
    envelope = json.loads(raw)
    assert envelope["metsuke_event"] == "dashboard_view_opened"
    assert set(envelope["payload"]) == {
        "view",
        "result",
        "duration_ms",
        "launch_method",
        "trace_cache",
    }
    assert envelope["payload"] == {
        "view": "overview",
        "result": "success",
        "duration_ms": envelope["payload"]["duration_ms"],
        "launch_method": "dashboard_server",
        "trace_cache": "not_applicable",
    }
    for forbidden in (
        "auth fixture",
        AUTH_PROMPT_ID,
        AUTH_SESSION_ID,
        project_canary,
        filter_canary,
        "2027-01-14",
    ):
        assert forbidden not in raw


def test_cookie_attributes_and_secret_permissions(auth_server):
    dashboard, _, _, secret_path = auth_server
    _, cookie, response_headers = _bootstrap(dashboard)
    set_cookie = _headers(response_headers)["Set-Cookie"]
    assert set_cookie.startswith(cookie + ";")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Secure" not in set_cookie
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert len(secret_path.read_bytes()) == auth.SECRET_BYTES


def test_nonce_and_cookie_payloads_have_opposite_instance_binding(auth_server):
    dashboard, _, _, _ = auth_server
    nonce = dashboard.auth.issue_bootstrap_nonce()
    cookie = dashboard.auth.issue_cookie()
    assert _payload(nonce)["instance"] == dashboard.instance_state.server_instance_id
    cookie_payload = _payload(cookie)
    assert "instance" not in cookie_payload
    assert dashboard.instance_state.server_instance_id not in json.dumps(cookie_payload)


def test_used_nonce_digests_are_kept_in_memory_for_only_sixty_seconds():
    clock = MutableClock()
    manager = auth.AuthManager(b"s" * auth.SECRET_BYTES, "instance", clock)
    first = manager.issue_bootstrap_nonce()
    assert manager.consume_bootstrap_nonce(first) is True
    assert len(manager._used_nonce_digests) == 1
    clock.value += auth.BOOTSTRAP_TTL_SECONDS + 1
    second = manager.issue_bootstrap_nonce()
    assert manager.consume_bootstrap_nonce(second) is True
    assert len(manager._used_nonce_digests) == 1


def test_signature_verification_uses_compare_digest():
    source = Path(auth.__file__).read_text()
    tree = ast.parse(source)
    decoder = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_decode_signed"
    )
    calls = [
        node
        for node in ast.walk(decoder)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
        and node.func.attr == "compare_digest"
    ]
    assert len(calls) == 1


# --- Connection reuse -------------------------------------------------------
#
# Every helper above opens a fresh connection per request, which is exactly why a
# fully green suite still let real Chrome hit `501 Unsupported method` on the second
# "traceで見る" click. `http.server` builds one handler instance per *connection* and
# reuses it for every request on that connection, so any state cached on the handler
# -- or any request body left unread in the socket -- leaks into the next request.
# The tests below therefore drive several requests over one persistent connection.

SECOND_PROMPT_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
MAX_BODY_BYTES = 64 * 1024


def _keepalive(port: int) -> http.client.HTTPConnection:
    """One connection reused for every request, exactly as a browser does."""
    return http.client.HTTPConnection(server.LOOPBACK_HOST, port, timeout=3)


def _exchange(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    return response.status, payload, _headers(response.getheaders())


def _install_trace_generator(dashboard: server.DashboardHTTPServer) -> None:
    def generate(session_id, _conn):
        path = dashboard.trace_jobs.cache.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<!doctype html>generated trace")
        return path

    dashboard.trace_jobs.generator = generate


def _await_job_settled(dashboard: server.DashboardHTTPServer, job_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = dashboard.trace_jobs.get(job_id)
        if job is not None and job.status not in {"queued", "running"}:
            return
        time.sleep(0.01)
    raise AssertionError("trace job never settled")


def test_repeated_trace_click_on_one_connection_is_never_a_protocol_error(auth_server):
    """The reported Chrome sequence, twice, over a single keep-alive connection."""
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    _install_trace_generator(dashboard)
    form_headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    post_body = (
        f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={AUTH_PROMPT_ID}"
    ).encode()
    observed: list[int] = []
    connection = _keepalive(dashboard.port)
    try:
        for _round in range(2):
            status, detail, _ = _exchange(
                connection, "GET", f"/prompts/{AUTH_PROMPT_ID}", headers={"Cookie": cookie}
            )
            observed.append(status)
            assert status == 200
            assert "prompt詳細".encode() in detail

            status, _, response_headers = _exchange(
                connection, "POST", "/trace-jobs", headers=form_headers, body=post_body
            )
            observed.append(status)
            assert status == 303
            location = response_headers["Location"]

            target = ""
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status, _, job_headers = _exchange(
                    connection, "GET", location, headers={"Cookie": cookie}
                )
                observed.append(status)
                assert status == 200
                refresh = job_headers.get("Refresh", "")
                if refresh.startswith("0; url="):
                    target = refresh[len("0; url=") :]
                    break
                assert refresh == "1"
                time.sleep(0.01)
            assert target.startswith(f"/traces/{AUTH_SESSION_ID}.html")

            # The fragment is browser-side only and must not reach the request line.
            status, trace, _ = _exchange(
                connection,
                "GET",
                target.split("#", 1)[0],
                headers={"Cookie": cookie},
            )
            observed.append(status)
            assert status == 200
            assert b"generated trace" in trace
    finally:
        connection.close()
    assert 501 not in observed


def test_consecutive_posts_on_one_connection_each_parse_their_own_body(auth_server):
    """A cached body would make the second POST act on the first one's fields."""
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    _install_trace_generator(dashboard)
    form_headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    fragments: list[str] = []
    connection = _keepalive(dashboard.port)
    try:
        for prompt_id in (AUTH_PROMPT_ID, SECOND_PROMPT_ID):
            body = (
                f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={prompt_id}"
            ).encode()
            status, _, response_headers = _exchange(
                connection, "POST", "/trace-jobs", headers=form_headers, body=body
            )
            assert status == 303
            job_id = response_headers["Location"].rsplit("/", 1)[1]
            job = dashboard.trace_jobs.get(job_id)
            assert job is not None
            fragments.append(job.fragment)
            _await_job_settled(dashboard, job_id)
    finally:
        connection.close()
    assert fragments == [f"#prompt={AUTH_PROMPT_ID}", f"#prompt={SECOND_PROMPT_ID}"]


@pytest.mark.parametrize(
    "case,expected",
    [
        ("origin", 403),
        ("cookie", 401),
        ("path", 404),
        ("csrf", 403),
    ],
)
def test_early_return_paths_leave_the_connection_parseable(auth_server, case, expected):
    """Every response that returns before reading the body must still drain it."""
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    form_headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = (
        f"csrf_token={csrf}&session_id={AUTH_SESSION_ID}&prompt_id={AUTH_PROMPT_ID}"
    ).encode()
    path = "/trace-jobs"
    if case == "origin":
        headers = {**form_headers, "Origin": "http://evil.example"}
    elif case == "cookie":
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    elif case == "path":
        path = "/not-trace-jobs"
        headers = dict(form_headers)
    else:
        # The token arrives in the header, so the CSRF check never reads the form
        # body -- this is the stale-token path that leaves bytes in the socket.
        headers = {**form_headers, "X-CSRF-Token": "stale-token"}

    connection = _keepalive(dashboard.port)
    try:
        status, _, _ = _exchange(connection, "POST", path, headers=headers, body=body)
        assert status == expected
        # The next request on the same connection must be parsed as a request, not
        # as the leftover bytes of the previous body.
        status, detail, _ = _exchange(
            connection, "GET", f"/prompts/{AUTH_PROMPT_ID}", headers={"Cookie": cookie}
        )
        assert status == 200
        assert "prompt詳細".encode() in detail
    finally:
        connection.close()


def test_oversized_declared_content_length_is_never_read_and_closes_the_connection(
    auth_server,
):
    """A hostile Content-Length must not be drained, and must not be left reusable."""
    dashboard, _, _, _ = auth_server
    _, cookie, csrf = _authenticated_dashboard(dashboard)
    declared = 128 * MAX_BODY_BYTES
    connection = _keepalive(dashboard.port)
    started = time.monotonic()
    try:
        connection.putrequest("POST", "/trace-jobs", skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{server.LOOPBACK_HOST}:{dashboard.port}")
        connection.putheader("Cookie", cookie)
        connection.putheader("X-CSRF-Token", csrf)
        connection.putheader("Content-Type", "application/x-www-form-urlencoded")
        connection.putheader("Content-Length", str(declared))
        connection.endheaders()
        # Only a token fragment of the promised body is ever sent.
        connection.send(b"x" * 16)
        response = connection.getresponse()
        response.read()
        elapsed = time.monotonic() - started
        assert response.status == 204
        # The server must neither wait for nor read the bytes it was promised.
        assert elapsed < 2
        assert response.getheader("Connection") == "close"
        assert response.will_close is True
        # A desynced connection must not survive for the next request to inherit.
        assert connection.sock is None
    finally:
        connection.close()
