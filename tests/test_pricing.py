import datetime as dt
import json
import sqlite3

import pytest

from metsuke import cli, ledger


def _stamp(value: str) -> float:
    return dt.datetime.fromisoformat(value).timestamp()


def _request(conn, request_id, ts, model, *, speed=None, server_tool_use=None):
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,server_tool_use,speed,
            is_synthetic,source)
           VALUES (?,?,'s',?,?,1000000,0,0,0,0,?,?,0,'transcript')""",
        (request_id, "s", ts, model, server_tool_use, speed),
    )


def test_current_scd2_prices_and_bundled_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    sonnet = conn.execute(
        """SELECT valid_from,valid_to,in_usd,out_usd FROM price
           WHERE model='claude-sonnet-5' ORDER BY valid_from"""
    ).fetchall()
    assert [tuple(row) for row in sonnet] == [
        ("2026-01-01", "2026-09-01", 2.0, 10.0),
        ("2026-09-01", None, 3.0, 15.0),
    ]
    haiku = conn.execute(
        "SELECT in_usd,out_usd,geo_us_x FROM price WHERE model='claude-haiku-4-5'"
    ).fetchone()
    assert tuple(haiku) == (1.0, 5.0, 1.0)
    assert conn.execute(
        "SELECT geo_us_x FROM price WHERE model='claude-opus-4-6'"
    ).fetchone()[0] == 1.1
    assert conn.execute(
        "SELECT value FROM meta WHERE key='bundled_price_version'"
    ).fetchone()[0] == "2026-07-20"
    conn.execute(
        "UPDATE price SET in_usd=999 WHERE model='claude-sonnet-5' AND valid_from='2026-01-01'"
    )
    conn.commit()
    conn.close()
    refreshed = ledger.connect()
    assert refreshed.execute(
        "SELECT in_usd FROM price WHERE model='claude-sonnet-5' AND valid_from='2026-01-01'"
    ).fetchone()[0] == 2.0
    refreshed.close()


def test_legacy_unmarked_sonnet_price_is_removed_once(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    conn.execute("DELETE FROM meta WHERE key='legacy_price_cleanup_v1'")
    conn.execute(
        """INSERT INTO price
           (model,valid_from,in_usd,out_usd,cache_read_x,cache_w5m_x,
            cache_w1h_x,batch_x,fast_x,geo_us_x)
           VALUES ('claude-sonnet-5','2025-01-01',3,15,.1,1.25,2,.5,2,1.1)"""
    )
    conn.commit()
    conn.close()

    migrated = ledger.connect()
    assert migrated.execute(
        """SELECT COUNT(*) FROM price
           WHERE model='claude-sonnet-5' AND valid_from='2025-01-01'"""
    ).fetchone()[0] == 0
    assert migrated.execute(
        "SELECT value FROM meta WHERE key='legacy_price_cleanup_v1'"
    ).fetchone()[0] == "done"
    migrated.close()


def test_period_prices_fast_models_and_server_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    _request(conn, "sonnet-promo", _stamp("2026-08-31T12:00:00+00:00"), "claude-sonnet-5")
    _request(conn, "sonnet-standard", _stamp("2026-09-01T12:00:00+00:00"), "claude-sonnet-5")
    _request(conn, "opus47-fast", _stamp("2026-07-20T12:00:00+00:00"), "claude-opus-4-7", speed="fast")
    _request(conn, "opus47-retired", _stamp("2026-07-25T12:00:00+00:00"), "claude-opus-4-7", speed="fast")
    _request(conn, "opus48-fast", _stamp("2026-07-20T12:00:00+00:00"), "claude-opus-4-8", speed="fast")
    _request(
        conn,
        "tools",
        _stamp("2026-07-20T12:00:00+00:00"),
        "claude-sonnet-5",
        server_tool_use=json.dumps(
            {"web_search_requests": 2, "web_fetch_requests": 3}
        ),
    )
    costs = {
        row["request_id"]: row
        for row in conn.execute(
            "SELECT request_id,token_cost_usd,server_tool_usd,cost_usd FROM v_request_cost"
        )
    }
    assert costs["sonnet-promo"]["cost_usd"] == pytest.approx(2.0)
    assert costs["sonnet-standard"]["cost_usd"] == pytest.approx(3.0)
    assert costs["opus47-fast"]["cost_usd"] == pytest.approx(30.0)
    assert costs["opus47-retired"]["cost_usd"] == pytest.approx(5.0)
    assert costs["opus48-fast"]["cost_usd"] == pytest.approx(10.0)
    assert costs["tools"]["token_cost_usd"] == pytest.approx(2.0)
    assert costs["tools"]["server_tool_usd"] == pytest.approx(0.02)
    assert costs["tools"]["cost_usd"] == pytest.approx(2.02)
    conn.close()


def test_unpriced_server_tool_is_visible_in_health(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    _request(
        conn,
        "code",
        _stamp("2026-07-20T12:00:00+00:00"),
        "claude-sonnet-5",
        server_tool_use=json.dumps({"code_execution_requests": 1}),
    )
    row = conn.execute(
        "SELECT * FROM v_health WHERE check_name='unpriced_server_tools'"
    ).fetchone()
    assert row["status"] == "warn" and "code_execution_requests" in row["detail"]
    conn.close()


def test_invoice_reconciliation_uses_utc_month(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    # 23:30 UTC is already the next local day/month in Asia/Tokyo.
    stamp = _stamp("2026-07-31T23:30:00+00:00")
    _request(conn, "month-edge", stamp, "claude-sonnet-5")
    conn.execute(
        "INSERT INTO invoice(month,billed_usd,ts) VALUES ('2026-07',2,?)",
        (stamp,),
    )
    conn.commit()
    conn.close()
    assert cli.main(["invoice", "--check", "2026-07", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ledger_usd"] == pytest.approx(2.0)


def test_bundled_price_ranges_reject_overlap():
    with pytest.raises(ValueError, match="overlapping price ranges"):
        ledger._validate_price_ranges(
            [
                {"model": "m", "valid_from": "2026-01-01", "valid_to": "2026-03-01"},
                {"model": "m", "valid_from": "2026-02-01"},
            ],
            "model",
        )


def test_manual_overlap_fails_health_without_double_counting(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    conn.execute(
        """INSERT INTO price
           (model,valid_from,valid_to,in_usd,out_usd,cache_read_x,cache_w5m_x,
            cache_w1h_x,batch_x,fast_x,geo_us_x,source_url)
           VALUES ('claude-sonnet-5','2026-07-01','2026-08-01',99,99,.1,1.25,2,.5,1,1.1,
                   'manual')"""
    )
    _request(conn, "overlap", _stamp("2026-07-20T12:00:00+00:00"), "claude-sonnet-5")
    rows = conn.execute(
        "SELECT request_id,cost_usd FROM v_request_cost WHERE request_id='overlap'"
    ).fetchall()
    health = conn.execute(
        "SELECT status FROM v_health WHERE check_name='price_range_overlap'"
    ).fetchone()[0]
    assert len(rows) == 1 and rows[0]["cost_usd"] == pytest.approx(99.0)
    assert health == "fail"
    conn.close()


def test_prices_cli_reports_version_and_effective_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    assert cli.main(["prices", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "bundled_file"
    assert result["bundled_version"] == "2026-07-20"
    assert result["range_status"]["status"] == "ok"
    current = {row["model"]: row for row in result["models"]}
    assert current["claude-sonnet-5"]["in_usd"] == 2.0
    assert not ledger.db_path().exists(), "prices must not violate the single-writer rule"


def test_prices_cli_reports_unavailable_ledger_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    ledger.db_path().touch()

    def unavailable():
        raise sqlite3.OperationalError("cannot read")

    monkeypatch.setattr(ledger, "connect_readonly", unavailable)
    assert cli.main(["prices", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "ledger unavailable: cannot read"
    }
