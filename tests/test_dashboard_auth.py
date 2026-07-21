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

from metsuke import ledger
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
    dashboard.trace_jobs.opener = lambda *args: opened.append(args) or True
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
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status, page, response_headers = _request(
            dashboard.port, location, headers={"Cookie": cookie}
        )
        if "別タブで開きました".encode() in page:
            break
        assert dict(response_headers).get("Refresh") == "1"
        time.sleep(0.01)
    assert status == 200
    assert "別タブで開きました".encode() in page
    assert b"file:" not in page and b"/traces/" not in page
    assert opened == [
        (
            dashboard.trace_jobs.cache.path_for(AUTH_SESSION_ID),
            f"#prompt={AUTH_PROMPT_ID}",
        )
    ]


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
