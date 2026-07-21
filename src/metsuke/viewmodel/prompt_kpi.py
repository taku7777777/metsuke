"""Canonical dashboard prompt KPI (ADR 0011)."""

from __future__ import annotations

import sqlite3

from .common import Window


def prompt_kpi_sql(alias: str = "r") -> str:
    """Canonical SQL expression for dashboard cost-bearing prompt count."""
    return f"COUNT(DISTINCT {alias}.prompt_id)"


def count_cost_bearing_prompts(
    conn: sqlite3.Connection, window: Window | None = None
) -> int:
    """Count distinct prompt IDs represented by non-synthetic priced-view requests.

    ``v_request_cost`` already excludes synthetic requests.  A prompt-table count is
    intentionally not accepted here because control/UI prompts can have no request.
    """
    sql = f"SELECT {prompt_kpi_sql()} FROM v_request_cost r"
    params: list[str] = []
    where = ["r.prompt_id IS NOT NULL"]
    if window is not None:
        lower, upper = window.sql_bounds()
        where.extend(
            [
                "datetime(r.ts,'unixepoch','localtime')>=?",
                "datetime(r.ts,'unixepoch','localtime')<?",
            ]
        )
        params.extend((lower, upper))
        if window.project is not None:
            sql += " JOIN session s USING(session_id)"
            where.append("s.project=?")
            params.append(window.project)
    row = conn.execute(sql + " WHERE " + " AND ".join(where), params).fetchone()
    return int(row[0])
