"""Dashboard overview query model shared by future renderers."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from .common import (
    Money,
    Page,
    Window,
    json_real_sql,
    local_timezone,
    restore_json_reals,
    scoped_requests_cte,
    window_totals_sql,
)
from .prompt_kpi import prompt_kpi_sql


@dataclass(frozen=True)
class Comparison:
    current: float | int
    previous: float | int
    percent_change: float | None
    display: str


@dataclass(frozen=True)
class Kpi:
    name: str
    value: float | int
    display: str
    comparison: Comparison


@dataclass(frozen=True)
class CostPart:
    name: str
    amount: Money


@dataclass(frozen=True)
class DailyCost:
    day: dt.date
    amount: Money
    selected: bool = False


@dataclass(frozen=True)
class RankedPrompt:
    prompt_id: str
    session_id: str
    project: str | None
    ts: float
    text: str | None
    request_count: int
    context_peak: int
    amount: Money


@dataclass(frozen=True)
class RankedSession:
    session_id: str
    project: str | None
    first_ts: float
    last_ts: float
    request_count: int
    prompt_count: int
    amount: Money


@dataclass(frozen=True)
class CacheRebuild:
    cause: str
    request_count: int
    amount: Money


@dataclass(frozen=True)
class OverviewModel:
    window: Window
    previous_window: Window
    timezone: str
    kpis: tuple[Kpi, ...]
    daily_costs: tuple[DailyCost, ...]
    cost_parts: tuple[CostPart, ...]
    top_prompts: tuple[RankedPrompt, ...]
    top_sessions: tuple[RankedSession, ...]
    cache_rebuilds: tuple[CacheRebuild, ...]
    unknown_cost_request_count: int


def _totals(values: dict) -> dict[str, float | int]:
    return {
        "cost": values["cost_usd"],
        "requests": values["request_count"],
        "prompts": values["prompt_count"],
        "sessions": values["session_count"],
        "projects": values["project_count"],
        "unknown": values["unknown_cost_request_count"],
    }


def _comparison(current: float | int, previous: float | int) -> Comparison:
    if previous == 0:
        return Comparison(current, previous, None, "比較不能")
    change = (current - previous) / previous * 100
    return Comparison(current, previous, change, f"{change:+.1f}%")


def _page_clause(page: Page | None, tie_break: str) -> tuple[str, list[int]]:
    if page is None:
        return "", []
    if page.sort != "cost":
        raise ValueError("overview currently supports cost sorting only")
    direction = "DESC" if page.order == "desc" else "ASC"
    return f" ORDER BY cost {direction},{tie_break} LIMIT ? OFFSET ?", [page.limit, page.offset]


def _context_window(window: Window, today: dt.date) -> tuple[dt.date, dt.date]:
    """31 consecutive days centred on the selection midpoint, right-capped at today.

    The chart always shows a fixed ~month of context so the reader can see where a
    selection (even a single day) sits relative to the recent past. When the ideal
    right edge would fall in the future the whole window shifts left to end at today;
    days before the earliest data simply render as $0 (no left clamp).
    """
    center = window.start + (window.end - window.start) // 2
    end = min(center + dt.timedelta(days=15), today)
    return end - dt.timedelta(days=30), end


def query(
    conn, window: Window, page: Page | None = None, *, today: dt.date | None = None
) -> OverviewModel:
    previous = window.previous()
    current_lower = window.sql_bounds()[0]
    if today is None:
        today = window.end
    context_start, context_end = _context_window(window, today)
    context_lower = f"{context_start.isoformat()} 00:00:00"
    context_upper = f"{(context_end + dt.timedelta(days=1)).isoformat()} 00:00:00"
    context_project = " AND s.project=?" if window.project is not None else ""
    context_project_params = [window.project] if window.project is not None else []
    scoped_cte, params = scoped_requests_cte(
        window,
        include_previous=True,
    )
    prompt_page_sql, prompt_page_params = _page_clause(page, "r.prompt_id ASC")
    session_page_sql, session_page_params = _page_clause(page, "r.session_id DESC")
    prompt_order = (
        prompt_page_sql if page is not None else " ORDER BY cost DESC,r.prompt_id ASC LIMIT 40"
    )
    session_order = (
        session_page_sql
        if page is not None
        else " ORDER BY cost DESC,r.session_id DESC LIMIT 40"
    )
    rows = conn.execute(
        f"""WITH RECURSIVE {scoped_cte},
        context_spine(day) AS (
            SELECT date(?)
            UNION ALL
            SELECT date(day,'+1 day') FROM context_spine WHERE day<?
        ),
        context_costs AS MATERIALIZED (
            SELECT date(r.ts,'unixepoch','localtime') day,SUM(r.cost_usd) cost,
              COUNT(*) requests,COUNT(r.cost_usd) known_costs
            FROM v_request_cost r LEFT JOIN session s USING(session_id)
            WHERE datetime(r.ts,'unixepoch','localtime')>=?
              AND datetime(r.ts,'unixepoch','localtime')<?{context_project}
            GROUP BY day
        ),
        context_daily AS (
            SELECT sp.day day,cc.cost cost,COALESCE(cc.requests,0) requests,
              COALESCE(cc.known_costs,0) known_costs
            FROM context_spine sp LEFT JOIN context_costs cc USING(day)
        ),
        current AS NOT MATERIALIZED (
            SELECT * FROM scoped
            WHERE datetime(ts,'unixepoch','localtime')>=?
        ),
        previous AS NOT MATERIALIZED (
            SELECT * FROM scoped
            WHERE datetime(ts,'unixepoch','localtime')<?
        ),
        prompt_rank AS MATERIALIZED (
            SELECT r.prompt_id,MIN(r.session_id) session_id,r.scoped_project project,
              MIN(r.ts) ts,MAX(p.text) text,COUNT(*) requests,
              MAX(COALESCE(r.input_tok,0)+COALESCE(r.cache_read_tok,0)
                  +COALESCE(r.cache_w5m_tok,0)+COALESCE(r.cache_w1h_tok,0)) peak,
              SUM(r.cost_usd) cost
            FROM current r LEFT JOIN prompt p USING(prompt_id)
            WHERE r.prompt_id IS NOT NULL AND r.scoped_session_id IS NOT NULL
            GROUP BY r.prompt_id{prompt_order}
        ),
        session_rank AS MATERIALIZED (
            SELECT r.session_id,r.scoped_project project,MIN(r.ts) first_ts,
              MAX(r.ts) last_ts,COUNT(*) requests,
              COUNT(DISTINCT r.prompt_id) prompts,SUM(r.cost_usd) cost
            FROM current r WHERE r.scoped_session_id IS NOT NULL
            GROUP BY r.session_id{session_order}
        ),
        cache_rank AS MATERIALIZED (
            SELECT ci.cause,COUNT(*) requests,COALESCE(SUM(r.cache_write_usd),0) cost
            FROM v_cache_identity ci JOIN current r USING(request_id)
            WHERE r.scoped_session_id IS NOT NULL
            GROUP BY ci.cause ORDER BY cost DESC,ci.cause
        ),
        parts AS MATERIALIZED (
            SELECT
              COALESCE(SUM(r.input_tok*r.in_usd*r.price_factor/1e6),0) input,
              COALESCE(SUM(COALESCE(r.output_tok,0)*r.out_usd*r.price_factor/1e6),0) output,
              COALESCE(SUM(r.cache_read_tok*r.in_usd*r.cache_read_x*r.price_factor/1e6),0) cache_read,
              COALESCE(SUM(r.cache_w5m_tok*r.in_usd*r.cache_w5m_x*r.price_factor/1e6),0) cache_w5m,
              COALESCE(SUM(r.cache_w1h_tok*r.in_usd*r.cache_w1h_x*r.price_factor/1e6),0) cache_w1h,
              COALESCE(SUM(r.server_tool_usd),0) server_tool FROM current r
        )
        SELECT 'current_totals',json_object(
          'cost_usd',{json_real_sql('cost_usd')},'request_count',request_count,
          'session_count',session_count,'project_count',project_count,
          'unknown_cost_request_count',unknown_cost_request_count,
          'prompt_count',prompt_count)
        FROM (SELECT {window_totals_sql()},{prompt_kpi_sql()} AS prompt_count FROM current r)
        UNION ALL
        SELECT 'previous_totals',json_object(
          'cost_usd',{json_real_sql('cost_usd')},'request_count',request_count,
          'session_count',session_count,'project_count',project_count,
          'unknown_cost_request_count',unknown_cost_request_count,
          'prompt_count',prompt_count)
        FROM (SELECT {window_totals_sql()},{prompt_kpi_sql()} AS prompt_count FROM previous r)
        UNION ALL
        SELECT 'parts',json_object(
          'input',{json_real_sql('input')},'output',{json_real_sql('output')},
          'cache_read',{json_real_sql('cache_read')},
          'cache_w5m',{json_real_sql('cache_w5m')},
          'cache_w1h',{json_real_sql('cache_w1h')},
          'server_tool',{json_real_sql('server_tool')}) FROM parts
        UNION ALL
        SELECT 'context',json_object(
          'day',day,'cost',{json_real_sql('cost')},
          'requests',requests,'known_costs',known_costs) FROM context_daily
        UNION ALL
        SELECT 'prompt',json_object(
          'prompt_id',prompt_id,'session_id',session_id,'project',project,
          'ts',{json_real_sql('ts')},'text',text,'requests',requests,'peak',peak,
          'cost',{json_real_sql('cost')})
        FROM prompt_rank
        UNION ALL
        SELECT 'session',json_object(
          'session_id',session_id,'project',project,
          'first_ts',{json_real_sql('first_ts')},'last_ts',{json_real_sql('last_ts')},
          'requests',requests,'prompts',prompts,'cost',{json_real_sql('cost')})
        FROM session_rank
        UNION ALL
        SELECT 'cache',json_object(
          'cause',cause,'requests',requests,'cost',{json_real_sql('cost')})
        FROM cache_rank""",
        [
            *params,
            context_start.isoformat(),
            context_end.isoformat(),
            context_lower,
            context_upper,
            *context_project_params,
            current_lower,
            current_lower,
            *prompt_page_params,
            *session_page_params,
        ],
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for kind, payload in rows:
        grouped.setdefault(kind, []).append(json.loads(payload))
    restore_json_reals(grouped["current_totals"], "cost_usd")
    restore_json_reals(grouped["previous_totals"], "cost_usd")
    restore_json_reals(
        grouped["parts"],
        "input",
        "output",
        "cache_read",
        "cache_w5m",
        "cache_w1h",
        "server_tool",
    )
    restore_json_reals(grouped.get("prompt", []), "ts", "cost")
    restore_json_reals(grouped.get("session", []), "first_ts", "last_ts", "cost")
    restore_json_reals(grouped.get("cache", []), "cost")
    restore_json_reals(grouped.get("context", []), "cost")
    current_totals = _totals(grouped["current_totals"][0])
    previous_totals = _totals(grouped["previous_totals"][0])
    kpis = []
    labels = {
        "cost": "API換算コスト",
        "prompts": "コスト発生prompt",
        "requests": "request",
        "sessions": "session",
        "projects": "project",
    }
    for key in ("cost", "prompts", "requests", "sessions", "projects"):
        value = current_totals[key]
        shown = Money.from_raw(float(value)).display if key == "cost" else f"{value:,}"
        kpis.append(Kpi(labels[key], value, shown, _comparison(value, previous_totals[key])))

    parts = grouped["parts"][0]
    cost_parts = tuple(
        CostPart(name, Money.from_raw(parts[name]))
        for name in (
            "input",
            "output",
            "cache_read",
            "cache_w5m",
            "cache_w1h",
            "server_tool",
        )
    )
    context_rows = {row["day"]: row for row in grouped.get("context", [])}

    def _daily_amount(row: dict | None) -> Money:
        # The date spine guarantees a row per day. A day with no requests is a real
        # $0; a day whose requests all lack a known price is genuinely unknown (—).
        if row is None or not row["requests"]:
            return Money.from_raw(0.0)
        if not row["known_costs"]:
            return Money.from_raw(None)
        return Money.from_raw(row["cost"])

    daily_costs = tuple(
        DailyCost(
            day,
            _daily_amount(context_rows.get(day.isoformat())),
            selected=window.start <= day <= window.end,
        )
        for day in (
            context_start + dt.timedelta(days=index) for index in range(31)
        )
    )

    top_prompts = tuple(
        RankedPrompt(
            row["prompt_id"],
            row["session_id"],
            row["project"],
            row["ts"],
            row["text"],
            row["requests"],
            row["peak"],
            Money.from_raw(row["cost"]),
        )
        for row in grouped.get("prompt", [])
    )
    top_sessions = tuple(
        RankedSession(
            row["session_id"],
            row["project"],
            row["first_ts"],
            row["last_ts"],
            row["requests"],
            row["prompts"],
            Money.from_raw(row["cost"]),
        )
        for row in grouped.get("session", [])
    )
    cache_rebuilds = tuple(
        CacheRebuild(row["cause"], row["requests"], Money.from_raw(row["cost"]))
        for row in grouped.get("cache", [])
    )
    return OverviewModel(
        window,
        previous,
        local_timezone(),
        tuple(kpis),
        daily_costs,
        cost_parts,
        top_prompts,
        top_sessions,
        cache_rebuilds,
        int(current_totals["unknown"]),
    )
