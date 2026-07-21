import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from metsuke import archiver, cli, ingest, judgment, ledger


ROOT = Path(__file__).parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    return home, source


def _transcript(source):
    stamp = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    path = source / "project" / "session.jsonl"
    path.parent.mkdir()
    rows = [
        {
            "type": "user",
            "sessionId": "session",
            "promptId": "prompt-1",
            "timestamp": stamp,
            "message": {"role": "user", "content": "implement task"},
        },
        {
            "type": "assistant",
            "sessionId": "session",
            "requestId": "request-1",
            "timestamp": stamp,
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "stop_reason": "end_turn",
                "content": [],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_task_cli_auto_attach_efficiency_and_rebuild(env, capsys):
    home, source = env
    _transcript(source)
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)

    assert cli.main(
        ["task", "start", "pricing repair", "--category", "feature", "--goal", "accurate"]
    ) == 0
    task_id = capsys.readouterr().out.split()[0]
    assert (home / "state/active-task").read_text().strip() == task_id
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "UserPromptSubmit"],
        input=json.dumps({"session_id": "session", "prompt_id": "prompt-1", "prompt": "work"}),
        text=True,
        env={"METSUKE_HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 0
    ingest.run(conn)
    link = conn.execute("SELECT * FROM task_prompt WHERE task_id=?", (task_id,)).fetchone()
    assert link["prompt_id"] == "prompt-1" and link["source"] == "active_task_hook"

    assert cli.main(
        [
            "task", "finish", "--outcome", "completed", "--quality", "4",
            "--rework-minutes", "15", "--note", "verified",
        ]
    ) == 0
    assert not (home / "state/active-task").exists()
    ingest.run(conn)
    task = conn.execute("SELECT * FROM v_task_efficiency WHERE task_id=?", (task_id,)).fetchone()
    assert task["outcome"] == "completed" and task["quality_score"] == 4
    assert task["rework_minutes"] == 15 and task["n_prompts"] == 1
    assert task["cost_usd"] == pytest.approx(2.0)
    expected = tuple(task)
    conn.close()

    ingest.rebuild()
    rebuilt = ledger.connect_readonly()
    assert tuple(
        rebuilt.execute("SELECT * FROM v_task_efficiency WHERE task_id=?", (task_id,)).fetchone()
    ) == expected
    rebuilt.close()


def test_task_status_and_invalid_judgment(env, capsys):
    home, _ = env
    conn = ledger.connect()
    judgment.record(
        "task_start",
        {
            "task_id": "bad-category",
            "title": "bad",
            "category": "anything",
            "ts_start": 1,
        },
        ts=1,
    )
    ingest.run(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM quarantine WHERE reason='invalid task category'"
    ).fetchone()[0] == 1
    conn.close()
    assert cli.main(["task", "status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"active_task": None, "tasks": []}


def test_task_rejects_negative_rework_without_closing_active_task(env, capsys):
    home, _ = env
    assert cli.main(["task", "start", "work", "--category", "chore"]) == 0
    task_id = capsys.readouterr().out.split()[0]
    assert cli.main(
        ["task", "finish", "--outcome", "completed", "--rework-minutes", "-1"]
    ) == 2
    assert (home / "state/active-task").read_text().strip() == task_id


def test_roi_ranges_and_recorded_human_cost(env, monkeypatch, capsys):
    home, _ = env
    monkeypatch.setenv("METSUKE_HOURLY_VALUE_USD", "120")
    conn = ledger.connect()
    conn.execute(
        """INSERT INTO marker
           (marker_id,ts_start,verdict,saving_usd,saving_low_usd,saving_high_usd,
            saving_basis,verdict_note)
           VALUES ('m',1,'win',30,20,40,'matched tasks','observed')"""
    )
    conn.commit()
    conn.close()
    assert cli.main(
        ["roi", "--add-cost", "maintenance", "--minutes", "30", "--usd", "5", "--note", "week"]
    ) == 0
    ingest.run()
    assert cli.main(["roi", "--json"]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["saving_usd"] == 30
    assert result["saving_low_usd"] == 20 and result["saving_high_usd"] == 40
    assert result["recorded_cost_usd"] == 5 and result["time_cost_usd"] == 60
    assert result["total_known_cost_usd"] == 65
    assert result["roi_low"] == pytest.approx(20 / 65)
    assert result["cost_complete"] is True
    assert cli.main(["roi", "--days", "90", "--json"]) == 0
    recent = json.loads(capsys.readouterr().out)
    assert recent["window_days"] == 90 and recent["saving_usd"] == 0
    assert recent["total_known_cost_usd"] == 65
    assert cli.main(["roi", "--minutes", "1"]) == 2
