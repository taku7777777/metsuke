from __future__ import annotations

import datetime as dt

from metsuke.viewmodel.common import Window


def data_max_date(conn) -> dt.date | None:
    row = conn.execute(
        "SELECT date(max(ts),'unixepoch','localtime') FROM v_request_cost"
    ).fetchone()
    return dt.date.fromisoformat(row[0]) if row and row[0] else None


def _date(value: str | dt.date | None, name: str) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return parsed


def _month_bounds(value: str, anchor: dt.date) -> tuple[dt.date, dt.date]:
    if value == "current":
        return anchor.replace(day=1), anchor
    if value == "last":
        end = anchor.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1), end
    try:
        start = dt.datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError) as exc:
        raise ValueError("month must be last or YYYY-MM") from exc
    if value != start.strftime("%Y-%m"):
        raise ValueError("month must be last or YYYY-MM")
    if start > anchor:
        raise ValueError("month must not be in the future")
    next_month = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start, next_month - dt.timedelta(days=1)


def resolve(
    conn,
    *,
    days=None,
    today=False,
    week=False,
    month=False,
    date_from=None,
    date_to=None,
    project=None,
    as_of=None,
) -> Window:
    anchor = _date(as_of, "as_of") or dt.date.today()
    explicit = date_from is not None or date_to is not None
    selected = sum((days is not None, bool(today), bool(week), bool(month), explicit))
    if selected > 1:
        raise ValueError("period options are mutually exclusive")
    if explicit:
        if date_from is None or date_to is None:
            raise ValueError("from and to must be specified together")
        start = _date(date_from, "from")
        end = _date(date_to, "to")
        assert start is not None and end is not None
    elif today:
        start = end = anchor
    elif week:
        if week in (True, "current"):
            start, end = anchor - dt.timedelta(days=anchor.weekday()), anchor
        elif week == "last":
            current_monday = anchor - dt.timedelta(days=anchor.weekday())
            start, end = (
                current_monday - dt.timedelta(days=7),
                current_monday - dt.timedelta(days=1),
            )
        else:
            raise ValueError("week must be last when a value is provided")
    elif month:
        month_value = "current" if month is True else month
        start, end = _month_bounds(month_value, anchor)
    else:
        count = 14 if days is None else days
        if count < 1:
            raise ValueError("days must be at least 1")
        start, end = anchor - dt.timedelta(days=count - 1), anchor
    if start > end:
        raise ValueError("from must not be after to")
    return Window(start, end, project, f"{start.isoformat()} — {end.isoformat()}")
