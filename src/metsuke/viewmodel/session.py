"""Ledger-only session summary and prompt-list model."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .common import Money, local_timezone


@dataclass(frozen=True)
class SessionPrompt:
    prompt_id: str
    ts: float
    text: str | None
    request_count: int
    amount: Money
    unknown_cost_request_count: int


@dataclass(frozen=True)
class SessionModel:
    session_id: str
    project: str | None
    first_ts: float
    last_ts: float
    timezone: str
    request_count: int
    amount: Money
    unknown_cost_request_count: int
    models: tuple[tuple[str, int], ...]
    prompts: tuple[SessionPrompt, ...]


def query(conn, session_id: str) -> SessionModel | None:
    session = conn.execute("SELECT * FROM session WHERE session_id=?", (session_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM v_request_cost WHERE session_id=? ORDER BY ts", (session_id,)
    ).fetchall()
    if session is None or not rows:
        return None
    prompt_rows = conn.execute(
        """SELECT r.prompt_id,MIN(r.ts) ts,MAX(p.text) text,COUNT(*) requests,
                  SUM(r.cost_usd) cost,SUM(r.cost_usd IS NULL) unknown
           FROM v_request_cost r LEFT JOIN prompt p USING(prompt_id)
           WHERE r.session_id=? AND r.prompt_id IS NOT NULL
           GROUP BY r.prompt_id ORDER BY ts,r.prompt_id""",
        (session_id,),
    ).fetchall()
    prompts = tuple(
        SessionPrompt(
            row["prompt_id"],
            row["ts"],
            row["text"],
            row["requests"],
            Money.from_raw(row["cost"]),
            row["unknown"] or 0,
        )
        for row in prompt_rows
    )
    models = Counter(row["model"] or "?" for row in rows)
    known_total = sum(row["cost_usd"] for row in rows if row["cost_usd"] is not None)
    return SessionModel(
        session_id,
        session["project"],
        min(row["ts"] for row in rows),
        max(row["ts"] for row in rows),
        local_timezone(),
        len(rows),
        Money.from_raw(known_total),
        sum(row["cost_usd"] is None for row in rows),
        tuple(sorted(models.items())),
        prompts,
    )
