import json
import subprocess
import sys
import time

from metsuke import archiver, cli, ingest, ledger, state, trace_html


def test_state_stale_atomic_and_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    monkeypatch.setenv("METSUKE_RECEIPT_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("METSUKE_PROMPT_CRIT_USD", "0")
    conn = ledger.connect()
    now = time.time()
    conn.execute("INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p','s',?,'hello')", (now - 30,))
    conn.execute("INSERT INTO request(request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source) VALUES ('r','s','s','p',?,'claude-fable-5',0,25000,0,0,0,0,'transcript')", (now - 20,))
    conn.execute("INSERT INTO hook_event VALUES (?,?,?,?,?)", (now + 1000, "statusline_sample", "s", None, "{}"))
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    result = state.write(conn, notify=True)
    assert result["stale"] is True and len(calls) == 1
    assert json.loads((tmp_path / "state.json").read_text())["last_prompt"]["prompt_id"] == "p"
    state.write(conn, notify=True)
    assert len(calls) == 1
    conn.execute("DELETE FROM meta WHERE key='receipts_notified'")
    state.write(conn, notify=False)
    assert len(calls) == 1
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    conn.close()


def test_state_includes_coldcache_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    monkeypatch.setattr(state.config, "COLDCACHE_MIN_USD", 1.75)
    conn = ledger.connect()
    assert state.build(conn)["thresholds"] == {"coldcache_min_usd": 1.75}
    conn.close()


def test_sync_is_fail_open_on_lock_contention(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    called = False

    def unexpected_ingest(conn):
        nonlocal called
        called = True

    monkeypatch.setattr(ingest, "run", unexpected_ingest)
    lock_path = tmp_path / "state/sync.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,pathlib,sys; "
            "pathlib.Path(sys.argv[1]).parent.mkdir(parents=True,exist_ok=True); "
            "handle=open(sys.argv[1],'a+'); fcntl.flock(handle,fcntl.LOCK_EX); "
            "print('locked',flush=True); sys.stdin.read()",
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert cli.main(["sync", "--quiet"]) == 0
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
    assert called is False


def test_sync_is_fail_open_on_ingest_error(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    monkeypatch.setattr(archiver, "run", lambda: None)
    monkeypatch.setattr(ingest, "run", lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["sync", "--quiet"]) == 0
    marker = json.loads((tmp_path / "state/last_sync_error.json").read_text())
    assert "RuntimeError: boom" in marker["error"]


def test_state_detects_each_missing_source_and_persisted_sync_error(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    conn.execute(
        "INSERT INTO ingest_log VALUES (?,?,?,?,?,?)", (now, 0, 0, 0, 0, 8)
    )
    _request(conn, "active-request", "s", now - 10)
    result = state.build(conn)
    assert result["stale"] is True
    assert "hooks_missing_during_request_activity" in result["health"]["stale_reasons"]

    conn.execute("DELETE FROM request")
    _hook(conn, now - 5, "SessionStart", "hook-only")
    result = state.build(conn)
    assert "ledger_missing_during_hook_activity" in result["health"]["stale_reasons"]

    conn.execute("DELETE FROM hook_event")
    _request(conn, "old-request", "old", now - 7200)
    _hook(conn, now - 7100, "Stop", "old")
    result = state.build(conn)
    assert result["stale"] is False

    path = tmp_path / "state/last_sync_error.json"
    path.write_text(json.dumps({"ts": now, "error": "boom"}))
    result = state.build(conn)
    assert result["stale"] is True
    assert result["health"]["last_sync_error"]["error"] == "boom"
    conn.close()


def _request(conn, rid, sid, ts, model="claude-fable-5", context=100_000, cache_w1h=0):
    conn.execute(
        """INSERT INTO request(request_id,session_id,lineage_id,ts,model,input_tok,output_tok,
           cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES (?,?,?,?,?,?,0,0,0,?,0,'transcript')""",
        (rid, sid, sid, ts, model, context, cache_w1h),
    )


def _hook(conn, ts, kind, sid, cost=None):
    payload = {"metsuke_event": kind, "metsuke_ts": ts, "payload": {"session_id": sid}}
    if cost is not None:
        payload["payload"]["cost"] = {"total_cost_usd": cost}
    conn.execute("INSERT INTO hook_event VALUES (?,?,?,?,?)", (ts, kind, sid, None, json.dumps(payload)))


def _prompt_request(conn, prompt_id, request_id, sid, ts, output_tok=100, interrupted=None):
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text,interrupted_message_id) VALUES (?,?,?,'hello',?)",
        (prompt_id, sid, ts, interrupted),
    )
    conn.execute(
        """INSERT INTO request(request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
           cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,?,?,?,?,'claude-fable-5',0,?,0,0,0,0,?,'transcript')""",
        (request_id, sid, sid, prompt_id, ts, output_tok, int(interrupted is not None)),
    )


def test_session_recent_completed_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _prompt_request(conn, "previous", "r-previous", "active", now - 100, output_tok=100)
    _prompt_request(conn, "current", "r-current", "active", now - 9, output_tok=200)
    _hook(conn, now - 10, "UserPromptSubmit", "active")
    active = state.build(conn)["sessions"]["active"]
    previous_cost = conn.execute("SELECT cost_usd FROM v_prompt_cost WHERE prompt_id='previous'").fetchone()[0]
    assert active["recent_prompts"] == [
        {
            "prompt_id": "previous",
            "cost_usd": previous_cost,
            "interrupted": False,
            "completed_ts": now - 100,
        }
    ]

    _prompt_request(conn, "latest", "r-latest", "complete", now - 5, output_tok=300, interrupted="missing")
    complete = state.build(conn)["sessions"]["complete"]
    latest_cost = conn.execute("SELECT cost_usd FROM v_prompt_cost WHERE prompt_id='latest'").fetchone()[0]
    assert complete["recent_prompts"] == [
        {
            "prompt_id": "latest",
            "cost_usd": latest_cost,
            "interrupted": True,
            "completed_ts": now - 5,
        }
    ]

    _request(conn, "no-prompt", "empty", now - 1)
    empty = state.build(conn)["sessions"]["empty"]
    assert empty["recent_prompts"] == []
    conn.close()


def test_session_recent_prompts_are_newest_first_and_limited_to_three(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    for i in range(4):
        _prompt_request(conn, f"p{i}", f"r{i}", "s", now - 40 + i * 10, output_tok=(i + 1) * 100)
    costs = {
        row["prompt_id"]: row["cost_usd"]
        for row in conn.execute("SELECT prompt_id,cost_usd FROM v_prompt_cost")
    }
    assert state.build(conn)["sessions"]["s"]["recent_prompts"] == [
        {"prompt_id": "p3", "cost_usd": costs["p3"], "interrupted": False, "completed_ts": now - 10},
        {"prompt_id": "p2", "cost_usd": costs["p2"], "interrupted": False, "completed_ts": now - 20},
        {"prompt_id": "p1", "cost_usd": costs["p1"], "interrupted": False, "completed_ts": now - 30},
    ]
    conn.close()


def test_prepare_prompt_details_for_recent_costly_session(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    monkeypatch.setenv("METSUKE_PROMPT_WARN_USD", "3")
    conn = ledger.connect()
    calls = []

    def generate(sid, focus=None, *, conn=None, record=True):
        calls.append((sid, record))
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
                        "prompt_id": "prompt/one",
                        "cost_usd": 3.0,
                        "interrupted": False,
                        "completed_ts": time.time(),
                    },
                    {
                        "prompt_id": "prompt-low",
                        "cost_usd": 2.99,
                        "interrupted": False,
                        "completed_ts": time.time(),
                    },
                ],
            }
        }
    }
    state._prepare_prompt_details(conn, result)
    prompt = result["sessions"]["s1"]["recent_prompts"][0]
    assert calls == [("s1", False)]
    assert prompt["detail_url"].endswith("/s1.html#prompt=prompt%2Fone")
    assert "detail_url" not in result["sessions"]["s1"]["recent_prompts"][1]
    conn.close()


