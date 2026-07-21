"""P6: the Metsuke.app entry point -- reuse-or-start, and every way it can fail.

Every test binds an ephemeral port and writes under tmp_path, so none of them can
reach the operator's installed dashboard, real ~/.metsuke, or ~/Applications.
"""

from __future__ import annotations

import functools
import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from metsuke import cli, doctor, ledger, state, trace_html
from metsuke.dashboard import launcher, server


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((server.LOOPBACK_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(server.LOOPBACK_HOST, port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _bootstrap_path(url: str) -> str:
    return "/" + url.split(f"{server.LOOPBACK_HOST}:", 1)[1].split("/", 1)[1]


def _shutdown(state_path: Path, timeout: float = 5.0) -> None:
    server.stop(state_path)
    deadline = time.monotonic() + timeout
    while state_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def dashboard_port(monkeypatch):
    port = _free_port()
    monkeypatch.setenv("METSUKE_DASHBOARD_PORT", str(port))
    return port


@pytest.fixture
def in_process_server(tmp_path):
    """A healthy server owned by this process, as `dashboard open` will find it."""

    state_path = tmp_path / "state" / "dashboard-state.json"
    dashboard = server.create_server(port=0, state_path=state_path)
    thread = threading.Thread(target=dashboard.serve_forever)
    thread.start()
    try:
        yield dashboard, state_path
    finally:
        dashboard.shutdown()
        thread.join(timeout=3)
        dashboard.close_lifecycle()


def test_open_without_a_server_starts_one_and_the_nonce_is_accepted(tmp_path, dashboard_port):
    state_path = tmp_path / "state" / "dashboard-state.json"
    opened: list[str] = []
    result = launcher.open_dashboard(
        state_path=state_path,
        opener=opened.append,
        syncer=None,
    )
    try:
        assert result.reused is False
        assert result.port == dashboard_port
        assert opened == [result.url]
        assert result.url.startswith(
            f"http://{server.LOOPBACK_HOST}:{dashboard_port}/bootstrap?nonce="
        )
        status, headers, _ = _get(result.port, _bootstrap_path(result.url))
        assert status == 303
        assert headers["Location"] == "/dashboard"
        assert "metsuke_dashboard=" in headers["Set-Cookie"]
    finally:
        _shutdown(state_path)


def test_open_reuses_a_healthy_server_instead_of_starting_a_second_one(in_process_server):
    dashboard, state_path = in_process_server
    before = json.loads(state_path.read_text())
    started: list[object] = []

    def refuse_to_start(*args):
        started.append(args)
        raise AssertionError("a healthy server must be reused, not restarted")

    opened: list[str] = []
    result = launcher.open_dashboard(
        state_path=state_path,
        opener=opened.append,
        starter=refuse_to_start,
        syncer=None,
    )

    assert started == [], "no second server process may be spawned"
    assert result.reused is True
    assert result.port == dashboard.port
    assert json.loads(state_path.read_text()) == before, "reuse must not rewrite state"
    # The nonce was minted outside the server, from the 0600 secret plus the
    # running instance id. The running server must still accept it.
    status, headers, _ = _get(result.port, _bootstrap_path(opened[0]))
    assert status == 303
    assert "metsuke_dashboard=" in headers["Set-Cookie"]


@pytest.mark.parametrize("stale_kind", ["dead_pid", "start_mismatch"])
def test_open_recovers_from_stale_state(tmp_path, dashboard_port, stale_kind):
    state_path = tmp_path / "state" / "dashboard-state.json"
    state_path.parent.mkdir(parents=True)
    current_start = server._process_start_time(os.getpid())
    assert current_start is not None
    stale = {
        "pid": 999_999_999 if stale_kind == "dead_pid" else os.getpid(),
        "process_start_time": (
            "definitely-not-the-current-start" if stale_kind == "start_mismatch" else current_start
        ),
        "port": _free_port(),
        "server_instance_id": f"stale-{stale_kind}",
    }
    state_path.write_text(json.dumps(stale))
    assert server.server_status(state_path).stale is True

    opened: list[str] = []
    result = launcher.open_dashboard(state_path=state_path, opener=opened.append, syncer=None)
    try:
        assert result.reused is False
        assert result.port == dashboard_port
        live = json.loads(state_path.read_text())
        assert live["server_instance_id"] != stale["server_instance_id"]
        assert _get(result.port, _bootstrap_path(result.url))[0] == 303
    finally:
        _shutdown(state_path)


def test_open_reports_a_port_conflict_without_a_traceback(tmp_path, monkeypatch, capsys):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((server.LOOPBACK_HOST, 0))
    listener.listen()
    listener.settimeout(0.25)
    port = listener.getsockname()[1]
    monkeypatch.setenv("METSUKE_DASHBOARD_PORT", str(port))
    state_path = tmp_path / "state" / "dashboard-state.json"
    try:
        with pytest.raises(launcher.DashboardLaunchError) as caught:
            launcher.open_dashboard(
                state_path=state_path,
                opener=lambda url: pytest.fail("the browser must not open on failure"),
                syncer=None,
            )
        message = str(caught.value)
        assert "could not start" in message and "metsuke doctor" in message
        assert "Traceback" not in message
        # The conflicting listener is never spoken to.
        with pytest.raises(socket.timeout):
            listener.accept()
        assert not state_path.exists()
    finally:
        listener.close()

    monkeypatch.setattr(
        launcher,
        "open_dashboard",
        lambda: (_ for _ in ()).throw(launcher.DashboardLaunchError("the dashboard could not start")),
    )
    assert cli.main(["dashboard", "open"]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "the dashboard could not start"
    assert "Traceback" not in captured.err


def test_dashboard_open_command_reports_the_reused_address_without_the_nonce(
    in_process_server, monkeypatch, capsys
):
    """The command Metsuke.app runs: exit 0, and no one-shot nonce in stdout."""

    dashboard, state_path = in_process_server
    opened: list[str] = []
    # The real launcher runs; only the paths it would take from the operator's
    # installation are redirected at the temporary server.
    real_open = launcher.open_dashboard
    monkeypatch.setattr(
        launcher,
        "open_dashboard",
        functools.partial(
            real_open,
            state_path=state_path,
            opener=opened.append,
            starter=lambda *args: pytest.fail("healthy server must be reused"),
            syncer=None,
        ),
    )
    assert cli.main(["dashboard", "open"]) == 0
    assert opened and "nonce=" in opened[0]
    output = capsys.readouterr().out
    assert output.strip() == (
        f"reusing dashboard at http://{server.LOOPBACK_HOST}:{dashboard.port}/dashboard"
    )
    assert "nonce" not in output


def test_a_nonce_for_a_stale_instance_is_refused_clearly(in_process_server):
    dashboard, state_path = in_process_server
    stale_state = server.ServerState(
        pid=dashboard.instance_state.pid,
        process_start_time=dashboard.instance_state.process_start_time,
        port=dashboard.port,
        server_instance_id="an-instance-that-restarted-away",
    )
    # The launcher notices the restart itself rather than leaving the browser
    # with an unexplained 401.
    with pytest.raises(launcher.DashboardLaunchError) as caught:
        launcher.bootstrap_url(state_path, stale_state)
    assert "restarted" in str(caught.value)
    assert "Traceback" not in str(caught.value)

    # And if such a nonce does reach the server, the refusal is a plain page.
    nonce = launcher.mint_bootstrap_nonce(state_path, "an-instance-that-restarted-away")
    status, headers, body = _get(dashboard.port, f"/bootstrap?nonce={nonce}")
    assert status == 401
    assert "Set-Cookie" not in headers
    assert body.decode() == "Metsuke.appから開き直してください"


def test_open_does_not_wait_for_sync(in_process_server):
    """A slow sync must never be why the Dock icon looks dead."""

    dashboard, state_path = in_process_server
    order: list[str] = []

    def slow_sync(_log_path):
        order.append("sync-started")

    result = launcher.open_dashboard(
        state_path=state_path,
        opener=lambda url: order.append("browser"),
        starter=lambda *args: pytest.fail("healthy server must be reused"),
        syncer=slow_sync,
    )
    assert result.reused is True
    # The sync is handed off before the browser opens and is never waited on:
    # start_background_sync spawns a detached process and returns.
    assert order == ["sync-started", "browser"]


def test_doctor_reports_dashboard_and_app_without_starting_a_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / ".metsuke"))
    state_path = Path(str(tmp_path / ".metsuke" / "state" / "dashboard-state.json"))
    state_path.parent.mkdir(parents=True)
    (state_path.parent / "dashboard-secret").write_bytes(b"s" * 32)
    os.chmod(state_path.parent / "dashboard-secret", 0o600)
    bundle = tmp_path / "Applications" / "Metsuke.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    moved_away = tmp_path / "moved-checkout" / ".venv" / "bin" / "metsuke"
    (bundle / "Metsuke").write_text(
        f"#!/bin/bash\n# metsuke-target: {moved_away}\nexec \"{moved_away}\" dashboard open\n"
    )

    items = []
    doctor._dashboard(items)
    doctor._app(items)
    reported = {item["check_name"]: item for item in items}
    assert reported["dashboard_server"]["value"] == "stopped"
    assert reported["dashboard_auth_secret"]["status"] == "ok"
    # A renamed or moved checkout leaves the bundle pointing at nothing.
    assert reported["metsuke_app"]["status"] == "fail"
    assert "no longer exists" in reported["metsuke_app"]["detail"]
    assert not state_path.exists(), "doctor must never start a dashboard server"

    os.chmod(state_path.parent / "dashboard-secret", 0o644)
    loose = []
    doctor._dashboard(loose)
    assert {item["check_name"]: item["status"] for item in loose}["dashboard_auth_secret"] == "fail"


def test_statusline_prompt_link_stays_a_file_url(tmp_path, monkeypatch):
    """P6 adds an app entry point; the statusline keeps its file:// affordance."""

    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    monkeypatch.setenv("METSUKE_PROMPT_WARN_USD", "3")
    conn = ledger.connect()

    def generate(sid, focus=None, *, conn=None, record=True):
        path = tmp_path / "traces" / f"{sid}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("detail")
        return path

    monkeypatch.setattr(trace_html, "generate", generate)
    result = {
        "sessions": {
            "s1": {
                "last_ts": time.time(),
                "recent_prompts": [
                    {
                        "prompt_id": "prompt-one",
                        "cost_usd": 3.0,
                        "interrupted": False,
                        "completed_ts": time.time(),
                    }
                ],
            }
        }
    }
    state._prepare_prompt_details(conn, result)
    conn.close()
    detail_url = result["sessions"]["s1"]["recent_prompts"][0]["detail_url"]
    assert detail_url.startswith("file://")
    assert detail_url.endswith("/s1.html#prompt=prompt-one")
    assert "127.0.0.1" not in detail_url and "http://" not in detail_url


# ---------------------------------------------------------------------------
# Environment-controlled regression guards.
#
# Every test above this line runs in pytest's inherited environment, which is why
# 410 of them passed while a Dock launch of Metsuke.app failed outright: `ps -o
# lstart=` formats its date per the caller's locale, and a GUI launch inherits no
# LANG at all. These tests set (or withhold) the environment explicitly instead.
# ---------------------------------------------------------------------------

JAPANESE_LOCALE = "ja_JP.UTF-8"


def _use_locale(monkeypatch, locale: str | None) -> None:
    for name in ("LANG", "LC_ALL", "LC_TIME"):
        if locale is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, locale)


def test_process_start_time_does_not_depend_on_the_caller_locale(monkeypatch):
    """The stored identity of a process must not encode who asked for it."""

    pid = os.getpid()
    _use_locale(monkeypatch, JAPANESE_LOCALE)
    japanese = server._process_start_time(pid)
    _use_locale(monkeypatch, None)
    unset = server._process_start_time(pid)
    _use_locale(monkeypatch, "C")
    c_locale = server._process_start_time(pid)

    assert japanese is not None
    assert unset is not None
    assert c_locale is not None
    # A Dock launch gets the unset form; the shell that wrote the state file got
    # the ja_JP form. Same pid, same instant, so they must be the same string.
    assert japanese == unset == c_locale


def test_state_written_under_one_locale_is_still_running_under_another(
    in_process_server, monkeypatch
):
    """A state file is read by processes whose environment nobody controls."""

    dashboard, state_path = in_process_server
    _use_locale(monkeypatch, JAPANESE_LOCALE)
    written_by_japanese_shell = server._process_start_time(os.getpid())
    assert written_by_japanese_shell is not None
    payload = json.loads(state_path.read_text())
    payload["process_start_time"] = written_by_japanese_shell
    state_path.write_text(json.dumps(payload))

    # Now read it the way a Dock or Spotlight launch would: no locale at all.
    _use_locale(monkeypatch, None)
    status = server.server_status(state_path)

    assert status.running is True, "a healthy server must not look stale to a GUI launch"
    assert status.stale is False
    assert status.state is not None
    assert status.state.port == dashboard.port


def test_open_reuses_a_healthy_server_when_the_instance_lock_is_held(in_process_server):
    """Defense in depth: never report 'lock is held' while the server answers.

    The state file is deliberately given an unrecognisable process identity, so
    the launcher takes the start path, spawns a real child, and that child loses
    the single-instance lock to the server already running in this process.
    """

    dashboard, state_path = in_process_server
    live = json.loads(state_path.read_text())
    live["process_start_time"] = "identity-detection-failed-somehow"
    state_path.write_text(json.dumps(live))
    assert server.server_status(state_path).running is False

    opened: list[str] = []
    result = launcher.open_dashboard(
        state_path=state_path,
        opener=opened.append,
        syncer=None,
    )

    assert result.reused is True
    assert result.port == dashboard.port
    # The nonce minted during recovery must be honoured by the running server.
    status, headers, _ = _get(result.port, _bootstrap_path(opened[0]))
    assert status == 303
    assert headers["Location"] == "/dashboard"
    assert "metsuke_dashboard=" in headers["Set-Cookie"]


def test_open_still_fails_clearly_when_the_lock_holder_is_not_healthy(tmp_path, monkeypatch):
    """Recovery is not a blanket excuse: an unhealthy lock holder is still a failure."""

    state_path = tmp_path / "state" / "dashboard-state.json"
    state_path.parent.mkdir(parents=True)
    lock_fd = server._acquire_lock(state_path)
    monkeypatch.setenv("METSUKE_DASHBOARD_PORT", str(_free_port()))
    try:
        with pytest.raises(launcher.DashboardLaunchError) as failure:
            launcher.open_dashboard(
                state_path=state_path,
                opener=lambda url: pytest.fail("no browser for a dead dashboard"),
                syncer=None,
            )
    finally:
        server._release_lock(lock_fd)
    assert "could not start" in str(failure.value)


def test_generated_app_launcher_reuses_a_server_from_a_stripped_environment(
    tmp_path, monkeypatch
):
    """The end-to-end guard: run the real bundle the way Dock runs it.

    Dock and Spotlight hand a process HOME and PATH and essentially nothing else
    -- no LANG, no shell exports. Running the generated launcher under an
    inherited pytest environment is exactly the blind spot that let a broken app
    ship past a green suite, so this builds the bundle and executes it with an
    explicit, minimal env.
    """

    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".venv" / "bin" / "metsuke").exists():
        pytest.skip("bundle build requires the project venv entrypoint")

    fake_home = tmp_path / "home"
    metsuke_home = fake_home / ".metsuke"
    metsuke_home.mkdir(parents=True)
    os.chmod(metsuke_home, 0o700)
    empty_source = tmp_path / "claude-projects"
    empty_source.mkdir()
    apps_dir = tmp_path / "Applications"
    dashboard_port = _free_port()
    # Read by the Python side directly; keeps sync, ledger, and state under tmp.
    (metsuke_home / "config.env").write_text(
        f"METSUKE_HOME={metsuke_home}\n"
        f"METSUKE_SOURCE={empty_source}\n"
        f"METSUKE_DASHBOARD_PORT={dashboard_port}\n"
    )

    build = subprocess.run(
        [str(repo / "scripts" / "install-app.sh")],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin",
            "METSUKE_APPS_DIR": str(apps_dir),
            # Never register a throwaway bundle with the real LaunchServices.
            "METSUKE_LSREGISTER": str(tmp_path / "no-lsregister"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    launcher_script = apps_dir / "Metsuke.app" / "Contents" / "MacOS" / "Metsuke"
    assert launcher_script.is_file()

    # A recording stub stands in for the browser: nothing real is ever opened.
    opened_log = tmp_path / "opened.txt"
    browser_stub = tmp_path / "record-open.sh"
    browser_stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{opened_log}"\n')
    os.chmod(browser_stub, 0o755)

    # The state file is written by a server running under a Japanese locale, as
    # the operator's own shell did.
    monkeypatch.setenv("METSUKE_HOME", str(metsuke_home))
    monkeypatch.setenv("METSUKE_CONFIG", str(metsuke_home / "config.env"))
    _use_locale(monkeypatch, JAPANESE_LOCALE)
    state_path = metsuke_home / "state" / "dashboard-state.json"
    running = server.create_server(
        port=0,
        state_path=state_path,
        database_path=tmp_path / "ledger.db",
    )
    thread = threading.Thread(target=running.serve_forever)
    thread.start()
    try:
        assert server.server_status(state_path).running is True
        launched = subprocess.run(
            [str(launcher_script)],
            env={
                "HOME": str(fake_home),
                "PATH": "/usr/bin:/bin",
                "BROWSER": str(browser_stub),
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
        app_log = metsuke_home / "logs" / "metsuke-app.log"
        log_text = app_log.read_text() if app_log.exists() else ""
        assert launched.returncode == 0, log_text or launched.stderr
        assert "lock is held" not in log_text
        assert "Traceback" not in log_text
        assert (
            f"reusing dashboard at http://{server.LOOPBACK_HOST}:{running.port}/dashboard"
            in log_text
        )

        urls = opened_log.read_text().split()
        assert len(urls) == 1, f"expected exactly one browser open, got {urls}"
        assert urls[0].startswith(
            f"http://{server.LOOPBACK_HOST}:{running.port}/bootstrap?nonce="
        )
        # End-to-end: the nonce minted by the GUI-launched process is accepted.
        status, headers, _ = _get(running.port, _bootstrap_path(urls[0]))
        assert status == 303
        assert headers["Location"] == "/dashboard"
    finally:
        running.shutdown()
        thread.join(timeout=5)
        running.close_lifecycle()


# --- stop must reach a server that is answering, whatever its recorded identity ---
#
# The LC_ALL=C fix made _process_start_time locale-independent, which stranded
# every state file written before it: the stored string was locale-formatted, the
# probe now returns the C form, so server_status said "stale" and stop refused --
# leaving a live server that could be neither stopped nor replaced, because it
# still held the single-instance lock.


def _break_recorded_identity(state_path: Path) -> int:
    """Make the recorded process identity unrecognisable; return the recorded pid."""

    payload = json.loads(state_path.read_text())
    payload["process_start_time"] = "火  7/21 20:47:20 2026"
    state_path.write_text(json.dumps(payload))
    return payload["pid"]


@pytest.fixture
def observed_signals(monkeypatch):
    """Record SIGTERMs instead of delivering them -- the recorded pid is this test."""

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, signum: sent.append((pid, signum)))
    return sent


def test_stop_terminates_a_server_that_answers_despite_a_mismatched_identity(
    in_process_server, observed_signals
):
    """The case that stranded the operator: healthy server, unrecognisable identity."""

    dashboard, state_path = in_process_server
    recorded_pid = _break_recorded_identity(state_path)

    status = server.server_status(state_path)
    assert status.running is False, "identity really does not match"
    assert status.serving is True, "but the recorded port is answering"

    assert server.stop(state_path) is True
    assert observed_signals == [(recorded_pid, signal.SIGTERM)]


def test_stop_signals_nothing_when_the_recorded_port_is_silent(tmp_path, observed_signals):
    """Identity mismatch alone never justifies a signal: the port must answer."""

    state_path = tmp_path / "state" / "dashboard-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "process_start_time": "火  7/21 20:47:20 2026",
                "port": _free_port(),  # bound by nobody
                "server_instance_id": "no-server-behind-this-record",
            }
        )
    )

    status = server.server_status(state_path)
    assert status.stale is True
    assert status.serving is False

    assert server.stop(state_path) is False
    assert observed_signals == [], "a pid must never be signalled on identity alone"


