"""Build the atomic, read-only hot-path cache consumed by shell sensors."""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from . import config
from .report import dominant_term


def _cost(conn, start: float, end: float) -> tuple[float, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0),COUNT(*) FROM v_request_cost WHERE ts>=? AND ts<?",
        (start, end),
    ).fetchone()
    return float(row[0]), int(row[1])


def _day_bounds(day: dt.date) -> tuple[float, float]:
    start = dt.datetime.combine(day, dt.time.min).timestamp()
    return start, (dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).timestamp())


def _sync_error(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {"error": "invalid error marker"}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return {"error": "unreadable error marker"}


def build(conn) -> dict:
    now = time.time()
    budget_day = config.optional_float_value("METSUKE_BUDGET_DAY", config.BUDGET_DAY)
    budget_week = config.optional_float_value("METSUKE_BUDGET_WEEK", config.BUDGET_WEEK)
    budget_month = config.optional_float_value("METSUKE_BUDGET_MONTH", config.BUDGET_MONTH)
    burn_window_s = config.int_value("METSUKE_BURN_WINDOW_S", config.BURN_WINDOW_S)
    coldcache_min = config.float_value(
        "METSUKE_COLDCACHE_MIN_USD", config.COLDCACHE_MIN_USD
    )
    local = dt.datetime.fromtimestamp(now)
    today = local.date()
    start, end = _day_bounds(today)
    today_cost, today_n = _cost(conn, start, end)
    burn_start = now - burn_window_s
    burn_cost, burn_n = _cost(conn, burn_start, now)
    burn_rate = burn_cost * 3600.0 / burn_window_s if burn_n > 0 else None
    elapsed = now - start
    baselines = []
    remaining = []
    for weeks in range(1, 4):
        day = today - dt.timedelta(days=7 * weeks)
        bstart, bend = _day_bounds(day)
        cutoff = min(bstart + elapsed, bend)
        partial, _ = _cost(conn, bstart, cutoff)
        full, _ = _cost(conn, bstart, bend)
        if conn.execute("SELECT 1 FROM request WHERE ts>=? AND ts<? LIMIT 1", (bstart, bend)).fetchone():
            baselines.append(partial)
            remaining.append(max(0.0, full - partial))
    pace = None
    landing = None
    if len(baselines) >= 2:
        median_partial = statistics.median(baselines)
        pace = today_cost / median_partial if median_partial > 0 else None
        landing = today_cost + statistics.median(remaining)

    week_start = today - dt.timedelta(days=today.weekday())
    ws, _ = _day_bounds(week_start)
    month_start = today.replace(day=1)
    ms, _ = _day_bounds(month_start)
    week_cost, _ = _cost(conn, ws, end)
    month_cost, _ = _cost(conn, ms, end)
    freshness = conn.execute("SELECT MAX(ts) FROM request").fetchone()[0]
    activity = conn.execute("SELECT MAX(ts) FROM hook_event").fetchone()[0]
    ingest_ts = conn.execute("SELECT MAX(ts) FROM ingest_log").fetchone()[0]
    sync_error = _sync_error(config.last_sync_error_path())
    reasons = []
    request_active = freshness is not None and now - freshness < 1800
    hook_active = activity is not None and now - activity < 1800
    if request_active and not hook_active:
        reasons.append("hooks_missing_during_request_activity")
    elif hook_active and not request_active:
        reasons.append("ledger_missing_during_hook_activity")
    elif request_active and hook_active and abs(freshness - activity) >= 900:
        reasons.append("request_hook_clock_gap")
    if ingest_ts is None or now - ingest_ts >= 900:
        reasons.append("ingest_not_recent")
    if sync_error is not None:
        reasons.append("last_sync_failed")
    stale = bool(reasons)

    last_prompt = None
    prow = conn.execute(
        """SELECT v.*,p.session_id FROM v_prompt_cost v
           JOIN prompt p ON p.prompt_id=v.prompt_id
           ORDER BY v.ts DESC LIMIT 1"""
    ).fetchone()
    if prow:
        rows = conn.execute("SELECT * FROM v_request_cost WHERE prompt_id=?", (prow["prompt_id"],)).fetchall()
        term, share = dominant_term(rows)
        last_prompt = {"prompt_id": prow["prompt_id"], "session_id": prow["session_id"], "cost_usd": prow["cost_usd"], "n_requests": prow["n_requests"], "dominant_term": term, "share_pct": round(share, 1), "interrupted": bool(prow["interrupted"])}

    sessions = {}
    for row in conn.execute("SELECT session_id,MAX(ts) last_ts FROM request WHERE agent_id IS NULL AND ts>=? GROUP BY session_id", (now - 43200,)):
        sid, last_ts = row["session_id"], row["last_ts"]
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM v_request_cost WHERE session_id=? AND agent_id IS NULL AND ts>=? AND ts<?", (sid, start, end)).fetchone()[0]
        latest = conn.execute(
            """SELECT COALESCE(input_tok,0)+COALESCE(cache_read_tok,0)+
                      COALESCE(cache_w5m_tok,0)+COALESCE(cache_w1h_tok,0) context_tok,
                      model,ts,cache_read_tok,cache_w5m_tok,cache_w1h_tok
               FROM request WHERE session_id=? AND agent_id IS NULL ORDER BY ts DESC LIMIT 1""",
            (sid,),
        ).fetchone()
        context_tok = latest["context_tok"] if latest else 0
        cache_write = conn.execute(
            """SELECT ts,cache_w5m_tok,cache_w1h_tok FROM request
               WHERE session_id=? AND agent_id IS NULL
                 AND (COALESCE(cache_w5m_tok,0)>0 OR COALESCE(cache_w1h_tok,0)>0)
               ORDER BY ts DESC LIMIT 1""",
            (sid,),
        ).fetchone()
        price = conn.execute(
            """SELECT in_usd,cache_w1h_x FROM price
               WHERE model=? AND date(?,'unixepoch')>=valid_from
                 AND (valid_to IS NULL OR date(?,'unixepoch')<valid_to)
               ORDER BY valid_from DESC LIMIT 1""",
            (
                latest["model"] if latest else None,
                latest["ts"] if latest else None,
                latest["ts"] if latest else None,
            ),
        ).fetchone()
        rebuild_low = round(context_tok / 1e6 * price[0] * 1.25, 2) if price else None
        rebuild_high = round(context_tok / 1e6 * price[0] * price[1], 2) if price else None
        ttl_kind = "unknown"
        ttl_min_s = ttl_max_s = None
        cache_write_ts = None
        if cache_write:
            latest_has_cache = latest and any(
                latest[key] for key in ("cache_read_tok", "cache_w5m_tok", "cache_w1h_tok")
            )
            # Cache hits refresh the preceding write duration. Use the latest
            # cache-bearing request as the expiry anchor while the latest write
            # remains the evidence for 5m/1h policy.
            cache_write_ts = latest["ts"] if latest_has_cache else cache_write["ts"]
            has_5m = bool(cache_write["cache_w5m_tok"])
            has_1h = bool(cache_write["cache_w1h_tok"])
            if has_5m and has_1h:
                ttl_kind, ttl_min_s, ttl_max_s = "mixed", 300, 3600
            elif has_1h:
                ttl_kind, ttl_min_s, ttl_max_s = "1h", 3600, 3600
                rebuild_low = rebuild_high
            elif has_5m:
                ttl_kind, ttl_min_s, ttl_max_s = "5m", 300, 300
                rebuild_high = rebuild_low
        prompt = conn.execute(
            "SELECT MAX(ts) FROM hook_event WHERE session_id=? AND kind='UserPromptSubmit'",
            (sid,),
        ).fetchone()[0]
        stopped = prompt is not None and conn.execute(
            "SELECT 1 FROM hook_event WHERE session_id=? AND kind='Stop' AND ts>=? LIMIT 1",
            (sid, prompt),
        ).fetchone()
        inflight = None
        if prompt is not None and not stopped:
            cur = conn.execute(
                """SELECT json_extract(payload_json,'$.payload.cost.total_cost_usd')
                   FROM hook_event WHERE session_id=? AND kind='statusline_sample'
                   ORDER BY ts DESC LIMIT 1""",
                (sid,),
            ).fetchone()
            initial = conn.execute(
                """SELECT json_extract(payload_json,'$.payload.cost.total_cost_usd')
                   FROM hook_event WHERE session_id=? AND kind='statusline_sample' AND ts<=?
                   ORDER BY ts DESC LIMIT 1""",
                (sid, prompt),
            ).fetchone()
            if cur and initial and cur[0] is not None and initial[0] is not None and cur[0] >= initial[0]:
                inflight = round(cur[0] - initial[0], 6)
        inflight_prompt_ts = prompt if prompt is not None and not stopped else None
        if inflight_prompt_ts is None:
            recent_prompt_rows = conn.execute(
                """SELECT v.prompt_id,v.cost_usd,v.interrupted,
                          (SELECT MAX(COALESCE(r.end_ts,r.ts)) FROM request r
                           WHERE r.prompt_id=v.prompt_id) completed_ts
                   FROM v_prompt_cost v JOIN prompt p ON p.prompt_id=v.prompt_id
                   WHERE p.session_id=? AND v.cost_usd IS NOT NULL
                   ORDER BY v.ts DESC LIMIT 3""",
                (sid,),
            ).fetchall()
        else:
            recent_prompt_rows = conn.execute(
                """SELECT v.prompt_id,v.cost_usd,v.interrupted,
                          (SELECT MAX(COALESCE(r.end_ts,r.ts)) FROM request r
                           WHERE r.prompt_id=v.prompt_id) completed_ts
                   FROM v_prompt_cost v JOIN prompt p ON p.prompt_id=v.prompt_id
                   WHERE p.session_id=? AND p.ts<? AND v.cost_usd IS NOT NULL
                   ORDER BY v.ts DESC LIMIT 3""",
                (sid, inflight_prompt_ts - 2.0),
            ).fetchall()
        sessions[sid] = {
            "last_ts": last_ts,
            "cost_today_usd": cost,
            "cache_write_ts": cache_write_ts,
            "cache_ttl_kind": ttl_kind,
            "cache_min_expires_at": cache_write_ts + ttl_min_s
            if cache_write_ts is not None and ttl_min_s is not None
            else None,
            "cache_max_expires_at": cache_write_ts + ttl_max_s
            if cache_write_ts is not None and ttl_max_s is not None
            else None,
            "ttl_remaining_s": max(0, int(cache_write_ts + ttl_max_s - now))
            if cache_write_ts is not None and ttl_max_s is not None
            else None,
            "context_tok": context_tok,
            "rebuild_cost_usd": rebuild_high,
            "rebuild_cost_low_usd": rebuild_low,
            "rebuild_cost_high_usd": rebuild_high,
            "inflight_prompt_ts": inflight_prompt_ts,
            "inflight_usd": inflight,
            "recent_prompts": [
                {
                    "prompt_id": row["prompt_id"],
                    "cost_usd": float(row["cost_usd"]),
                    "interrupted": bool(row["interrupted"]),
                    "completed_ts": row["completed_ts"],
                }
                for row in recent_prompt_rows
            ],
        }
    return {"generated_at": now, "freshness_ts": freshness, "stale": stale, "health": {"request_last_ts": freshness, "hook_last_ts": activity, "ingest_last_ts": ingest_ts, "last_sync_error": sync_error, "stale_reasons": reasons}, "thresholds": {"coldcache_min_usd": coldcache_min}, "today": {"date": today.isoformat(), "cost_usd": today_cost, "n_requests": today_n, "budget_usd": budget_day, "burn_rate_usd_h": burn_rate, "pace_ratio": pace, "landing_usd": landing}, "week": {"cost_usd": week_cost, "budget_usd": budget_week}, "month": {"cost_usd": month_cost, "budget_usd": budget_month}, "last_prompt": last_prompt, "sessions": sessions}


def _notify(title: str, msg: str) -> dict[str, str]:
    status = {"macos": "failed", "ntfy": "not_configured"}
    apple_script = """on run argv
display notification (item 2 of argv) with title (item 1 of argv)
end run"""
    try:
        completed = subprocess.run(
            ["osascript", "-e", apple_script, title, msg],
            check=False,
            capture_output=True,
            text=True,
        )
        if getattr(completed, "returncode", 0) == 0:
            status["macos"] = "accepted"
        else:
            error = (getattr(completed, "stderr", "") or "").strip()
            print(
                f"metsuke macOS notification failed: {error or 'osascript returned an error'}",
                file=sys.stderr,
            )
    except OSError as exc:
        print(f"metsuke macOS notification failed: {exc}", file=sys.stderr)

    try:
        url = config.ntfy_url_path().read_text().splitlines()[0].strip()
        if url:
            completed = subprocess.run(
                ["curl", "-fsS", "-m", "5", "-d", msg, url],
                check=False,
                capture_output=True,
                text=True,
            )
            if getattr(completed, "returncode", 0) == 0:
                status["ntfy"] = "accepted"
            else:
                status["ntfy"] = "failed"
                error = (getattr(completed, "stderr", "") or "").strip()
                print(
                    f"metsuke ntfy notification failed: {error or 'curl returned an error'}",
                    file=sys.stderr,
                )
    except FileNotFoundError:
        pass
    except (OSError, IndexError) as exc:
        status["ntfy"] = "failed"
        print(f"metsuke ntfy notification failed: {exc}", file=sys.stderr)
    return status


def _record_nudge(rule: str, sid: str, detail: dict, now: float) -> None:
    spool = config.hooks_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    payload = {"metsuke_event": "nudge_fired", "metsuke_ts": now, "payload": {"rule": rule, "session_id": sid, "detail": detail}}
    path = spool / f"{time.time_ns()}-{os.getpid()}-nudge-{rule}.ndjson"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(path, config.FILE_MODE)


def _cooldown(conn, key: str) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='nudges_notified'").fetchone()
    values = json.loads(row[0]) if row else []
    if key in values:
        return False
    values = (values + [key])[-200:]
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('nudges_notified',?)", (json.dumps(values),))
    return True


def _daily_allowed(conn, rule: str, now: float) -> bool:
    date = dt.datetime.fromtimestamp(now).date().isoformat()
    row = conn.execute("SELECT value FROM meta WHERE key='nudge_daily'").fetchone()
    data = json.loads(row[0]) if row else {}
    if data.get("date") != date:
        data = {"date": date, "counts": {}}
    counts = data["counts"]
    cap = config.int_value("METSUKE_NUDGE_DAILY_CAP", config.NUDGE_DAILY_CAP)
    if counts.get(rule, 0) >= cap:
        return False
    counts[rule] = counts.get(rule, 0) + 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('nudge_daily',?)", (json.dumps(data),))
    return True


def _notify_nudges(conn, result: dict) -> None:
    now = time.time()
    runaway_usd = config.float_value("METSUKE_RUNAWAY_USD", config.RUNAWAY_USD)
    ttl_gap_s = config.int_value(
        "METSUKE_TTL_PRENOTIFY_GAP_S", config.TTL_PRENOTIFY_GAP_S
    )
    coldcache_min = config.float_value(
        "METSUKE_COLDCACHE_MIN_USD", config.COLDCACHE_MIN_USD
    )
    for sid, session in result["sessions"].items():
        inflight = session.get("inflight_usd")
        prompt_ts = session.get("inflight_prompt_ts")
        if inflight is not None and inflight >= runaway_usd:
            key = f"runaway:{sid}:{int(prompt_ts)}"
            if _cooldown(conn, key) and _daily_allowed(conn, "runaway_guard", now):
                msg = f"🚨 session {sid[:8]}… 進行中プロンプト ${inflight:.2f}"
                notification = _notify("metsuke runaway guard", msg)
                _record_nudge(
                    "runaway_guard",
                    sid,
                    {"cost_usd": inflight, "notification": notification},
                    now,
                )
        write_ts = session.get("cache_write_ts")
        gap = now - write_ts if write_ts is not None else None
        rebuild = session.get("rebuild_cost_usd")
        if (
            gap is not None
            and session.get("cache_ttl_kind") in {"1h", "mixed"}
            and ttl_gap_s <= gap < 3600
            and rebuild is not None
            and rebuild >= coldcache_min
            and session.get("inflight_prompt_ts") is None
        ):
            key = f"ttl:{sid}:{int(session['last_ts'])}"
            if _cooldown(conn, key) and _daily_allowed(conn, "ttl_prenotify", now):
                minutes = max(0, int((3600 - gap) / 60))
                msg = f"⏳ session {sid[:8]}… キャッシュ残{minutes}分（再構築≈${rebuild:.2f}）。続けるなら今、終わりなら放置か/handoff"
                notification = _notify("metsuke cache TTL", msg)
                _record_nudge(
                    "ttl_prenotify",
                    sid,
                    {
                        "gap_s": gap,
                        "rebuild_cost_usd": rebuild,
                        "notification": notification,
                    },
                    now,
                )
    conn.commit()


def _prepare_prompt_details(conn, result: dict) -> None:
    """Prepare one local trace per recently active session with a costly prompt."""
    from . import trace_html

    now = time.time()
    warn_usd = config.float_value("METSUKE_PROMPT_WARN_USD", config.PROMPT_WARN_USD)
    for sid, session in result["sessions"].items():
        prompts = session.get("recent_prompts") or []
        if not prompts:
            continue
        target = trace_html.target_path(sid)
        if target is None:
            continue
        has_costly_prompt = any(prompt.get("cost_usd", 0) >= warn_usd for prompt in prompts)
        if not has_costly_prompt:
            continue
        active_recently = now - session.get("last_ts", 0) <= 600
        needs_refresh = not target.is_file()
        if not needs_refresh:
            try:
                needs_refresh = target.stat().st_mtime < session.get("last_ts", 0)
            except OSError:
                needs_refresh = True
        if active_recently and needs_refresh:
            try:
                generated = trace_html.generate(sid, conn=conn, record=False)
                if generated is not None:
                    target = generated
                    needs_refresh = False
            except Exception as exc:
                # Detail HTML is an optional statusline affordance. Never let
                # transcript reconstruction failures break ingestion.
                print(f"metsuke prompt detail generation failed: {exc}", file=sys.stderr)
        if needs_refresh or not target.is_file():
            continue
        base_url = target.resolve().as_uri()
        for prompt in prompts:
            if prompt.get("cost_usd", 0) >= warn_usd:
                prompt["detail_url"] = (
                    f"{base_url}#prompt={quote(prompt['prompt_id'], safe='')}"
                )


def _notify_receipt(conn, result: dict) -> None:
    if config.int_value("METSUKE_RECEIPT_NOTIFY_ENABLED", 0) != 1:
        return
    prompt = result.get("last_prompt")
    if not prompt:
        return
    session = result["sessions"].get(prompt.get("session_id"))
    if not session or session.get("inflight_prompt_ts") is not None:
        return
    crit_usd = config.float_value("METSUKE_PROMPT_CRIT_USD", config.PROMPT_CRIT_USD)
    ts = conn.execute("SELECT MAX(ts) FROM request WHERE prompt_id=?", (prompt["prompt_id"],)).fetchone()
    if not ts or time.time() - ts[0] > 600 or prompt["cost_usd"] < crit_usd:
        return
    row = conn.execute("SELECT value FROM meta WHERE key='receipts_notified'").fetchone()
    notified = json.loads(row[0]) if row else []
    if prompt["prompt_id"] in notified:
        return
    prompt_short = prompt["prompt_id"][:8]
    labels = {
        "cache_read": "キャッシュ読み込み",
        "cache_creation": "キャッシュ作成",
        "output": "出力",
        "input": "入力",
        "server_tool": "サーバーツール",
    }
    dominant = labels.get(prompt["dominant_term"], prompt["dominant_term"])
    message = (
        f"API換算 ${prompt['cost_usd']:.2f}・モデル呼出{prompt['n_requests']}回・"
        f"主因: {dominant}・詳細: metsuke explain {prompt_short} --html --open"
    )
    _notify("metsuke 高コストプロンプト", message)
    notified = (notified + [prompt["prompt_id"]])[-200:]
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('receipts_notified',?)", (json.dumps(notified),))
    conn.commit()


def write(conn, notify: bool) -> dict:
    result = build(conn)
    if notify:
        _prepare_prompt_details(conn, result)
    path = config.state_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    os.chmod(tmp, config.FILE_MODE)
    os.replace(tmp, path)
    if notify:
        _notify_receipt(conn, result)
        _notify_nudges(conn, result)
    return result
