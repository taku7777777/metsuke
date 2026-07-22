"""Generate frontend/test/fixture.json from the REAL v2 serializer.

Run: ./.venv/bin/python scripts/gen_v2_fixture.py

Seeds a throwaway ledger with a few days of varied requests, runs the tested
``overview.query`` for a 7-day window, and dumps ``dashboard2.web.overview_payload`` — the
exact bytes ``/v2/api/overview`` returns. Committing this output keeps the jsdom render
gate's fixture in lock-step with ``to_jsonable(OverviewModel)`` so the shape cannot drift.
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
from metsuke.dashboard2 import web  # noqa: E402
from metsuke.dashboard.routes import DashboardRequest  # noqa: E402
from metsuke.viewmodel import overview  # noqa: E402
from metsuke.viewmodel.common import Page, Window  # noqa: E402

TODAY = dt.date(2026, 7, 20)
FROM = dt.date(2026, 7, 14)  # 7-day selection -> 7 selected bars of the 31-day context


def _ts(day: dt.date, hour: int = 12) -> float:
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC).timestamp()


# (request_id, session, project, prompt_id, prompt_text, day, tokens...)
# tokens = (input, output, cache_read, cache_w5m, cache_w1h)
ROWS = [
    ("r01", "sess-alpha-000000000000000001", "metsuke", "prm-alpha-0000000000000000a1",
     "リファクタリング: dashboard v2 のレンダリングを Preact に移行する計画を立てて", TODAY, (420000, 900, 380000, 1200, 40)),
    ("r02", "sess-alpha-000000000000000001", "metsuke", "prm-alpha-0000000000000000a2",
     "テストが落ちる原因を調べて", TODAY, (90000, 300, 210000, 300, 10)),
    ("r03", "sess-beta-0000000000000000b1", "atlas-api", "prm-beta-00000000000000000b1",
     "SQL クエリの N+1 を解消したい", TODAY, (260000, 1400, 60000, 800, 30)),
    ("r04", "sess-alpha-000000000000000001", "metsuke", "prm-alpha-0000000000000000a3",
     "CSP を厳格に保ったまま SVG チャートを実装する方法", dt.date(2026, 7, 19), (180000, 700, 150000, 500, 20)),
    ("r05", "sess-gamma-000000000000000c1", "atlas-api", "prm-gamma-00000000000000000c1",
     "デプロイスクリプトのレビュー", dt.date(2026, 7, 17), (140000, 500, 90000, 400, 15)),
    ("r06", "sess-delta-000000000000000d1", "metsuke", "prm-delta-00000000000000000d1",
     "型エラーの修正", dt.date(2026, 7, 14), (75000, 250, 40000, 200, 8)),
    ("r07", "sess-alpha-000000000000000001", "metsuke", "prm-alpha-0000000000000000a4",
     "古いセッション（選択範囲外・コンテキスト用）", dt.date(2026, 7, 11), (60000, 200, 30000, 150, 5)),
    ("r08", "sess-beta-0000000000000000b1", "atlas-api", "prm-beta-00000000000000000b2",
     "さらに古いリクエスト", dt.date(2026, 7, 8), (48000, 160, 22000, 90, 4)),
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fixture.db"
        conn = ledger.connect(db_path)
        conn.execute("DELETE FROM price WHERE model='claude-sonnet-5'")
        conn.execute(
            """INSERT INTO price
               (model,valid_from,in_usd,out_usd,cache_read_x,cache_w5m_x,cache_w1h_x,
                batch_x,fast_x,geo_us_x)
               VALUES ('claude-sonnet-5','1970-01-01',3,15,0.1,1.25,2,0.5,2,1.1)"""
        )
        seen_sessions: set[str] = set()
        seen_prompts: set[str] = set()
        for rid, sess, project, pid, text, day, toks in ROWS:
            stamp = _ts(day)
            if sess not in seen_sessions:
                conn.execute(
                    "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
                    (sess, project, stamp, stamp),
                )
                seen_sessions.add(sess)
            else:
                conn.execute(
                    "UPDATE session SET last_ts=MAX(last_ts,?), first_ts=MIN(first_ts,?) WHERE session_id=?",
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
                   (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
                    cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
                   VALUES (?,?,?,?,?,'claude-sonnet-5',?,?,?,?,?,0,0,'transcript')""",
                (rid, sess, sess, pid, stamp, i_tok, o_tok, cr_tok, w5_tok, w1_tok),
            )
        conn.execute(
            "INSERT INTO ingest_log(ts,manifest_pos,segments,records,quarantined,parser_version) VALUES (?,?,?,?,?,?)",
            (_ts(TODAY, 13), 1, len(ROWS), len(ROWS), 0, 1),
        )
        conn.commit()

        window = Window(FROM, TODAY, None, f"{FROM} — {TODAY}")
        model = overview.query(conn, window, Page(), today=TODAY)
        request = DashboardRequest("overview", window, Page(), "7d")
        # A representative freshness block (fresh, not stale) for the client to render against.
        freshness = type("F", (), {"stale": False, "last_ingest": _ts(TODAY, 13), "age_seconds": 120.0})()
        payload = web.overview_payload(request, model, freshness)
        conn.close()

    out = Path(__file__).resolve().parent.parent / "frontend" / "test" / "fixture.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    model_json = payload["model"]
    selected = sum(1 for d in model_json["daily_costs"] if d["selected"])
    print(f"wrote {out}")
    print(f"  daily_costs={len(model_json['daily_costs'])} selected={selected}")
    print(f"  cost_parts={len(model_json['cost_parts'])}")
    print(f"  top_prompts={len(model_json['top_prompts'])} top_sessions={len(model_json['top_sessions'])}")
    print(f"  kpis={[k['display'] for k in model_json['kpis']]}")


if __name__ == "__main__":
    main()
