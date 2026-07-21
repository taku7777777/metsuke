import json
import sqlite3
import subprocess
import time

import pytest

from metsuke import cli, doctor, ingest, ledger


def _request(
    conn,
    rid,
    sid,
    prompt_id,
    ts,
    *,
    model="claude-fable-5",
    output=0,
    input_tok=0,
    cache_read=0,
    interrupted=0,
):
    conn.execute(
        """INSERT INTO request
           (request_id,message_id,session_id,lineage_id,prompt_id,ts,model,input_tok,
            output_tok,cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,
            is_interrupted,source)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,?,'transcript')""",
        (
            rid,
            f"m-{rid}",
            sid,
            sid,
            prompt_id,
            ts,
            model,
            input_tok,
            output,
            cache_read,
            interrupted,
        ),
    )


def _prompt(conn, pid, sid, ts, label=None, interrupted_message_id=None):
    conn.execute(
        """INSERT INTO prompt(prompt_id,session_id,ts,text,task_label,interrupted_message_id)
           VALUES (?,?,?,?,?,?)""",
        (pid, sid, ts, "work", label, interrupted_message_id),
    )


def _session(conn, sid, project="project", ts=None):
    ts = time.time() if ts is None else ts
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        (sid, project, ts, ts),
    )


def _hook(conn, ts, kind="SessionStart", sid="s", cost=None):
    payload = {"session_id": sid}
    if cost is not None:
        payload["cost"] = {"total_cost_usd": cost}
    envelope = {"metsuke_event": kind, "metsuke_ts": ts, "payload": payload}
    conn.execute(
        "INSERT INTO hook_event VALUES (?,?,?,?,?)",
        (ts, kind, sid, None, json.dumps(envelope)),
    )


def _health(conn):
    return {row["check_name"]: dict(row) for row in conn.execute("SELECT * FROM v_health")}


def test_v_health_thresholds_and_estimator_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _session(conn, "s", ts=now)
    _prompt(conn, "p", "s", now - 100, "feature")
    _request(conn, "r", "s", "p", now - 100, output=200_000)
    _hook(conn, now - 100)
    conn.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?)", (now - 100, 0, 0, 0, 0, 2))
    rows = _health(conn)
    assert rows["ledger_freshness"]["status"] == "ok"
    assert rows["hook_freshness"]["status"] == "ok"
    assert rows["ingest_recent"]["status"] == "ok"
    assert rows["estimator_gap"]["status"] == "skip"
    assert rows["label_coverage_cost"]["status"] == "ok"

    conn.execute("UPDATE request SET ts=?", (now - 2000,))
    conn.execute("UPDATE hook_event SET ts=?", (now - 8000,))
    conn.execute("UPDATE ingest_log SET ts=?", (now - 1000,))
    rows = _health(conn)
    assert rows["ledger_freshness"]["status"] == "warn"
    assert rows["hook_freshness"]["status"] == "fail"
    assert rows["ingest_recent"]["status"] == "warn"
    conn.close()


def test_health_estimator_and_cost_weighted_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _session(conn, "s", ts=now)
    _prompt(conn, "labeled", "s", now - 2, "feature")
    _prompt(conn, "plain", "s", now - 1)
    _request(conn, "r1", "s", "labeled", now - 2, output=200_000)
    _request(conn, "r2", "s", "plain", now - 1, output=600_000)
    conn.execute("UPDATE request SET cost_usd_sdk=10 WHERE request_id='r1'")
    conn.execute("UPDATE request SET cost_usd_sdk=30 WHERE request_id='r2'")
    _hook(conn, now, "statusline_sample", "s", cost=40.0)
    coverage = conn.execute("SELECT * FROM v_label_coverage").fetchone()
    assert coverage["labeled_cost_usd"] == pytest.approx(10)
    assert coverage["total_cost_usd"] == pytest.approx(40)
    assert coverage["cost_coverage_pct"] == pytest.approx(25)
    health = _health(conn)
    assert health["estimator_gap"]["status"] == "ok"
    assert health["label_coverage_cost"]["status"] == "fail"
    conn.execute("UPDATE request SET is_interrupted=1 WHERE request_id='r1'")
    conn.execute(
        "INSERT INTO quarantine VALUES (?,?,?,?)", (now, "fixture", "broken row", "raw")
    )
    conn.execute(
        """INSERT INTO nudge
           (rule,fired_ts,session_id,detail_json,followed,decided_ts,outcome)
           VALUES ('rule',?,'s','{}',0,?,'not_followed')""",
        (now, now),
    )
    conn.execute("UPDATE request SET agent_id='orphan' WHERE request_id='r2'")
    _request(conn, "unknown", "s", "plain", now, model="future-model")
    health = _health(conn)
    assert health["quarantine_7d"]["status"] == "fail"
    assert health["unknown_models"]["status"] == "fail"
    assert health["orphan_agents"]["status"] == "warn"
    assert health["nudge_conversion_14d"]["status"] == "warn"
    assert health["interrupted_share_7d"]["status"] == "fail"
    conn.close()


