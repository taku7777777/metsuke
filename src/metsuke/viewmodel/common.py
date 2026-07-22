"""Immutable, JSON-serializable primitives shared by every view model."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal

from metsuke.project import display_name


def project_name(value: str | None) -> str:
    """Expose the shared project display rule to view models."""
    return display_name(value)


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date
    project: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("window start must not be after end")

    def sql_bounds(self) -> tuple[str, str]:
        return (
            f"{self.start.isoformat()} 00:00:00",
            f"{(self.end + dt.timedelta(days=1)).isoformat()} 00:00:00",
        )

    def previous(self) -> Window:
        days = (self.end - self.start).days + 1
        previous_end = self.start - dt.timedelta(days=1)
        previous_start = previous_end - dt.timedelta(days=days - 1)
        return Window(previous_start, previous_end, self.project)


@dataclass(frozen=True)
class Page:
    limit: int = 40
    page: int = 1
    sort: str = "cost"
    order: Literal["asc", "desc"] = "desc"

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 200:
            raise ValueError("page limit must be between 1 and 200")
        if self.page < 1:
            raise ValueError("page number must be at least 1")
        if self.order not in {"asc", "desc"}:
            raise ValueError("page order must be asc or desc")
        if self.sort not in {"cost", "time", "requests", "prompts", "project", "context"}:
            raise ValueError("page sort key is not allowlisted")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


@dataclass(frozen=True)
class Money:
    raw: float | None
    display: str

    @classmethod
    def from_raw(cls, value: float | None) -> Money:
        return cls(value, "—" if value is None else f"${value:,.2f}")

    def __str__(self) -> str:
        return self.display

    def __format__(self, spec: str) -> str:
        return format(self.display, spec)


@dataclass(frozen=True)
class WindowTotals:
    cost_usd: float
    request_count: int
    session_count: int
    project_count: int
    unknown_cost_request_count: int


SCOPED_REQUEST_COLUMNS = (
    "request_id",
    "session_id",
    "prompt_id",
    "ts",
    "agent_id",
    "is_interrupted",
    "input_tok",
    "output_tok",
    "cache_read_tok",
    "cache_w5m_tok",
    "cache_w1h_tok",
    "in_usd",
    "out_usd",
    "cache_read_x",
    "cache_w5m_x",
    "cache_w1h_x",
    "price_factor",
    "server_tool_usd",
    "cost_usd",
    "cache_write_usd",
)


def scoped_requests_cte(
    window: Window,
    *,
    include_previous: bool = False,
    columns: tuple[str, ...] = SCOPED_REQUEST_COLUMNS,
) -> tuple[str, list[str]]:
    """Build one materialized, project-aware evaluation of ``v_request_cost``."""
    selected = window.previous() if include_previous else window
    lower = selected.sql_bounds()[0]
    upper = window.sql_bounds()[1]
    project = " AND s.project=?" if window.project is not None else ""
    params = [lower, upper]
    if window.project is not None:
        params.append(window.project)
    selected_columns = ",".join(f"r.{column}" for column in columns)
    return (
        f"""scoped AS MATERIALIZED (
            SELECT {selected_columns},s.project AS scoped_project,
              s.session_id AS scoped_session_id
            FROM v_request_cost r LEFT JOIN session s USING(session_id)
            WHERE datetime(r.ts,'unixepoch','localtime')>=?
              AND datetime(r.ts,'unixepoch','localtime')<?{project}
        )""",
        params,
    )


def window_totals_sql(alias: str = "r") -> str:
    """Canonical total/count columns for a request relation with ``scoped_project``."""
    return f"""COALESCE(SUM({alias}.cost_usd),0) AS cost_usd,
        COUNT(*) AS request_count,
        COUNT(DISTINCT {alias}.session_id) AS session_count,
        COUNT(DISTINCT {alias}.scoped_project) AS project_count,
        COALESCE(SUM({alias}.cost_usd IS NULL),0) AS unknown_cost_request_count"""


def window_totals_from_row(row) -> WindowTotals:
    return WindowTotals(
        row["cost_usd"],
        row["request_count"],
        row["session_count"],
        row["project_count"],
        row["unknown_cost_request_count"],
    )


def json_real_sql(expression: str) -> str:
    """Encode a SQLite REAL as round-trip-safe JSON text instead of lossy JSON numeric."""
    return (
        f"CASE WHEN {expression} IS NULL THEN NULL "
        f"ELSE printf('%!.17g',{expression}) END"
    )


def restore_json_reals(rows: list[dict], *fields: str) -> None:
    for row in rows:
        for field in fields:
            if row.get(field) is not None:
                row[field] = float(row[field])


def query_window_totals(conn, window: Window) -> WindowTotals:
    """Return the canonical total/count aggregation for one local-date window."""
    cte, params = scoped_requests_cte(
        window,
        columns=("session_id", "cost_usd"),
    )
    row = conn.execute(
        f"WITH {cte} SELECT {window_totals_sql()} FROM scoped r",
        params,
    ).fetchone()
    return window_totals_from_row(row)


@dataclass(frozen=True)
class FrozenMap:
    items: tuple[tuple[str, Any], ...]


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenMap(tuple((str(key), freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, FrozenMap):
        return {key: thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class Node:
    kind: str
    args: tuple[Any, ...] = ()
    kwargs: FrozenMap = FrozenMap(())


@dataclass(frozen=True)
class Cell:
    text: str | Money = ""
    cls: str = ""
    sort: str | float | None = None
    title: str | None = None
    bar: float | None = None
    content: Node | None = None
    clip: str = ""
    dot: str | None = None
    warn: bool = False


@dataclass(frozen=True)
class Column:
    label: str
    cls: str = ""
    sortable: bool = False
    sort_dir: str = ""


@dataclass(frozen=True)
class Row:
    cells: tuple[Cell, ...]
    highlight: bool = False

    def __init__(self, cells, highlight: bool = False):
        object.__setattr__(self, "cells", tuple(cells))
        object.__setattr__(self, "highlight", highlight)


@dataclass(frozen=True)
class LegacyViewModel:
    title: str
    period: str
    total: Node | str
    body: Node
    timezone: str


def node(kind: str, *args, **kwargs) -> Node:
    return Node(kind, tuple(freeze(arg) for arg in args), freeze(kwargs))


def table(columns, rows, *, foot=None) -> Node:
    frozen_rows = tuple(row if isinstance(row, Row) else Row(row) for row in rows)
    return node("table", tuple(columns), frozen_rows, foot=None if foot is None else tuple(foot))


def _primitive(kind):
    def make(*args, **kwargs):
        return node(kind, *args, **kwargs)

    return make


card = _primitive("card")
insight = _primitive("insight")
insight_body = _primitive("insight_body")
legend = _primitive("legend")
tabs = _primitive("tabs")
panel = _primitive("panel")
grain_tabs = _primitive("grain_tabs")
grain_panel = _primitive("grain_panel")
heading = _primitive("heading")
code = _primitive("code")
clip = _primitive("clip")
plain = _primitive("plain")
warning = _primitive("warning")
code_lines = _primitive("code_lines")
text_block = _primitive("text_block")
block = _primitive("block")
stacked_bars = _primitive("stacked_bars")
cache_balance = _primitive("cache_balance")
volume_chart = _primitive("volume_chart")
line_chart = _primitive("line_chart")


def join(*parts) -> Node:
    return node("join", *parts)


def money(value: float) -> Money:
    return Money.from_raw(value)


def local_timezone() -> str:
    return dt.datetime.now().astimezone().tzname() or "local"


def to_jsonable(value: Any) -> Any:
    """Convert immutable DTOs to JSON-native values without invoking a renderer."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, FrozenMap):
        return {key: to_jsonable(item) for key, item in value.items}
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, sqlite3.Row):
        # Chart nodes (trend's volume_chart) carry raw sqlite3.Row markers/regimes straight
        # from fetchall(). A Row is not a dict/list/tuple/dataclass, so it would otherwise
        # fall through to the identity return and break json.dumps. Serialize it as a
        # column->value object (self-describing, matching how render.py reads row["field"]).
        # Purely additive: no existing model embeds a Row, and every value is passed through
        # to_jsonable unchanged, so no number is altered.
        return {key: to_jsonable(value[key]) for key in value.keys()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    return value
