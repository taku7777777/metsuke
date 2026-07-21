import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

import pytest

from metsuke import archiver, ingest, ledger


ROOT = Path(__file__).parents[1]


def _attr(key, value, value_type="stringValue"):
    return {"key": key, "value": {value_type: value}}


def _record(kind, ts, request_id, *, session_id="session-1", sequence=1):
    attrs = [
        _attr("event.name", kind),
        _attr("session.id", session_id),
        _attr("event.sequence", str(sequence), "intValue"),
    ]
    if request_id is not None:
        attrs.append(_attr("request_id", request_id))
    if kind == "api_request":
        attrs.extend(
            [
                _attr("prompt.id", "prompt-1"),
                _attr("model", "claude-sonnet-5-20260701"),
                _attr("effort", "high"),
                _attr("query_source", "away_summary"),
                _attr("speed", "fast"),
                _attr("input_tokens", 100, "intValue"),
                _attr("output_tokens", "200", "intValue"),
                _attr("cache_read_tokens", 300, "intValue"),
                _attr("cache_creation_tokens", 400, "intValue"),
                _attr("cost_usd", 0.123, "doubleValue"),
                _attr("duration_ms", 42.5, "doubleValue"),
            ]
        )
    elif kind == "api_error":
        attrs.extend([_attr("error", "overloaded"), _attr("status_code", 529, "intValue")])
    else:
        attrs.append(_attr("prompt", "private full text must be dropped"))
    return {"timeUnixNano": str(int(ts * 1e9)), "attributes": attrs}


def _envelope(records, resource_attributes=None):
    resource = {}
    if resource_attributes:
        resource["resource"] = {"attributes": resource_attributes}
    resource["scopeLogs"] = [{"logRecords": records}]
    return {"resourceLogs": [resource]}


def _write_otel(home, name, records):
    directory = home / "otel"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(_envelope(records)) + "\n")


def _write_transcript(source, ts):
    stamp = dt.datetime.fromtimestamp(ts, dt.UTC).isoformat().replace("+00:00", "Z")
    user_stamp = dt.datetime.fromtimestamp(ts - 1, dt.UTC).isoformat().replace("+00:00", "Z")
    path = source / "project" / "session-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "sessionId": "session-1",
            "promptId": "prompt-1",
            "timestamp": user_stamp,
            "message": {"role": "user", "content": "work"},
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "requestId": "req_same",
            "promptId": "prompt-1",
            "timestamp": stamp,
            "message": {
                "id": "message-transcript",
                "model": "claude-sonnet-5-20260701",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 13,
                    "cache_read_input_tokens": 17,
                    "cache_creation_input_tokens": 42,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 19,
                        "ephemeral_1h_input_tokens": 23,
                    },
                },
                "speed": "normal",
                "stop_reason": "end_turn",
                "content": [],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    return home, source


def test_otlp_expansion_filter_and_dedup(env):
    home, source = env
    now = 1_784_250_000.0
    records = [
        _record("api_request", now, "req_same", sequence=1),
        _record("api_error", now + 1, "req_error", sequence=2),
        _record("user_prompt", now + 2, None, sequence=3),
    ]
    _write_otel(home, "events.json", records)
    _write_otel(home, "rotated.json", records)
    stats = archiver.run(source)
    assert stats.segments == 2
    entries = [row for row in archiver.manifest_entries() if row["kind"] == "otel"]
    assert len(entries) == 2 and all(row["path"].startswith("__otel__/") for row in entries)
    conn = ledger.connect()
    ingest.run(conn)
    events = list(conn.execute("SELECT * FROM otel_event ORDER BY kind"))
    assert len(events) == 2 and {row["kind"] for row in events} == {"api_request", "api_error"}
    request = next(row for row in events if row["kind"] == "api_request")
    assert request["input_tok"] == 100 and request["output_tok"] == 200
    assert request["cost_usd_sdk"] == 0.123 and request["query_source"] == "away_summary"
    assert "resourceLogs" not in request["raw_json"]
    assert json.loads(request["raw_json"])["attributes"]["session.id"] == "session-1"
    conn.close()


def test_resource_attributes_are_merged_and_identity_is_not_persisted(env):
    home, source = env
    now = 1_784_250_000.0
    record = _record("api_request", now, "req-resource")
    record["attributes"] = [
        item for item in record["attributes"] if item["key"] != "session.id"
    ]
    envelope = _envelope(
        [record],
        [_attr("session.id", "resource-session"), _attr("user.email", "private@example.com")],
    )
    directory = home / "otel"
    directory.mkdir(parents=True)
    (directory / "resource.json").write_text(json.dumps(envelope) + "\n")
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    event = conn.execute("SELECT * FROM otel_event WHERE request_id='req-resource'").fetchone()
    assert event["session_id"] == "resource-session"
    assert "private@example.com" not in event["raw_json"]
    conn.close()


def test_otel_event_without_dedup_identity_is_quarantined(env):
    home, source = env
    record = _record("api_error", 1_784_250_000.0, None, session_id="")
    record["attributes"] = [
        item
        for item in record["attributes"]
        if item["key"] not in {"session.id", "event.sequence"}
    ]
    _write_otel(home, "missing-identity.json", [record])
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    assert conn.execute("SELECT COUNT(*) FROM otel_event").fetchone()[0] == 0
    reason = conn.execute("SELECT reason FROM quarantine").fetchone()[0]
    assert "missing request_id or session/sequence identity" in reason
    conn.close()


