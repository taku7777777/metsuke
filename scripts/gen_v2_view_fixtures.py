"""Generate the period + dist jsdom render fixtures from the REAL v2 serializer.

Run: ./.venv/bin/python scripts/gen_v2_view_fixtures.py

Seeds a throwaway ledger with a spread of sessions / prompts / projects / models (some
interrupted, some delegated to sub-agents), then dumps the exact bytes ``/v2/api/period`` and
``/v2/api/dist`` return — ``dashboard2.web.view_payload(request, <view>.query(...), freshness)``.

For each view it writes both a populated fixture (window that covers the seed) and an EMPTY
fixture (a window with no requests, produced by the same real query path — never hand-zeroed),
so the jsdom gate can prove "empty model -> no rows" faithfully. Committing this output keeps
the render gate's fixtures in lock-step with ``to_jsonable(<view>.query(...))``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path

os.environ["TZ"] = "UTC"
time.tzset()

from metsuke import ledger  # noqa: E402
from metsuke.dashboard.routes import DashboardRequest  # noqa: E402
from metsuke.dashboard2 import web  # noqa: E402
from metsuke.viewmodel import cache, dist, period, trend  # noqa: E402
from metsuke.viewmodel.common import Page, Window  # noqa: E402

TODAY = dt.date(2026, 7, 20)
FROM = dt.date(2026, 7, 14)  # 7-day window covering the seed
EMPTY_FROM = dt.date(2000, 1, 1)  # a window the seed never touches -> empty tables
EMPTY_TO = dt.date(2000, 1, 7)


def _ts(day: dt.date, hour: int = 12) -> float:
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC).timestamp()


# (request_id, session, project, prompt_id, text, day, tokens, agent_id, is_interrupted, model)
# tokens = (input, output, cache_read, cache_w5m, cache_w1h)
ROWS = [
    ("r01", "sess-alpha-01", "metsuke", "prm-alpha-a1", "dashboard v2 のレンダリングを Preact に移行する計画",
     TODAY, (420000, 900, 380000, 1200, 40), None, 0, "claude-opus-5"),
    ("r02", "sess-alpha-01", "metsuke", "prm-alpha-a2", "テストが落ちる原因を調べて",
     TODAY, (90000, 300, 210000, 300, 10), None, 1, "claude-sonnet-5"),
    ("r03", "sess-beta-01", "atlas-api", "prm-beta-b1", "SQL クエリの N+1 を解消したい",
     TODAY, (260000, 1400, 60000, 800, 30), None, 0, "claude-sonnet-5"),
    ("r04", "sess-beta-01", "atlas-api", "prm-beta-b1", "SQL クエリの N+1 を解消したい (delegated)",
     TODAY, (120000, 400, 30000, 200, 8), "agent-01", 0, "claude-haiku-5"),
    ("r05", "sess-alpha-01", "metsuke", "prm-alpha-a3", "CSP を保ったまま SVG チャートを実装する方法",
     dt.date(2026, 7, 19), (180000, 700, 150000, 500, 20), None, 0, "claude-opus-5"),
    ("r06", "sess-gamma-01", "atlas-api", "prm-gamma-c1", "デプロイスクリプトのレビュー",
     dt.date(2026, 7, 17), (140000, 500, 90000, 400, 15), None, 0, "claude-sonnet-5"),
    ("r07", "sess-delta-01", "orbit-cli", "prm-delta-d1", "型エラーの修正",
     dt.date(2026, 7, 16), (75000, 250, 40000, 200, 8), None, 0, "claude-sonnet-5"),
    ("r08", "sess-eps-01", "sandbox-experiments", "prm-eps-e1", "自動実行タスク（対話のみから除外）",
     dt.date(2026, 7, 15), (56000, 180, 24000, 120, 5), None, 0, "claude-haiku-5"),
    ("r09", "sess-alpha-01", "metsuke", "prm-alpha-a4", "選択範囲外・コンテキスト用の古い prompt",
     dt.date(2026, 7, 11), (60000, 200, 30000, 150, 5), None, 0, "claude-sonnet-5"),
    ("r10", "sess-zeta-01", "orbit-cli", "prm-zeta-z1", "巨大コンテキストの調査",
     dt.date(2026, 7, 18), (620000, 2200, 540000, 3000, 90), None, 0, "claude-opus-5"),
]


def _seed(conn) -> None:
    conn.execute("DELETE FROM price")
    for model, in_usd, out_usd in [
        ("claude-opus-5", 15, 75),
        ("claude-sonnet-5", 3, 15),
        ("claude-haiku-5", 0.8, 4),
    ]:
        conn.execute(
            """INSERT INTO price
               (model,valid_from,in_usd,out_usd,cache_read_x,cache_w5m_x,cache_w1h_x,
                batch_x,fast_x,geo_us_x)
               VALUES (?, '1970-01-01', ?, ?, 0.1, 1.25, 2, 0.5, 2, 1.1)""",
            (model, in_usd, out_usd),
        )
    seen_sessions: set[str] = set()
    seen_prompts: set[str] = set()
    for rid, sess, project, pid, text, day, toks, agent, intr, model in ROWS:
        stamp = _ts(day)
        if sess not in seen_sessions:
            conn.execute(
                "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
                (sess, project, stamp, stamp),
            )
            seen_sessions.add(sess)
        else:
            conn.execute(
                "UPDATE session SET last_ts=MAX(last_ts,?), first_ts=MIN(first_ts,?) "
                "WHERE session_id=?",
                (stamp, stamp, sess),
            )
        if pid not in seen_prompts:
            conn.execute(
                "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES (?,?,?,?)",
                (pid, sess, stamp, text),
            )
            seen_prompts.add(pid)
        i_tok, o_tok, cr_tok, w5_tok, w1_tok = toks
        conn.execute(
            """INSERT INTO request
               (request_id,session_id,lineage_id,prompt_id,ts,model,agent_id,input_tok,
                output_tok,cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,
                is_interrupted,source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,'transcript')""",
            (rid, sess, sess, pid, stamp, model, agent, i_tok, o_tok, cr_tok, w5_tok, w1_tok, intr),
        )
    conn.execute(
        "INSERT INTO ingest_log(ts,manifest_pos,segments,records,quarantined,parser_version) "
        "VALUES (?,?,?,?,?,?)",
        (_ts(TODAY, 13), 1, len(ROWS), len(ROWS), 0, 1),
    )
    # A marker + a regime_event inside the trend window so volume_chart emits an annotation
    # band and a regime line — and so the fixture exercises to_jsonable's sqlite3.Row branch
    # (trend.query hands these raw Rows to the chart node).
    conn.execute(
        "INSERT INTO marker(marker_id,ts_start,ts_end,category,verdict) VALUES (?,?,?,?,?)",
        ("mk-01", _ts(dt.date(2026, 7, 16)), _ts(dt.date(2026, 7, 18)), "施策A", "pending"),
    )
    conn.execute(
        "INSERT INTO regime_event(ts,kind,detail) VALUES (?,?,?)",
        (_ts(dt.date(2026, 7, 17)), "price_change", "opus 値上げ"),
    )
    conn.commit()


def _freshness():
    return type("F", (), {"stale": False, "last_ingest": _ts(TODAY, 13), "age_seconds": 120.0})()


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "frontend" / "test"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        conn = ledger.connect(Path(tmp) / "fixture.db")
        _seed(conn)

        window = Window(FROM, TODAY, None, f"{FROM} — {TODAY}")
        empty = Window(EMPTY_FROM, EMPTY_TO, None, f"{EMPTY_FROM} — {EMPTY_TO}")

        outputs = {
            "fixture-period.json": DashboardRequest("period", window, Page(), "7d"),
            "fixture-dist.json": DashboardRequest("dist", window, Page(), "7d"),
            "fixture-trend.json": DashboardRequest("trend", window, Page(), "7d"),
            "fixture-cache.json": DashboardRequest("cache", window, Page(), "7d"),
            "fixture-period-empty.json": DashboardRequest("period", empty, Page(), "custom"),
            "fixture-dist-empty.json": DashboardRequest("dist", empty, Page(), "custom"),
            "fixture-trend-empty.json": DashboardRequest("trend", empty, Page(), "custom"),
            "fixture-cache-empty.json": DashboardRequest("cache", empty, Page(), "custom"),
        }
        for name, request in outputs.items():
            if request.view == "period":
                model = period.query(conn, request.window, request.page)
            elif request.view == "trend":
                model = trend.query(conn, request.window)
            elif request.view == "cache":
                model = cache.query(conn, request.window)
            else:
                model = dist.query(conn, request.window)
            payload = web.view_payload(request, model, _freshness())
            (out_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"wrote {name}")
        conn.close()


if __name__ == "__main__":
    main()
