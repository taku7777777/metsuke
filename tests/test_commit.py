import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from metsuke import archiver, ingest, ledger


ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(not shutil.which("jq") or not shutil.which("git"), reason="git+jq required")
def test_git_post_commit_sensor(tmp_path):
    repo = tmp_path / "sample-repo"
    home = tmp_path / "home"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("one\ntwo\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sensor test"], cwd=repo, check=True)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/git-post-commit.sh")], cwd=repo,
        env={"METSUKE_HOME": str(home), "PATH": os.environ["PATH"]}, capture_output=True,
    )
    assert result.returncode == 0
    files = list((home / "spool/hooks").glob("*-git_commit.ndjson"))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    payload = row["payload"]
    assert row["metsuke_event"] == "git_commit"
    assert payload["sha"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert payload["insertions"] == 2 and payload["files"] == ["file.txt"]


def _commit_hook(conn, ts, sha, repo_path, body="", insertions=3, deletions=1):
    payload = {
        "repo": Path(repo_path).name,
        "repo_path": repo_path,
        "branch": "main",
        "sha": sha,
        "subject": "commit",
        "body": body,
        "insertions": insertions,
        "deletions": deletions,
        "files": ["a.py"],
    }
    envelope = {"metsuke_event": "git_commit", "metsuke_ts": ts, "payload": payload}
    conn.execute(
        "INSERT INTO hook_event VALUES (?,?,?,?,?)",
        (ts, "git_commit", None, None, json.dumps(envelope)),
    )


def _session_prompt(conn, sid, project, prompt_id, ts, label=None):
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        (sid, project, ts, ts),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text,task_label) VALUES (?,?,?,?,?)",
        (prompt_id, sid, ts, "work", label),
    )


def test_derive_commits_attribution_retry_outcomes_and_revert(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    repo_path = "/Users/test/project"
    slug = repo_path.replace("/", "-")
    _session_prompt(conn, "s1", slug, "p1", 1000)
    _commit_hook(conn, 1100, "a" * 40, repo_path)
    _commit_hook(conn, 30_000, "b" * 40, repo_path)
    _commit_hook(
        conn, 1200, "c" * 40, repo_path,
        body=f"Revert change\n\nThis reverts commit {'a' * 12}.", insertions=0, deletions=3,
    )
    ingest._derive_commits(conn)
    first = conn.execute("SELECT * FROM commit_event WHERE sha=?", ("a" * 40,)).fetchone()
    late = conn.execute("SELECT * FROM commit_event WHERE sha=?", ("b" * 40,)).fetchone()
    assert first["prompt_id"] == "p1" and late["prompt_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM outcome WHERE label='completed'").fetchone()[0] == 1
    reverted = conn.execute("SELECT * FROM outcome WHERE label='reverted'").fetchone()
    assert reverted["prompt_id"] == "p1" and reverted["ts"] == 1200

    _session_prompt(conn, "s2", slug, "p2", 29_900)
    ingest._derive_commits(conn)
    assert conn.execute("SELECT prompt_id FROM commit_event WHERE sha=?", ("b" * 40,)).fetchone()[0] == "p2"
    assert conn.execute("SELECT COUNT(*) FROM outcome WHERE source='auto'").fetchone()[0] == 3
    ingest._derive_commits(conn)
    assert conn.execute("SELECT COUNT(*) FROM outcome WHERE source='auto'").fetchone()[0] == 3
    conn.close()


def _tool_transcript(source):
    path = source / "project" / "tools.jsonl"
    path.parent.mkdir()
    record = {
        "type": "assistant", "sessionId": "tools", "requestId": "r", "promptId": "p",
        "timestamp": "2026-07-17T00:00:00Z",
        "message": {
            "id": "m", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "id": "write", "name": "Write", "input": {"file_path": "/tmp/a", "content": "a\nb\nc"}},
                {"type": "tool_use", "id": "edit", "name": "Edit", "input": {"file_path": "/tmp/b", "old_string": "a\nb", "new_string": "x\ny\nz"}},
            ],
        },
    }
    path.write_text(json.dumps(record) + "\n")


def test_tool_call_lines_and_migration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    _tool_transcript(source)
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    tools = {row["tool_use_id"]: row for row in conn.execute("SELECT * FROM tool_call")}
    assert tools["write"]["file_path"] == "/tmp/a" and tools["write"]["lines_changed"] == 3
    assert tools["edit"]["file_path"] == "/tmp/b" and tools["edit"]["lines_changed"] == 3
    conn.close()

    old_path = home / "old.db"
    old = sqlite3.connect(old_path)
    old.execute(
        """CREATE TABLE tool_call(
           tool_use_id TEXT PRIMARY KEY,request_id TEXT,session_id TEXT,agent_id TEXT,
           prompt_id TEXT,name TEXT,ts REAL,is_error INTEGER,result_bytes INTEGER)"""
    )
    old.commit()
    old.close()
    migrated = ledger.connect(old_path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(tool_call)")}
    assert {"file_path", "lines_changed"} <= columns
    migrated.close()
    migrated_again = ledger.connect(old_path)
    assert {row[1] for row in migrated_again.execute("PRAGMA table_info(tool_call)")} == columns
    migrated_again.close()


def test_label_coverage_view(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    stamp = time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1))
    _session_prompt(conn, "s1", "project", "p1", stamp, "feature")
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p2','s1',?,'other')",
        (stamp + 1,),
    )
    conn.execute(
        "INSERT INTO outcome(prompt_id,ts,label,source) VALUES ('p1',?,'completed','manual')",
        (stamp + 2,),
    )
    row = conn.execute("SELECT * FROM v_label_coverage WHERE iso_week='2026-W29'").fetchone()
    assert row["prompts"] == 2 and row["labeled_prompts"] == 1
    assert row["coverage_pct"] == 50 and row["outcome_prompts"] == 1
    assert row["outcome_coverage_pct"] == 50
    conn.close()