def test_inflight_and_rebuild_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _request(conn, "r1", "s1", now - 5, context=100_000)
    _hook(conn, now - 30, "statusline_sample", "s1", 4.0)
    _hook(conn, now - 20, "UserPromptSubmit", "s1")
    _hook(conn, now - 1, "statusline_sample", "s1", 9.2)
    got = state.build(conn)["sessions"]["s1"]
    assert got["inflight_usd"] == 5.2
    assert got["rebuild_cost_usd"] == 2.0
    _hook(conn, now, "Stop", "s1")
    assert state.build(conn)["sessions"]["s1"]["inflight_usd"] is None
    conn.execute("DELETE FROM hook_event WHERE kind='Stop'")
    conn.execute("UPDATE hook_event SET payload_json=replace(payload_json, '9.2', '3.0') WHERE ts>?", (now - 2,))
    assert state.build(conn)["sessions"]["s1"]["inflight_usd"] is None
    conn.close()


def test_notify_passes_unicode_as_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(state.subprocess, "run", run)
    status = state._notify("日本語タイトル", "🚨 進行中プロンプト")

    command, kwargs = calls[0]
    assert command[0:2] == ["osascript", "-e"]
    assert command[-2:] == ["日本語タイトル", "🚨 進行中プロンプト"]
    assert "\\u" not in command[2]
    assert kwargs["text"] is True
    assert status == {"macos": "accepted", "ntfy": "not_configured"}