def test_v_unaccounted_input_lower_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    stamp = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
    _session(conn, "s", ts=stamp)
    _request(conn, "previous", "s", None, stamp - 10, input_tok=100_000, cache_read=1_000_000)
    _prompt(conn, "lost", "s", stamp, interrupted_message_id="missing-message")
    row = conn.execute("SELECT * FROM v_unaccounted").fetchone()
    assert row["n_lost_interruptions"] == 1
    assert row["input_side_lower_usd"] == pytest.approx(2.0)
    conn.close()


def test_invoice_check_and_rebuild(env, capsys):
    home, _ = env
    conn = ledger.connect()
    stamp = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
    _session(conn, "s", ts=stamp)
    _prompt(conn, "p", "s", stamp)
    _request(conn, "r", "s", "p", stamp, output=200_000)
    assert cli.main(["invoice", "2026-07", "10.4", "--note", "bill"]) == 0
    ingest.run(conn)
    assert cli.main(["invoice", "--check", "2026-07", "--json"]) == 0
    checked = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert checked["ledger_usd"] == pytest.approx(10)
    assert checked["residual_pct"] == pytest.approx(100 * 0.4 / 10.4)
    assert checked["price_calibration_candidate"] is True

    assert cli.main(["invoice", "2026-07", "12"]) == 0
    ingest.run(conn)
    assert cli.main(["invoice", "--check", "2026-07", "--json"]) == 0
    checked = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert checked["price_calibration_candidate"] is False
    conn.close()
    ingest.rebuild()
    rebuilt = ledger.connect_readonly()
    assert rebuilt.execute("SELECT billed_usd FROM invoice WHERE month='2026-07'").fetchone()[0] == 12
    rebuilt.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    return home, source


def test_counter_and_roi(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    stamp = time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1))
    _session(conn, "analyst", "-tmp-metsuke-analyst-2026-W28", stamp)
    _prompt(conn, "analysis", "analyst", stamp)
    _request(conn, "cost", "analyst", "analysis", stamp, output=200_000)
    conn.execute(
        """INSERT INTO marker(marker_id,ts_start,verdict,saving_usd)
           VALUES ('iv-win',?,'win',20),('iv-null',?,'win',NULL)""",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO outcome(prompt_id,ts,label,source) VALUES
           ('a',?,'completed','auto'),('b',?,'completed','auto'),('a',?,'reverted','auto')""",
        (stamp, stamp, stamp + 1),
    )
    counter = conn.execute("SELECT * FROM v_counter WHERE week='2026-W29'").fetchone()
    assert counter["completed_n"] == 2 and counter["reverted_n"] == 1
    assert counter["revert_rate_pct"] == 50
    conn.commit()
    assert cli.main(["roi", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["saving_usd"] == 20 and data["winning_markers"] == 2
    assert data["analyst_cost_usd"] == pytest.approx(10) and data["roi_ratio"] == 2
    conn.close()

    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "empty"))
    assert cli.main(["roi", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["roi_ratio"] is None


def test_doctor_json_and_fail_exit(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    fake_user = tmp_path / "user"
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setattr(doctor.Path, "home", lambda: fake_user)
    conn = ledger.connect()
    now = time.time()
    _session(conn, "s", ts=now)
    _prompt(conn, "p", "s", now - 1, "feature")
    _request(conn, "r", "s", "p", now - 1, output=1)
    _hook(conn, now - 1)
    conn.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?)", (now - 1, 0, 0, 0, 0, 2))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('manifest_pos','0')")
    conn.commit()
    conn.close()
    (home / "state.json").write_text(json.dumps({"generated_at": now}))
    (home / "archive/manifest.jsonl").touch()
    (home / "state/last_backup.json").write_text(json.dumps({"ts": now}))
    settings = fake_user / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    hooks = {
        event: [{"hooks": [{"command": f"/repo/hook-sensor.sh {event}"}]}]
        for event in doctor.HOOK_EVENTS
    }
    settings.write_text(
        json.dumps({"statusLine": {"command": "/repo/statusline.sh"}, "hooks": hooks})
    )

    def launchctl(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1 if command[-1].endswith("tick") else 0)

    monkeypatch.setattr(doctor.subprocess, "run", launchctl)
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda _path: shutil_usage(total=100 * 1024**3, used=10, free=90 * 1024**3),
    )
    assert cli.main(["doctor", "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)
    tick = next(row for row in rows if row["check_name"] == "launchd:com.metsuke.tick")
    assert tick["status"] == "fail"
    assert next(row for row in rows if row["check_name"] == "state_freshness")["status"] == "ok"


def test_doctor_manifest_reports_sqlite_error_instead_of_crashing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    manifest = tmp_path / "archive" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    manifest.touch()

    def unavailable():
        raise sqlite3.OperationalError("cannot read")

    monkeypatch.setattr(ledger, "connect_readonly", unavailable)
    items = []
    doctor._manifest(items)
    assert items == [
        {
            "check_name": "archive_manifest",
            "status": "fail",
            "value": "invalid",
            "detail": "cannot read",
        }
    ]


class shutil_usage:
    def __init__(self, total, used, free):
        self.total = total
        self.used = used
        self.free = free
