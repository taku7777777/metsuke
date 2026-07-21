from __future__ import annotations

import ast
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from metsuke import cli, config
from metsuke.dashboard import server


def _request(
    port: int,
    path: str = "/healthz",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _wait_until_healthy(port: int, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _request(port)[:2] == (200, b"ok"):
                return
        except OSError:
            pass
        time.sleep(0.01)
    raise AssertionError("dashboard server did not become healthy")


@pytest.fixture
def running_server(tmp_path):
    state_path = tmp_path / "state" / "dashboard-state.json"
    dashboard = server.create_server(port=0, state_path=state_path)
    thread = threading.Thread(target=dashboard.serve_forever)
    thread.start()
    try:
        _wait_until_healthy(dashboard.port)
        yield dashboard, state_path
    finally:
        dashboard.shutdown()
        thread.join(timeout=3)
        dashboard.close_lifecycle()
        assert not thread.is_alive()


def _lan_ip() -> str | None:
    candidates = {
        item[4][0]
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        if item[4][0] != "127.0.0.1"
    }
    if candidates:
        return sorted(candidates)[0]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return address if address != "127.0.0.1" else None


def test_server_accepts_loopback_and_rejects_lan_address(running_server):
    dashboard, _ = running_server
    assert _request(dashboard.port)[:2] == (200, b"ok")

    address = _lan_ip()
    if address is None:
        pytest.skip("no non-loopback IPv4 address is available for the LAN bind test")
    with pytest.raises(OSError):
        socket.create_connection((address, dashboard.port), timeout=0.3)


def test_health_is_minimal_and_unknown_paths_are_404(running_server):
    dashboard, _ = running_server
    status, body, headers = _request(dashboard.port)
    assert status == 200
    assert body == b"ok"
    lowered = body.lower()
    for forbidden in (b"version", b"python", b"sqlite", b"ledger", b".metsuke", b"/"):
        assert forbidden not in lowered
    assert "Server" not in headers
    assert "Date" not in headers

    status, body, _ = _request(dashboard.port, "/missing")
    assert status == 404
    assert "dashboardへ戻る".encode() in body


def test_security_headers_are_present_and_cors_is_absent(running_server):
    dashboard, _ = running_server
    _, _, headers = _request(dashboard.port)
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    assert not any(name.lower().startswith("access-control-") for name in headers)

    status, stylesheet, style_headers = _request(dashboard.port, "/dashboard.css")
    assert status == 200
    assert b"focus-visible" in stylesheet
    assert b"prefers-reduced-motion" in stylesheet
    assert style_headers["Content-Type"] == "text/css; charset=utf-8"
    assert not any(name.lower().startswith("access-control-") for name in style_headers)


def test_port_conflict_fails_without_contacting_other_listener(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((server.LOOPBACK_HOST, 0))
    listener.listen()
    listener.settimeout(0.25)
    port = listener.getsockname()[1]
    state_path = tmp_path / "state" / "dashboard-state.json"
    try:
        with pytest.raises(server.DashboardPortInUseError) as caught:
            server.create_server(port=port, state_path=state_path)
        assert "metsuke doctor" in str(caught.value)
        with pytest.raises(socket.timeout):
            listener.accept()
        assert not state_path.exists()
    finally:
        listener.close()


def test_single_instance_blocks_second_server_and_state_is_minimal(running_server):
    dashboard, state_path = running_server
    status = server.server_status(state_path)
    assert status.running is True
    assert status.stale is False
    assert status.state == dashboard.instance_state
    with pytest.raises(server.DashboardAlreadyRunningError):
        server.create_server(port=0, state_path=state_path)

    raw = json.loads(state_path.read_text())
    assert set(raw) == {"pid", "process_start_time", "port", "server_instance_id"}
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert not ({"cookie", "nonce", "url"} & set(raw))


@pytest.mark.parametrize("stale_kind", ["dead_pid", "start_mismatch", "health_missing"])
def test_stale_state_does_not_block_startup(tmp_path, stale_kind):
    state_path = tmp_path / "state" / "dashboard-state.json"
    state_path.parent.mkdir()
    current_start = server._process_start_time(os.getpid())
    assert current_start is not None
    stale = {
        "pid": 999_999_999 if stale_kind == "dead_pid" else os.getpid(),
        "process_start_time": (
            "definitely-not-the-current-start" if stale_kind == "start_mismatch" else current_start
        ),
        "port": 9,
        "server_instance_id": f"stale-{stale_kind}",
    }
    state_path.write_text(json.dumps(stale))
    assert server.server_status(state_path).stale is True

    dashboard = server.create_server(port=0, state_path=state_path)
    try:
        assert dashboard.instance_state is not None
        assert dashboard.instance_state.server_instance_id != stale["server_instance_id"]
    finally:
        dashboard.close_lifecycle()


def test_access_log_does_not_record_path_or_query(running_server, capsys, caplog):
    dashboard, _ = running_server
    secret_path = "/missing/private-ledger?token=do-not-log"
    assert _request(dashboard.port, secret_path, {"Cookie": "session=cookie-do-not-log"})[0] == 404
    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert secret_path not in combined
    assert "do-not-log" not in combined
    assert "private-ledger" not in combined
    assert "cookie-do-not-log" not in combined


def test_dashboard_package_has_no_external_network_client_imports():
    """Allow the loopback server/probe and URL parser; reject external client stacks."""
    root = Path(__file__).parents[1] / "src" / "metsuke" / "dashboard"
    forbidden_roots = {
        "aiohttp",
        "anthropic",
        "ftplib",
        "httpx",
        "openai",
        "requests",
        "smtplib",
        "telnetlib",
        "websocket",
        "websockets",
    }
    loopback_only = {"http.server", "socket"}
    non_network_url_parser = {"urllib.parse"}
    seen_loopback = set()
    for path in root.glob("*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                root_name = module.split(".")[0]
                assert root_name not in forbidden_roots, f"external network import: {module}"
                if root_name == "urllib" and module not in non_network_url_parser:
                    raise AssertionError(f"unapproved urllib import: {module}")
                if module in loopback_only:
                    assert path.name == "server.py"
                    seen_loopback.add(module)
                    assert "deliberately the only network-capable imports" in source
                elif root_name in {"http", "socket"}:
                    raise AssertionError(f"unapproved network import: {module}")
    assert seen_loopback == loopback_only


def test_explicit_stop_terminates_server_and_removes_state(tmp_path):
    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind((server.LOOPBACK_HOST, 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    state_path = tmp_path / "state" / "dashboard-state.json"
    script = (
        "from pathlib import Path; "
        "from metsuke.dashboard.server import serve; "
        f"serve(port={port}, state_path=Path({str(state_path)!r}))"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        _wait_until_healthy(port)
        assert server.stop(state_path) is True
        assert process.wait(timeout=5) == 0
        assert not state_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_dashboard_port_uses_central_config_allowlist(monkeypatch, tmp_path):
    assert "METSUKE_DASHBOARD_PORT" in config.CONFIG_KEYS
    monkeypatch.setenv("METSUKE_CONFIG", str(tmp_path / "missing-config.env"))
    monkeypatch.delenv("METSUKE_DASHBOARD_PORT", raising=False)
    assert config.dashboard_port() == 48127
    monkeypatch.setenv("METSUKE_DASHBOARD_PORT", "49001")
    assert config.dashboard_port() == 49001


def test_dashboard_cli_uses_nested_maintenance_subcommands(monkeypatch, capsys):
    served = []
    monkeypatch.setattr(server, "serve", lambda **kwargs: served.append(kwargs))
    assert cli.main(["dashboard", "serve"]) == 0
    assert served == [{}]

    monkeypatch.setattr(
        server,
        "server_status",
        lambda: server.ServerStatus(state=None, running=False, stale=False),
    )
    assert cli.main(["dashboard", "status"]) == 1
    assert capsys.readouterr().out.strip() == "stopped"

    monkeypatch.setattr(server, "stop", lambda: True)
    assert cli.main(["dashboard", "stop"]) == 0
    assert capsys.readouterr().out.strip() == "stopping"
