import json

import pytest

from metsuke import ingest, ledger


def _event(conn, ts, kind, sid, prompt=""):
    envelope = {
        "metsuke_event": kind,
        "metsuke_ts": ts,
        "payload": {"session_id": sid, "prompt": prompt},
    }
    conn.execute(
        "INSERT INTO hook_event VALUES (?,?,?,?,?)",
        (ts, kind, sid, None, json.dumps(envelope)),
    )


def _nudge_event(conn, fired, rule, sid="s"):
    envelope = {
        "metsuke_event": "nudge_fired",
        "metsuke_ts": fired,
        "payload": {"rule": rule, "session_id": sid, "detail": {"cost_usd": 7}},
    }
    conn.execute(
        "INSERT INTO hook_event VALUES (?,?,?,?,?)",
        (fired, "nudge_fired", sid, None, json.dumps(envelope)),
    )


def _request(conn, rid, sid, ts, model="claude-fable-5", agent=None, interrupted=0):
    conn.execute(
        """INSERT INTO request(request_id,session_id,agent_id,lineage_id,ts,model,input_tok,
           output_tok,cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,?,?,?,?,?,0,0,0,0,0,0,?,'transcript')""",
        (rid, sid, agent, f"{sid}/{agent}" if agent else sid, ts, model, interrupted),
    )


@pytest.mark.parametrize(
    ("rule", "setup", "expected"),
    [
        ("coldcache_warn", lambda c, f: _event(c, f + 10, "UserPromptSubmit", "s", "/handoff now"), 1),
        ("coldcache_warn", lambda c, f: None, None),
        ("coldcache_warn", lambda c, f: _event(c, f + 10, "UserPromptSubmit", "s", "continue"), 0),
        ("ctx_warn", lambda c, f: _event(c, f + 10, "UserPromptSubmit", "s", "/handoff now"), 1),
        ("ctx_warn", lambda c, f: _event(c, f + 10, "UserPromptSubmit", "s", "continue"), 0),
        ("runaway_guard", lambda c, f: _request(c, "i", "s", f + 10, interrupted=1), 1),
        ("runaway_guard", lambda c, f: _request(c, "fan", "s", f + 10, agent="new"), 0),
    ],
)
def test_nudge_predicates_and_logical_time(tmp_path, monkeypatch, rule, setup, expected):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    fired = 1000.0
    _nudge_event(conn, fired, rule)
    setup(conn, fired)
    monkeypatch.setattr(ingest.time, "time", lambda: fired + 601)
    ingest._derive_nudges(conn)
    ingest._derive_nudges(conn)
    row = conn.execute("SELECT * FROM nudge").fetchone()
    assert row["followed"] == expected
    expected_outcome = {1: "followed", 0: "not_followed", None: "unknown"}[expected]
    assert row["outcome"] == expected_outcome
    assert row["outcome_reason"]
    assert row["decided_ts"] == fired + 600
    assert conn.execute("SELECT COUNT(*) FROM nudge").fetchone()[0] == 1
    conn.close()


def test_budget_100_model_downgrade_and_unmeasured(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    fired = 1_784_500_000.0
    _request(conn, "before", "s", fired - 1, "claude-fable-5")
    _nudge_event(conn, fired, "budget_warn_100")
    _event(conn, fired + 5, "UserPromptSubmit", "s", "continue")
    _request(conn, "after", "s", fired + 10, "claude-sonnet-5")
    _nudge_event(conn, fired, "ttl_prenotify", "other")
    monkeypatch.setattr(ingest.time, "time", lambda: fired + 601)
    ingest._derive_nudges(conn)
    measured = conn.execute("SELECT * FROM nudge WHERE rule='budget_warn_100'").fetchone()
    unmeasured = conn.execute("SELECT * FROM nudge WHERE rule='ttl_prenotify'").fetchone()
    assert measured["followed"] == 1
    assert unmeasured["followed"] is None and unmeasured["decided_ts"] == fired + 600
    assert unmeasured["outcome"] == "unknown"
    conn.close()


def test_compact_recovery_unmeasured(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    fired = 1000.0
    _nudge_event(conn, fired, "compact_recovery")
    monkeypatch.setattr(ingest.time, "time", lambda: fired + 601)
    ingest._derive_nudges(conn)
    row = conn.execute("SELECT * FROM nudge WHERE rule='compact_recovery'").fetchone()
    assert row["followed"] is None and row["decided_ts"] == fired + 600
    assert row["outcome"] == "unknown" and row["outcome_reason"] == "rule_not_measured"
    conn.close()


def test_hook_payload_redacted_and_derive_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    spool = tmp_path / "spool/hooks"
    spool.mkdir(parents=True)
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
    envelope = {
        "metsuke_event": "UserPromptSubmit",
        "metsuke_ts": 123.0,
        "payload": {"session_id": "s", "prompt": f"use {secret}"},
    }
    (spool / "hook.ndjson").write_text(json.dumps(envelope) + "\n")
    conn = ledger.connect()
    ingest.run(conn)
    payload = conn.execute("SELECT payload_json FROM hook_event").fetchone()[0]
    assert secret not in payload and "REDACTED:anthropic_key" in payload
    conn.close()
