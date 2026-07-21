"""Ledger-only prompt detail model (the numeric part of ``metsuke explain``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .common import Money, local_timezone


@dataclass(frozen=True)
class DominantTerm:
    term: str
    share_pct: float


@dataclass(frozen=True)
class DominantComponent:
    """One canonical cost component used by prompt detail and period ranking."""

    name: str
    terms: tuple[tuple[str, ...], ...]
    trailing_factors: tuple[str, ...] = ()
    scale: float = 1.0


# Order is also the deterministic tie-break order used by both consumers.
DOMINANT_COMPONENTS = (
    DominantComponent(
        "cache_read",
        (("cache_read_tok", "in_usd", "cache_read_x"),),
        ("price_factor",),
    ),
    DominantComponent(
        "cache_creation",
        (
            ("cache_w5m_tok", "cache_w5m_x"),
            ("cache_w1h_tok", "cache_w1h_x"),
        ),
        ("in_usd", "price_factor"),
    ),
    DominantComponent("output", (("output_tok", "out_usd"),), ("price_factor",)),
    DominantComponent("input", (("input_tok", "in_usd"),), ("price_factor",)),
    DominantComponent("server_tool", (("server_tool_usd",),), scale=1e6),
)


@dataclass(frozen=True)
class PromptRequest:
    request_id: str
    ts: float
    model: str | None
    agent_id: str | None
    input_tok: int | None
    cache_read_tok: int | None
    cache_w5m_tok: int | None
    cache_w1h_tok: int | None
    output_tok: int | None
    interrupted: bool
    tool_count: int
    amount: Money


@dataclass(frozen=True)
class PromptModel:
    prompt_id: str
    session_id: str
    text: str | None
    timezone: str
    amount: Money
    unknown_cost_request_count: int
    dominant: DominantTerm
    requests: tuple[PromptRequest, ...]


def _value(row, key, default=0):
    try:
        value = row[key]
    except (IndexError, KeyError):
        value = default
    return default if value is None else value


def dominant_component_names() -> tuple[str, ...]:
    return tuple(component.name for component in DOMINANT_COMPONENTS)


def _component_value(row, component: DominantComponent) -> float:
    terms = []
    for factor_columns in component.terms:
        term = _value(row, factor_columns[0])
        for factor_column in factor_columns[1:]:
            term *= _value(row, factor_column)
        terms.append(term)
    value = sum(terms)
    for factor_column in component.trailing_factors:
        value *= _value(row, factor_column, 1 if factor_column == "price_factor" else 0)
    return value * component.scale


def dominant_component_totals(rows: Sequence[Any]) -> dict[str, float]:
    return {
        component.name: sum(_component_value(row, component) for row in rows)
        for component in DOMINANT_COMPONENTS
    }


def dominant_component_name(values: Mapping[str, float | None]) -> str:
    return max(
        DOMINANT_COMPONENTS,
        key=lambda component: values.get(component.name) or 0,
    ).name


def _sql_value(column: str, alias: str) -> str:
    return f"coalesce({alias}.{column},0)"


def dominant_component_sql(alias: str = "r", column_prefix: str = "dominant_") -> str:
    """Return aggregate SQL columns derived from the canonical component definition."""

    columns = []
    for component in DOMINANT_COMPONENTS:
        terms = [
            "*".join(_sql_value(column, alias) for column in factor_columns)
            for factor_columns in component.terms
        ]
        expression = f"({' + '.join(terms)})"
        for factor_column in component.trailing_factors:
            default = 1 if factor_column == "price_factor" else 0
            expression += f"*coalesce({alias}.{factor_column},{default})"
        if component.scale != 1:
            expression += f"*{component.scale:g}"
        columns.append(f"sum({expression}) as {column_prefix}{component.name}")
    return ",".join(columns)


def dominant_term(rows) -> DominantTerm:
    sums = dominant_component_totals(rows)
    term = dominant_component_name(sums)
    total = sum(_value(row, "cost_usd") for row in rows) * 1e6
    return DominantTerm(term, sums[term] / total * 100 if total else 0.0)


def query(conn, prompt_id: str) -> PromptModel | None:
    prompt = conn.execute("SELECT * FROM prompt WHERE prompt_id=?", (prompt_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM v_request_cost WHERE prompt_id=? ORDER BY ts", (prompt_id,)
    ).fetchall()
    if not rows:
        return None
    tools = {
        row["request_id"]: row["n"]
        for row in conn.execute(
            "SELECT request_id,COUNT(*) n FROM tool_call WHERE prompt_id=? GROUP BY request_id",
            (prompt_id,),
        )
    }
    requests = tuple(
        PromptRequest(
            row["request_id"],
            row["ts"],
            row["model"],
            row["agent_id"],
            row["input_tok"],
            row["cache_read_tok"],
            row["cache_w5m_tok"],
            row["cache_w1h_tok"],
            row["output_tok"],
            bool(row["is_interrupted"]),
            tools.get(row["request_id"], 0),
            Money.from_raw(row["cost_usd"]),
        )
        for row in rows
    )
    known_total = sum(row["cost_usd"] for row in rows if row["cost_usd"] is not None)
    return PromptModel(
        prompt_id,
        rows[0]["session_id"],
        prompt["text"] if prompt else None,
        local_timezone(),
        Money.from_raw(known_total),
        sum(row["cost_usd"] is None for row in rows),
        dominant_term(rows),
        requests,
    )