def _ordered_result(base, monkeypatch, order):
    home = base / "home"
    source = base / "source"
    source.mkdir(parents=True)
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    now = 1_784_250_000.0
    if order == "transcript_first":
        _write_transcript(source, now)
        archiver.run(source)
        conn = ledger.connect()
        ingest.run(conn)
        _write_otel(home, "events.json", [_record("api_request", now + 2, "req_same")])
        archiver.run(source)
        ingest.run(conn)
    else:
        _write_otel(home, "events.json", [_record("api_request", now + 2, "req_same")])
        archiver.run(source)
        conn = ledger.connect()
        ingest.run(conn)
        _write_transcript(source, now)
        archiver.run(source)
        ingest.run(conn)
    row = conn.execute("SELECT * FROM request WHERE request_id='req_same'").fetchone()
    result = tuple(row)
    assert row["source"] == "transcript"
    assert (row["input_tok"], row["output_tok"], row["cache_read_tok"]) == (11, 13, 17)
    assert (row["cache_w5m_tok"], row["cache_w1h_tok"]) == (19, 23)
    assert row["query_source"] == "away_summary" and row["effort"] == "high"
    assert row["cost_usd_sdk"] == 0.123
    assert row["api_duration_ms"] == 42.5 and row["end_ts"] == now
    cost = conn.execute("SELECT SUM(cost_usd) FROM v_daily").fetchone()[0]
    one = conn.execute("SELECT cost_usd FROM v_request_cost WHERE request_id='req_same'").fetchone()[0]
    assert cost == pytest.approx(one)
    expected = tuple(row)
    conn.close()
    ingest.rebuild()
    rebuilt = ledger.connect_readonly()
    rebuilt_row = rebuilt.execute(
        "SELECT * FROM request WHERE request_id='req_same'"
    ).fetchone()
    assert tuple(rebuilt_row) == expected and rebuilt_row["api_duration_ms"] == 42.5
    rebuilt.close()
    return result


def test_rule7_order_independent_convergence(tmp_path, monkeypatch):
    first = _ordered_result(tmp_path / "a", monkeypatch, "transcript_first")
    second = _ordered_result(tmp_path / "b", monkeypatch, "otel_first")
    assert first == second


def test_otel_only_billing_background_and_health(env):
    home, source = env
    conn = ledger.connect()
    assert conn.execute("SELECT status FROM v_health WHERE check_name='otel_freshness'").fetchone()[0] == "skip"
    conn.close()
    now = time.time()
    _write_otel(home, "events.json", [_record("api_request", now, "req_background")])
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    request = conn.execute("SELECT * FROM request WHERE request_id='req_background'").fetchone()
    assert request["source"] == "otel" and request["cache_w5m_tok"] == 0
    assert request["end_ts"] == now and request["api_duration_ms"] == 42.5
    assert request["cache_w1h_tok"] == 400
    assert conn.execute("SELECT cost_usd FROM v_daily").fetchone()[0] > 0
    background = conn.execute("SELECT * FROM v_background").fetchone()
    assert background["query_source"] == "away_summary" and background["n"] == 1
    assert conn.execute("SELECT status FROM v_health WHERE check_name='otel_freshness'").fetchone()[0] == "ok"
    conn.close()


def test_otel_request_migration_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE request (
        request_id TEXT PRIMARY KEY,message_id TEXT,session_id TEXT,agent_id TEXT,lineage_id TEXT,
        prompt_id TEXT,ts REAL,model TEXT,input_tok INTEGER,output_tok INTEGER,cache_read_tok INTEGER,
        cache_w5m_tok INTEGER,cache_w1h_tok INTEGER,server_tool_use TEXT,service_tier TEXT,speed TEXT,
        geo TEXT,stop_reason TEXT,is_synthetic INTEGER,is_interrupted INTEGER,on_main_path INTEGER,
        source TEXT,parser_version INTEGER,raw_path TEXT)"""
    )
    old.commit()
    old.close()
    conn = ledger.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(request)")}
    assert {"query_source", "effort", "cost_usd_sdk"} <= columns
    conn.close()
    again = ledger.connect(path)
    assert {row[1] for row in again.execute("PRAGMA table_info(request)")} == columns
    again.close()


def test_collector_config_is_logs_only_and_privacy_filtered():
    source = (ROOT / "otel/otelcol.yaml").read_text()
    assert 'attributes["event.name"]' in source
    assert "api_request" in source and "api_error" in source
    assert "user_prompt" not in source and "tool_result" not in source
    assert "metrics:" not in source.split("pipelines:", 1)[1]
    assert "127.0.0.1:__OTEL_PORT__" in source and "max_megabytes: 32" in source
    assert "attributes/metsuke_privacy" in source and "resource/metsuke_privacy" in source
    assert "user.email" in source and "user.account_uuid" in source


def test_otel_installer_enables_required_log_export_settings():
    source = (ROOT / "scripts/install-otel-env.sh").read_text()
    for expected in (
        '"CLAUDE_CODE_ENABLE_TELEMETRY": "1"',
        '"OTEL_LOGS_EXPORTER": "otlp"',
        '"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"',
        '"OTEL_METRICS_EXPORTER": "none"',
    ):
        assert expected in source
    assert 'f"http://localhost:{port}"' in source


def test_malformed_otel_quarantine_redacts_secret(env):
    home, source = env
    secret = "sk-proj-" + "o" * 32
    record = {"attributes": [_attr("event.name", "api_request"), _attr("error", secret)]}
    _write_otel(home, "bad.json", [record])
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    raw = conn.execute("SELECT raw FROM quarantine").fetchone()[0]
    assert secret not in raw and "[REDACTED" in raw
    conn.close()