def test_status_says_a_server_is_still_answering_rather_than_absent(
    tmp_path, monkeypatch, capsys
):
    """`dashboard status` must not report absence while a server serves traffic."""

    metsuke_home = tmp_path / "home"
    monkeypatch.setenv("METSUKE_HOME", str(metsuke_home))
    state_path = metsuke_home / "state" / "dashboard-state.json"
    dashboard = server.create_server(
        port=0,
        state_path=state_path,
        database_path=tmp_path / "ledger.db",
    )
    thread = threading.Thread(target=dashboard.serve_forever)
    thread.start()
    try:
        _break_recorded_identity(state_path)

        exit_code = cli.main(["dashboard", "status"])
        reported = capsys.readouterr().out.strip()

        # Distinct from both plain "running" and plain "stopped".
        assert reported != "running"
        assert reported != "stopped"
        assert reported.startswith("stale")
        assert str(dashboard.port) in reported
        assert "answering" in reported, f"must not read as absent: {reported!r}"
        # Not a cleanly running server, so exit 0 would be wrong.
        assert exit_code == 1
    finally:
        dashboard.shutdown()
        thread.join(timeout=3)
        dashboard.close_lifecycle()


def test_stop_still_terminates_a_server_whose_identity_matches(
    in_process_server, observed_signals
):
    """Regression guard: the ordinary path is unchanged."""

    dashboard, state_path = in_process_server
    recorded_pid = json.loads(state_path.read_text())["pid"]

    assert server.server_status(state_path).running is True
    assert server.stop(state_path) is True
    assert observed_signals == [(recorded_pid, signal.SIGTERM)]