def test_notify_reports_osascript_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(state.subprocess, "run", run)
    status = state._notify("title", "message")

    assert status["macos"] == "failed"
    assert "permission denied" in capsys.readouterr().err


def test_notify_test_command_requires_macos_acceptance(monkeypatch, capsys):
    monkeypatch.setattr(
        state,
        "_notify",
        lambda title, msg: {"macos": "accepted", "ntfy": "not_configured"},
    )
    assert cli.main(["notify-test"]) == 0
    output = capsys.readouterr().out
    assert '"macos": "accepted"' in output
    assert "目視" not in output
    assert "表示されたか確認" in output

    monkeypatch.setattr(
        state,
        "_notify",
        lambda title, msg: {"macos": "failed", "ntfy": "not_configured"},
    )
    assert cli.main(["notify-test"]) == 1


def test_runaway_notify_once_ntfy_and_notify_false(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _request(conn, "r1", "s1", now - 5)
    _hook(conn, now - 30, "statusline_sample", "s1", 4.0)
    _hook(conn, now - 20, "UserPromptSubmit", "s1")
    _hook(conn, now - 1, "statusline_sample", "s1", 9.2)
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    state.write(conn, notify=False)
    assert calls == []
    (tmp_path / "state/ntfy.url").write_text("https://ntfy.example/topic\n")
    state.write(conn, notify=True)
    assert sum(c[0] == "osascript" for c in calls) == 1
    assert sum(c[0] == "curl" for c in calls) == 1
    assert len(list((tmp_path / "spool/hooks").glob("*-nudge-runaway_guard.ndjson"))) == 1
    state.write(conn, notify=True)
    assert len(calls) == 2
    conn.close()


def test_ttl_prenotify_window_and_inflight_exclusion(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _request(conn, "r1", "ttl", now - 3100, context=100_000, cache_w1h=100_000)
    _request(conn, "r2", "active", now - 3100, context=100_000, cache_w1h=100_000)
    _hook(conn, now - 100, "UserPromptSubmit", "active")
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    state.write(conn, notify=True)
    assert len(calls) == 1 and calls[0][0] == "osascript"
    files = list((tmp_path / "spool/hooks").glob("*-nudge-ttl_prenotify.ndjson"))
    assert len(files) == 1 and json.loads(files[0].read_text())["payload"]["session_id"] == "ttl"
    conn.close()


def test_runaway_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    for i in range(4):
        sid = f"s{i}"
        _request(conn, f"r{i}", sid, now - 5)
        _hook(conn, now - 30, "statusline_sample", sid, 0.0)
        _hook(conn, now - 20, "UserPromptSubmit", sid)
        _hook(conn, now - 1, "statusline_sample", sid, 6.0)
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    state.write(conn, notify=True)
    assert len(calls) == 3
    assert len(list((tmp_path / "spool/hooks").glob("*-nudge-runaway_guard.ndjson"))) == 3
    conn.close()
