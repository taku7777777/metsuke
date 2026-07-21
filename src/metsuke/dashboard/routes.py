"""Dashboard URL contract and request-local view-model orchestration.

This module deliberately returns presentation-neutral response values.  All HTML
serialization lives in :mod:`metsuke.dashboard.pages`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode

from ..viewmodel import cache, dist, overview, period, prompt, session, trend
from ..viewmodel.common import Page, Window
from . import pages
from .db import DashboardDatabaseError, LedgerBusyError, LedgerNotFoundError, connect_dashboard

ALLOWED_VIEWS = frozenset({"overview", "period", "trend", "cache", "dist"})
ALLOWED_SORTS = frozenset({"cost"})
ALLOWED_ORDERS = frozenset({"asc", "desc"})
ALLOWED_KEYS = frozenset({"view", "from", "to", "project", "range", "limit", "page", "sort", "order"})
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")
BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
STALE_AFTER_SECONDS = 15 * 60
MAX_HTML_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class DashboardResponse:
    status: int
    body: bytes
    headers: dict[str, str]
    content_type: str = "text/plain; charset=utf-8"
    view: str | None = None


@dataclass(frozen=True)
class DashboardRequest:
    view: str
    window: Window
    page: Page
    preset: str


class InvalidDashboardRequest(ValueError):
    pass


def _write_diagnostic(path: Path | None, error: DashboardDatabaseError) -> None:
    """Record only a safe error code; SQL, paths, and request data are excluded."""

    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(path, 0o600)
            os.write(descriptor, f"{int(time.time())} {error.code}\n".encode("ascii"))
        finally:
            os.close(descriptor)
    except OSError:
        # A diagnostic failure must never replace the safe HTTP response.
        return


def _database_error_response(
    error: DashboardDatabaseError,
    *,
    diagnostic_path: Path | None,
    retry_path: str,
) -> DashboardResponse:
    _write_diagnostic(diagnostic_path, error)
    if isinstance(error, LedgerNotFoundError):
        kind, status = "initial_sync", 503
    elif isinstance(error, LedgerBusyError):
        kind, status = "busy", 503
    else:
        kind, status = "unavailable", 503
    return DashboardResponse(
        status,
        pages.state_page(kind, retry_path=retry_path).encode(),
        {},
        "text/html; charset=utf-8",
    )


def _ledger_state(conn, now: float) -> tuple[bool, pages.Freshness]:
    row = conn.execute(
        """SELECT EXISTS(SELECT 1 FROM request WHERE is_synthetic=0) has_data,
                  (SELECT MAX(ts) FROM ingest_log) last_ingest"""
    ).fetchone()
    last_ingest = row["last_ingest"]
    age = max(0.0, now - last_ingest) if last_ingest is not None else None
    freshness = pages.Freshness(
        last_ingest=last_ingest,
        age_seconds=age,
        stale=age is None or age >= STALE_AFTER_SECONDS,
    )
    return bool(row["has_data"]), freshness


def _initial_sync_response() -> DashboardResponse:
    return DashboardResponse(
        503,
        pages.state_page("initial_sync").encode(),
        {},
        "text/html; charset=utf-8",
    )


def _bounded_html(body: str, *, view: str | None = None) -> DashboardResponse:
    encoded = body.encode()
    if len(encoded) >= MAX_HTML_RESPONSE_BYTES:
        return DashboardResponse(
            503,
            pages.state_page("response_too_large").encode(),
            {},
            "text/html; charset=utf-8",
        )
    return DashboardResponse(200, encoded, {}, "text/html; charset=utf-8", view)


def _preset_window(name: str, today: dt.date) -> tuple[dt.date, dt.date]:
    if name == "yesterday":
        day = today - dt.timedelta(days=1)
        return day, day
    if name == "today":
        return today, today
    if name == "7d":
        return today - dt.timedelta(days=6), today
    if name == "month":
        return today.replace(day=1), today
    if name == "last-month":
        end = today.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1), end
    raise InvalidDashboardRequest("unknown range preset")


def _matching_preset(start: dt.date, end: dt.date, today: dt.date) -> str:
    for name in ("yesterday", "today", "7d", "month", "last-month"):
        if (start, end) == _preset_window(name, today):
            return name
    return "custom"


def _parse_date(value: str, today: dt.date) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidDashboardRequest("invalid date") from exc
    if parsed.isoformat() != value or parsed > today:
        raise InvalidDashboardRequest("invalid date")
    return parsed


def _one(values: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    found = values.get(key)
    if found is None:
        return default
    if len(found) != 1:
        raise InvalidDashboardRequest("duplicate parameter")
    return found[0]


def _positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise InvalidDashboardRequest(f"invalid {name}") from exc
    if str(parsed) != value or parsed < 1:
        raise InvalidDashboardRequest(f"invalid {name}")
    return parsed


def canonical_query(request: DashboardRequest) -> str:
    values: list[tuple[str, str]] = [
        ("view", request.view),
        ("from", request.window.start.isoformat()),
        ("to", request.window.end.isoformat()),
    ]
    if request.window.project is not None:
        values.append(("project", request.window.project))
    if request.page.limit != 40:
        values.append(("limit", str(request.page.limit)))
    if request.page.page != 1:
        values.append(("page", str(request.page.page)))
    if request.page.sort != "cost":
        values.append(("sort", request.page.sort))
    if request.page.order != "desc":
        values.append(("order", request.page.order))
    return urlencode(values)


def resolve_query(raw_query: str, today: dt.date) -> tuple[DashboardRequest, str | None]:
    values = parse_qs(raw_query, keep_blank_values=True, strict_parsing=True)
    if set(values) - ALLOWED_KEYS:
        raise InvalidDashboardRequest("unknown parameter")
    view = _one(values, "view", "overview")
    if view not in ALLOWED_VIEWS:
        raise InvalidDashboardRequest("invalid view")
    range_name = _one(values, "range")
    date_from = _one(values, "from")
    date_to = _one(values, "to")
    if range_name is not None:
        if date_from is not None or date_to is not None:
            raise InvalidDashboardRequest("range and explicit dates cannot be combined")
        start, end = _preset_window(range_name, today)
    elif date_from is None and date_to is None:
        range_name = "yesterday"
        start, end = _preset_window(range_name, today)
    elif date_from is None or date_to is None:
        raise InvalidDashboardRequest("both dates are required")
    else:
        start = _parse_date(date_from, today)
        end = _parse_date(date_to, today)
    if start > end:
        raise InvalidDashboardRequest("window start is after end")

    project = _one(values, "project")
    if project == "":
        project = None
    if project is not None and (len(project) > 1024 or "\x00" in project):
        raise InvalidDashboardRequest("invalid project")
    limit = _positive_integer(_one(values, "limit", "40") or "", "limit")
    page_number = _positive_integer(_one(values, "page", "1") or "", "page")
    if page_number > 1_000_000:
        raise InvalidDashboardRequest("invalid page")
    sort = _one(values, "sort", "cost")
    order = _one(values, "order", "desc")
    if sort not in ALLOWED_SORTS or order not in ALLOWED_ORDERS:
        raise InvalidDashboardRequest("invalid sort")
    try:
        page = Page(limit=limit, page=page_number, sort=sort, order=order)
    except ValueError as exc:
        raise InvalidDashboardRequest(str(exc)) from exc
    window = Window(start, end, project, f"{start} — {end}")
    request = DashboardRequest(view, window, page, range_name or _matching_preset(start, end, today))
    canonical = canonical_query(request)
    return request, canonical if range_name is not None else None


def dashboard_response(
    raw_query: str,
    database_path: Path,
    today: dt.date,
    *,
    now: float | None = None,
    diagnostic_path: Path | None = None,
) -> DashboardResponse:
    try:
        request, redirect_query = resolve_query(raw_query, today)
    except (InvalidDashboardRequest, ValueError):
        return DashboardResponse(400, b"bad request", {})
    if redirect_query is not None:
        return DashboardResponse(303, b"", {"Location": f"/dashboard?{redirect_query}"})
    try:
        with closing(connect_dashboard(database_path)) as conn:
            has_data, freshness = _ledger_state(conn, time.time() if now is None else now)
            if not has_data:
                return _initial_sync_response()
            queries = {
                "overview": lambda: overview.query(conn, request.window, request.page),
                "period": lambda: period.query(conn, request.window, request.page),
                "trend": lambda: trend.query(conn, request.window),
                "cache": lambda: cache.query(conn, request.window),
                "dist": lambda: dist.query(conn, request.window),
            }
            model = queries[request.view]()
    except DashboardDatabaseError as error:
        return _database_error_response(
            error,
            diagnostic_path=diagnostic_path,
            retry_path="/dashboard",
        )
    return _bounded_html(
        pages.dashboard_page(request, model, today, freshness),
        view=request.view,
    )


def _detail_target(raw_target: str) -> tuple[str, str] | None:
    path, separator, query = raw_target.partition("?")
    if separator and query:
        return None
    for kind, prefix in (("prompt", "/prompts/"), ("session", "/sessions/")):
        if not path.startswith(prefix):
            continue
        raw_identifier = path[len(prefix) :]
        if not raw_identifier or "/" in raw_identifier or BAD_PERCENT_ESCAPE.search(raw_identifier):
            return None
        try:
            identifier = unquote(raw_identifier, errors="strict")
        except UnicodeError:
            return None
        if ID_PATTERN.fullmatch(identifier) is None:
            return None
        return kind, identifier
    return None


def detail_target_is_valid(raw_target: str) -> bool:
    """Validate a detail URL without opening the ledger or touching the filesystem."""

    return _detail_target(raw_target) is not None


def _resolve_identifier(conn, kind: str, identifier: str) -> str | None:
    if kind == "prompt":
        relation = "SELECT prompt_id identifier FROM prompt UNION SELECT prompt_id FROM request WHERE prompt_id IS NOT NULL"
    else:
        relation = "SELECT session_id identifier FROM session UNION SELECT session_id FROM request"
    rows = conn.execute(
        f"""SELECT identifier FROM ({relation})
            WHERE identifier=? OR substr(identifier,1,length(?))=?
            ORDER BY identifier LIMIT 3""",
        (identifier, identifier, identifier),
    ).fetchall()
    exact = [row["identifier"] for row in rows if row["identifier"] == identifier]
    if exact:
        return exact[0]
    return rows[0]["identifier"] if len(rows) == 1 else None


def detail_response(
    raw_target: str,
    database_path: Path,
    *,
    now: float | None = None,
    diagnostic_path: Path | None = None,
    csrf_token: str | None = None,
) -> DashboardResponse:
    target = _detail_target(raw_target)
    if target is None:
        return DashboardResponse(
            404,
            pages.state_page("not_found").encode(),
            {},
            "text/html; charset=utf-8",
        )
    kind, supplied_identifier = target
    retry_path = f"/{kind}s/{quote(supplied_identifier, safe='')}"
    try:
        with closing(connect_dashboard(database_path)) as conn:
            has_data, freshness = _ledger_state(conn, time.time() if now is None else now)
            if not has_data:
                return _initial_sync_response()
            identifier = _resolve_identifier(conn, kind, supplied_identifier)
            if identifier is None:
                return DashboardResponse(
                    404,
                    pages.state_page("not_found").encode(),
                    {},
                    "text/html; charset=utf-8",
                )
            if identifier != supplied_identifier:
                return DashboardResponse(
                    303,
                    b"",
                    {"Location": f"/{kind}s/{quote(identifier, safe='')}"},
                )
            model = prompt.query(conn, identifier) if kind == "prompt" else session.query(conn, identifier)
    except DashboardDatabaseError as error:
        return _database_error_response(
            error,
            diagnostic_path=diagnostic_path,
            retry_path=retry_path,
        )
    if model is None:
        return DashboardResponse(
            404,
            pages.state_page("not_found").encode(),
            {},
            "text/html; charset=utf-8",
        )
    body = (
        pages.prompt_page(model, freshness, csrf_token)
        if kind == "prompt"
        else pages.session_page(model, freshness, csrf_token)
    )
    return _bounded_html(body)
