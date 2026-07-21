import json
import time

from metsuke import cli, ledger, state


def _cached_request(conn, rid, sid, ts, *, w5m=0, w1h=100_000):
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,ts,model,input_tok,output_tok,cache_read_tok,
            cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES (?,?,?,?, 'claude-sonnet-5',0,0,0,?,?,0,'transcript')""",
        (rid, sid, sid, ts, w5m, w1h),
    )


def test_state_distinguishes_cache_ttl_policies(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    _cached_request(conn, "five", "five", now - 30, w5m=100_000, w1h=0)
    _cached_request(conn, "hour", "hour", now - 30, w5m=0, w1h=100_000)
    _cached_request(conn, "mixed", "mixed", now - 30, w5m=50_000, w1h=50_000)
    sessions = state.build(conn)["sessions"]
    assert sessions["five"]["cache_ttl_kind"] == "5m"
    assert sessions["five"]["ttl_remaining_s"] <= 270
    assert sessions["five"]["rebuild_cost_low_usd"] == sessions["five"]["rebuild_cost_high_usd"]
    assert sessions["hour"]["cache_ttl_kind"] == "1h"
    assert sessions["hour"]["ttl_remaining_s"] > 3500
    assert sessions["mixed"]["cache_ttl_kind"] == "mixed"
    assert sessions["mixed"]["rebuild_cost_low_usd"] < sessions["mixed"]["rebuild_cost_high_usd"]
    conn.close()


def test_ttl_review_waits_for_four_week_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    conn.close()
    assert cli.main(["ttl-review", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "insufficient_data"


def test_ttl_review_deprioritizes_small_avoidable_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    for index, days_ago in enumerate((0, 3, 6, 9, 12, 15, 18, 21, 24, 27)):
        first = now - days_ago * 86400 - 5000
        sid = f"s{index}"
        _cached_request(conn, f"{sid}-first", sid, first)
        _cached_request(conn, f"{sid}-rebuild", sid, first + 4000)
    conn.commit()
    conn.close()
    assert cli.main(["ttl-review", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evidence_span_days"] >= 28
    assert result["active_days"] >= 10
    assert result["ttl_expiry_events"] == 10
    assert result["decision"] == "deprioritize"
    assert result["avoidable_usd_per_calendar_day"] < 5
