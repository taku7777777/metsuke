import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from metsuke import archiver, cli, config, ingest, judgment, ledger, state


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "projects"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    return home, source


def _transcript(source: Path):
    path = source / "project" / "session.jsonl"
    path.parent.mkdir()
    records = [
        {
            "type": "user",
            "sessionId": "session",
            "promptId": "prompt-1",
            "timestamp": "2026-07-17T01:00:00Z",
            "message": {"role": "user", "content": "do the task"},
        },
        {
            "type": "assistant",
            "sessionId": "session",
            "requestId": "request-1",
            "promptId": "prompt-1",
            "timestamp": "2026-07-17T01:00:01Z",
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "stop_reason": "end_turn",
                "content": [],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")


def _sync(conn):
    ingest.run(conn)


def _facts(conn):
    return {
        "marker": [tuple(r) for r in conn.execute("SELECT * FROM marker ORDER BY marker_id")],
        "outcome": [tuple(r) for r in conn.execute("SELECT * FROM outcome ORDER BY prompt_id,ts")],
        "label": conn.execute("SELECT task_label FROM prompt WHERE prompt_id='prompt-1'").fetchone()[0],
        "regime": [tuple(r) for r in conn.execute("SELECT * FROM regime_event WHERE kind='manual-change'")],
    }


def test_judgment_cli_sequence_and_rebuild(env, capsys):
    _, source = env
    _transcript(source)
    archiver.run(source)
    conn = ledger.connect()
    _sync(conn)

    assert cli.main(["mark", "start", "--category", "prompting", "--hypothesis", "shorter is cheaper", "--expected", "-10% cost"]) == 0
    marker_id = capsys.readouterr().out.split()[0]
    _sync(conn)
    assert cli.main(["mark", "end"]) == 0
    _sync(conn)
    assert cli.main(
        ["mark", "verdict", marker_id, "win", "--note", "observed", "--saving-usd", "12.5"]
    ) == 0
    assert cli.main(["done", "completed"]) == 0
    assert cli.main(["regime", "add", "manual-change", "CLAUDE.md updated"]) == 0
    judgment.record("task_label", {"prompt_id": "prompt-1", "label": "feature"}, ts=100.0)
    judgment.record("task_label", {"prompt_id": "prompt-1", "label": "design"}, ts=101.0)
    _sync(conn)

    marker = conn.execute("SELECT * FROM marker WHERE marker_id=?", (marker_id,)).fetchone()
    assert marker["ts_start"] and marker["ts_end"]
    assert marker["verdict"] == "win" and marker["decided_by"] == "human"
    assert marker["saving_usd"] == 12.5
    assert marker["verdict_note"] == "observed"
    assert conn.execute("SELECT COUNT(*) FROM outcome").fetchone()[0] == 1
    before = _facts(conn)
    conn.close()

    ingest.rebuild()
    rebuilt = ledger.connect_readonly()
    assert _facts(rebuilt) == before
    assert _facts(rebuilt)["label"] == "design"
    rebuilt.close()


def test_outcome_idempotent(env):
    _, source = env
    _transcript(source)
    archiver.run(source)
    conn = ledger.connect()
    _sync(conn)
    payload = {
        "prompt_id": "prompt-1",
        "ts": 200.0,
        "label": "partial",
        "source": "manual",
    }
    judgment.record("outcome", payload, ts=200.0)
    judgment.record("outcome", payload, ts=200.0)
    _sync(conn)
    _sync(conn)
    assert conn.execute("SELECT COUNT(*) FROM outcome").fetchone()[0] == 1
    conn.close()


def test_invalid_judgments_quarantined_without_blocking(env):
    judgment.record("unknown", {"value": 1}, ts=300.0)
    judgment.record(
        "marker_verdict",
        {"marker_id": "iv-x", "verdict": "maybe", "decided_by": "human", "verdict_ts": 301.0},
        ts=301.0,
    )
    judgment.record("regime", {"ts": 302.0, "regime_kind": "valid", "detail": "kept"}, ts=302.0)
    conn = ledger.connect()
    _sync(conn)
    assert conn.execute("SELECT COUNT(*) FROM quarantine WHERE src='judgment'").fetchone()[0] == 2
    assert conn.execute("SELECT detail FROM regime_event WHERE kind='valid'").fetchone()[0] == "kept"
    _sync(conn)
    assert conn.execute("SELECT COUNT(*) FROM quarantine WHERE src='judgment'").fetchone()[0] == 2
    conn.close()


def _proposal(home, name, body):
    directory = home / "spool/proposals"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False))
    return path


def test_approve_task_labels(env, monkeypatch):
    home, _ = env
    path = _proposal(
        home,
        "labels",
        {
            "kind": "task_label",
            "rationale": "group similar work",
            "items": [
                {"prompt_id": "p1", "label": "feature"},
                {"prompt_id": "p2", "label": "design"},
            ],
        },
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["approve", "labels"]) == 0
    files = list(config.hooks_spool_dir().glob("*-judgment-task_label.ndjson"))
    assert len(files) == 2
    assert all(json.loads(p.read_text())["payload"]["decided_by"] == "ai+human" for p in files)
    assert not path.exists() and (path.parent / "applied-labels.json").exists()


def test_approve_requires_tty_and_rejects_partial(env, monkeypatch):
    home, _ = env
    path = _proposal(
        home,
        "tty",
        {"kind": "task_label", "rationale": "r", "items": [{"prompt_id": "p", "label": "feature"}]},
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["approve", "tty"]) == 1
    assert path.exists() and not list(config.hooks_spool_dir().glob("*.ndjson"))

    bad = _proposal(
        home,
        "bad",
        {
            "kind": "task_label",
            "rationale": "r",
            "items": [{"prompt_id": "ok", "label": "feature"}, {"prompt_id": "missing-label"}],
        },
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["approve", "bad"]) == 1
    assert bad.exists() and not list(config.hooks_spool_dir().glob("*.ndjson"))


def test_deadman_previous_iso_week(env, monkeypatch, capsys):
    home, _ = env
    reports = home / "reports"
    reports.mkdir(parents=True)
    report = reports / "2026-W28.md"
    report.write_text("weekly report")
    now = dt.datetime(2026, 7, 15, 12, 0).timestamp()
    assert cli.main(["deadman", "--now", str(now)]) == 0
    assert f"ok: {report}" in capsys.readouterr().out

    report.unlink()
    calls = []
    monkeypatch.setattr(state, "_notify", lambda title, message: calls.append((title, message)))
    assert cli.main(["deadman", "--now", str(now)]) == 1
    assert calls == [("metsuke deadman", "先週レポート 2026-W28 が見つかりません — 週次アナリストが止まっています")]


def test_mark_end_and_done_resolution_errors(env, capsys):
    assert cli.main(["mark", "end"]) == 1
    assert "no open marker" in capsys.readouterr().err
    assert cli.main(["done", "completed"]) == 1
    assert "no prompt" in capsys.readouterr().err


def test_existing_prompt_table_migrates_task_label(env):
    home, _ = env
    home.mkdir(parents=True, exist_ok=True)
    path = home / "old-ledger.db"
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE prompt (
           prompt_id TEXT PRIMARY KEY, session_id TEXT, ts REAL, text TEXT,
           interrupted_message_id TEXT)"""
    )
    old.commit()
    old.close()
    conn = ledger.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prompt)")}
    assert "task_label" in columns
    conn.close()
