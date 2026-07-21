"""The only module permitted to emit markup for views. All escaping happens here.

Builders must never construct ``Html`` directly. They may only receive safe markup from
renderer primitives such as ``plain()``, ``code()``, ``table()``, and ``join()``.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from importlib import resources
from typing import Sequence

from metsuke.trace_html import CSP


class Html(str):
    """Marker type: a string that is already safe markup."""


CLASSES = frozenset({"left", "num", "dim", "warn", "clip", "project-clip", "barcell", "total"})
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\Z")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


@dataclass(frozen=True)
class Cell:
    text: str = ""
    cls: str = ""
    sort: str | float | None = None
    title: str | None = None
    bar: float | None = None
    content: Html | None = None
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
    cells: Sequence[Cell]
    highlight: bool = False


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _class(value: str) -> str:
    if value and value not in CLASSES:
        raise ValueError(f"invalid HTML class: {value}")
    return value


def _safe(value: Html) -> str:
    if not isinstance(value, Html):
        raise TypeError("markup body must be Html")
    return str(value)


def table(
    columns: Sequence[Column],
    rows: Sequence[Sequence[Cell] | Row],
    *,
    foot: Sequence[Cell] | None = None,
) -> Html:
    for column in columns:
        _class(column.cls)
        if column.sort_dir not in {"", "asc", "desc"}:
            raise ValueError("column sort direction must be asc or desc")
    if any(len(row.cells if isinstance(row, Row) else row) != len(columns) for row in rows) or (
        foot is not None and len(foot) != len(columns)
    ):
        raise ValueError("table rows must match columns")

    def attrs(cls: str, sort: str | float | None, title: str | None) -> str:
        out = f' class="{_esc(_class(cls))}"' if cls else ""
        if sort is not None:
            out += f' data-sort="{_esc(sort)}"'
        if title is not None:
            out += f' title="{_esc(title)}"'
        return out

    heads = []
    for column in columns:
        extra = ' data-sortable=""' if column.sortable else ""
        if column.sort_dir:
            extra += f' data-dir="{column.sort_dir}"'
        if column.sortable:
            direction = {"asc": "ascending", "desc": "descending"}.get(
                column.sort_dir, "none"
            )
            extra += f' tabindex="0" aria-sort="{direction}"'
        heads.append(
            f'<th scope="col"{attrs(column.cls, None, None)}{extra}>{_esc(column.label)}</th>'
        )
    body_rows = []
    for row_value in rows:
        row = row_value.cells if isinstance(row_value, Row) else row_value
        cells = []
        for cell in row:
            if cell.bar is not None and not 0 <= cell.bar <= 1:
                raise ValueError("bar must be between 0 and 1")
            content = _safe(cell.content) if cell.content is not None else _esc(cell.text)
            if cell.warn:
                content = '<span class="warn">⚠ </span>' + content
            if cell.dot is not None:
                if not _COLOR.fullmatch(cell.dot):
                    raise ValueError(f"invalid dot color: {cell.dot}")
                content = f'<i class="dot" style="background:{cell.dot}"></i>' + content
            if cell.clip:
                if cell.clip not in {"clip", "project-clip", "prompt-clip"}:
                    raise ValueError(f"invalid clip kind: {cell.clip}")
                style = ' style="max-width:260px"' if cell.clip == "prompt-clip" else ""
                cls = "clip" if cell.clip == "prompt-clip" else cell.clip
                content = f'<span class="{cls}"{style}>{content}</span>'
            if cell.bar is not None:
                content = (
                    '<span class="barcell"><span class="bar"><i style="width:'
                    f'{cell.bar * 100:.1f}%"></i></span><b>{content}</b></span>'
                )
            cells.append(f"<td{attrs(cell.cls, cell.sort, cell.title)}>{content}</td>")
        style = (
            ' style="background:#3a2025"'
            if isinstance(row_value, Row) and row_value.highlight
            else ""
        )
        body_rows.append(f"<tr{style}>" + "".join(cells) + "</tr>")
    footer = ""
    if foot is not None:
        footer = (
            "<tfoot><tr>"
            + "".join(
                f"<td{attrs(cell.cls, cell.sort, cell.title)}>{_esc(cell.text)}</td>"
                for cell in foot
            )
            + "</tr></tfoot>"
        )
    return Html(
        '<div class="card"><table><thead><tr>'
        + "".join(heads)
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody>"
        + footer
        + "</table></div>"
    )


def card(body: Html, *, title: str | None = None) -> Html:
    heading_html = f"<h3>{_esc(title)}</h3>" if title is not None else ""
    return Html(f'<div class="card">{heading_html}{_safe(body)}</div>')


def insight(text: str) -> Html:
    return Html(f'<div class="insight">{_esc(text).replace(chr(10), "<br>")}</div>')


def insight_body(body: Html) -> Html:
    return Html(f'<div class="insight">{_safe(body)}</div>')


def legend(items: Sequence[tuple[str, str]]) -> Html:
    parts = []
    for label, color in items:
        if not _COLOR.fullmatch(color):
            raise ValueError(f"invalid color: {color}")
        parts.append(f'<span><i style="background:{color}"></i>{_esc(label)}</span>')
    return Html('<div class="legend">' + "".join(parts) + "</div>")


def tabs(group: str, items: Sequence[tuple[str, str, bool]]) -> Html:
    if not _ID.fullmatch(group):
        raise ValueError("invalid tab group")
    parts = []
    for panel_id, label, active in items:
        if not _ID.fullmatch(panel_id):
            raise ValueError("invalid panel id")
        cls = ' class="on"' if active else ""
        selected = "true" if active else "false"
        parts.append(
            f'<button type="button" role="tab" data-tab="{panel_id}" aria-controls="{panel_id}" '
            f'aria-selected="{selected}"{cls}>{_esc(label)}</button>'
        )
    return Html(
        f'<div class="tabs" role="tablist" data-group="{group}">' + "".join(parts) + "</div>"
    )


def panel(group: str, panel_id: str, body: Html, *, active: bool) -> Html:
    if not _ID.fullmatch(group) or not _ID.fullmatch(panel_id):
        raise ValueError("invalid panel identity")
    cls = "panel on" if active else "panel"
    hidden = "" if active else " hidden"
    return Html(
        f'<div class="{cls}" role="tabpanel" data-group="{group}" id="{panel_id}"{hidden}>'
        f"{_safe(body)}</div>"
    )


def grain_tabs(items: Sequence[tuple[str, str, bool, str | None]]) -> Html:
    parts = []
    for grain, label, active, title in items:
        if grain not in {"daily", "weekly", "monthly"}:
            raise ValueError("invalid grain")
        cls = ' class="on"' if active else ""
        selected = "true" if active else "false"
        title_attr = f' title="{_esc(title)}"' if title else ""
        parts.append(
            f'<button type="button" role="tab" data-grain="{grain}" '
            f'aria-selected="{selected}"{cls}{title_attr}>{_esc(label)}</button>'
        )
    return Html(
        '<div class="tabs" role="tablist" data-group="v2-grain">' + "".join(parts) + "</div>"
    )


def grain_panel(grain: str, body: Html, *, active: bool) -> Html:
    if grain not in {"daily", "weekly", "monthly"}:
        raise ValueError("invalid grain")
    display = "block" if active else "none"
    hidden = "" if active else " hidden"
    return Html(
        f'<div class="grain-panel" role="tabpanel" data-grain-panel="{grain}" '
        f'style="display:{display}"{hidden}>{_safe(body)}</div>'
    )


def heading(level: int, text: str) -> Html:
    if level not in (2, 3):
        raise ValueError("heading level must be 2 or 3")
    return Html(f"<h{level}>{_esc(text)}</h{level}>")


def code(text: str) -> Html:
    return Html(f"<code>{_esc(text)}</code>")


def clip(text: str, *, max_width: int, title: str | None = None) -> Html:
    if type(max_width) is not int or not 1 <= max_width <= 1000:
        raise ValueError("clip max_width must be an integer from 1 to 1000")
    title_attr = f' title="{_esc(title)}"' if title is not None else ""
    return Html(
        f'<span class="clip" style="max-width:{max_width}px"{title_attr}>{_esc(text)}</span>'
    )


def plain(text: str) -> Html:
    return Html(_esc(text))


def warning(text: str) -> Html:
    return Html(f'<span class="warn">{_esc(text)}</span>')


def code_lines(items: Sequence[str]) -> Html:
    return Html("<br>".join(str(code(item)) for item in items))


def join(*parts: Html) -> Html:
    return Html("".join(_safe(part) for part in parts))


def text_block(text: str, *, cls: str = "") -> Html:
    _class(cls)
    attr = f' class="{cls}"' if cls else ""
    return Html(f"<div{attr}>{_esc(text)}</div>")


def block(body: Html, *, cls: str = "") -> Html:
    _class(cls)
    attr = f' class="{cls}"' if cls else ""
    return Html(f"<div{attr}>{_safe(body)}</div>")


def money(value: float) -> str:
    return f"${value:,.2f}"


def _nice_top(value: float) -> float:
    if value <= 0:
        return 1
    raw = value / 4
    power = 10 ** math.floor(math.log10(raw))
    step = next(item * power for item in (1, 2, 5, 10) if item * power >= raw)
    return step * 4


def stacked_bars(
    days,
    series: dict[str, list[float]],
    colors: dict[str, str],
    *,
    height=280,
    width=1150,
    money_values=True,
) -> Html:
    if any(not _COLOR.fullmatch(color) for color in colors.values()):
        raise ValueError("invalid series color")
    ml, mr, mt, mb = 58, 16, 18, 38
    pw, ph = width - ml - mr, height - mt - mb
    totals = [sum(series[key][i] for key in series) for i in range(len(days))]
    top = _nice_top(max(totals, default=0))
    out = []
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        label = money(value) if money_values else f"{value:g}"
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" stroke="#2d3648"/><text x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end" fill="#7d8899">{_esc(label)}</text>'
        )
    bar_width = max(3, pw / max(1, len(days)) * 0.68)
    for index, day in enumerate(days):
        x = ml + (index + 0.5) * pw / len(days)
        base = mt + ph
        if day.weekday() == 0:
            out.append(
                f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#394254"/>'
            )
        for key, values in series.items():
            value = values[index]
            segment_height = value / top * ph
            base -= segment_height
            value_label = money(value) if money_values else f"{value:g}件"
            out.append(
                f'<rect x="{x - bar_width / 2:.1f}" y="{base:.1f}" width="{bar_width:.1f}" height="{max(0, segment_height):.2f}" fill="{colors[key]}"><title>{_esc(day.strftime("%m-%d"))} {_esc(key)} {_esc(value_label)}</title></rect>'
            )
        if totals[index] > 0.8 * max(totals, default=1):
            total_label = money(totals[index]) if money_values else f"{totals[index]:g}"
            out.append(
                f'<text x="{x:.1f}" y="{base - 4:.1f}" text-anchor="middle" fill="#d6dde8">{_esc(total_label)}</text>'
            )
        if index % 2 == 0:
            color = "#7d8899" if day.weekday() > 4 else "#aab4c3"
            out.append(
                f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" fill="{color}">{_esc(day.strftime("%m-%d"))}</text>'
            )
    title = "期間別コストの積み上げ棒グラフ" if money_values else "期間別件数の積み上げ棒グラフ"
    return Html(
        f'<svg viewBox="0 0 {width} {height}" role="img"><title>{title}</title>'
        + "".join(out)
        + "</svg>"
    )


def cache_balance(days, read, write_5m, write_1h, *, width=1150, height=240) -> Html:
    ml, mr, mt, mb = 58, 50, 18, 35
    pw, ph = width - ml - mr, height - mt - mb
    top = _nice_top(max([*read, *[write_5m[i] + write_1h[i] for i in range(len(days))]], default=0))
    bar_width = pw / max(1, len(days)) * 0.25
    out = []
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        out.append(
            f'<line x1="{ml}" y1="{y}" x2="{width - mr}" y2="{y}" stroke="#2d3648"/><text x="{ml - 6}" y="{y + 4}" text-anchor="end" fill="#7d8899">${value:.2f}</text>'
        )
    points = []
    colors = {"read": "#2dd4bf", "write": "#facc15", "ratio": "#fb923c"}
    for index, day in enumerate(days):
        x = ml + (index + 0.5) * pw / len(days)
        write = write_5m[index] + write_1h[index]
        ratio = write_1h[index] / write * 100 if write else 0
        for offset, value, color, name in (
            (-bar_width / 2, read[index], colors["read"], "read"),
            (bar_width / 2, write, colors["write"], "write"),
        ):
            bar_height = value / top * ph
            out.append(
                f'<rect x="{x + offset - bar_width / 2:.1f}" y="{mt + ph - bar_height:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}"><title>{_esc(day.strftime("%m-%d"))} {name} {_esc(money(value))}</title></rect>'
            )
        points.append(f"{x:.1f},{mt + ph - ratio / 100 * ph:.1f}")
        out.append(
            f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" fill="#7d8899">{_esc(day.strftime("%m-%d"))}</text>'
        )
    out.append(
        f'<polyline points="{" ".join(points)}" fill="none" stroke="{colors["ratio"]}" stroke-width="2"/>'
    )
    return Html(
        f'<svg viewBox="0 0 {width} {height}" role="img">'
        "<title>キャッシュ読み書き費用と1時間キャッシュ比率</title>"
        + "".join(out)
        + "</svg>"
    )


def _axis_label(day, grain):
    return day.strftime("%Y-%m" if grain == "monthly" else "%m-%d")


def volume_chart(labels, data, colors, moving, grain, lo_ts, hi_ts, markers, regimes) -> Html:
    if any(not _COLOR.fullmatch(color) for color in colors.values()):
        raise ValueError("invalid series color")
    width, height = 1150, 280
    mr, mt, mb = 16, 18, 38
    totals = [sum(data[key][i] for key in data) for i in range(len(labels))]
    top = _nice_top(max([*totals, *(moving or [])], default=0))
    ml = max(58, len(money(top)) * 7 + 14)
    pw, ph = width - ml - mr, height - mt - mb
    out = []

    def xpos(ts):
        return ml + (ts - lo_ts) / (hi_ts - lo_ts) * pw

    for marker in markers:
        x1 = max(ml, xpos(marker["ts_start"]))
        x2 = min(width - mr, xpos(marker["ts_end"] or hi_ts))
        out.append(
            f'<rect x="{x1:.1f}" y="{mt}" width="{max(1, x2 - x1):.1f}" height="{ph}" fill="#7aa2f7" opacity=".10"><title>{_esc(marker["category"] or "marker")} · {_esc(marker["verdict"] or "pending")}</title></rect>'
        )
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" stroke="#2d3648"/><text x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end" fill="#7d8899">{money(value)}</text>'
        )
    bar_width = max(8, pw / max(1, len(labels)) * 0.68)
    for index, label in enumerate(labels):
        x = ml + (index + 0.5) * pw / len(labels)
        base = mt + ph
        for key, values in data.items():
            segment = values[index] / top * ph
            base -= segment
            out.append(
                f'<rect x="{x - bar_width / 2:.1f}" y="{base:.1f}" width="{bar_width:.1f}" height="{max(0, segment):.2f}" fill="{colors[key]}"><title>{_esc(_axis_label(label, grain))} {_esc(key)} {money(values[index])}</title></rect>'
            )
        if grain != "daily" or index % 2 == 0:
            out.append(
                f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" fill="#7d8899">{_esc(_axis_label(label, grain))}</text>'
            )
    if moving:
        points = " ".join(
            f"{ml + (i + 0.5) * pw / len(labels):.1f},{mt + ph - value / top * ph:.1f}"
            for i, value in enumerate(moving)
        )
        out.append(
            f'<polyline points="{points}" fill="none" stroke="#fff" stroke-width="2"><title>7日移動平均</title></polyline>'
        )
    for regime in regimes:
        x = xpos(regime["ts"])
        out.append(
            f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#f87171" stroke-dasharray="4 3"><title>{_esc(regime["kind"])}</title></line>'
        )
    return Html(
        f'<svg viewBox="0 0 {width} {height}" role="img">'
        "<title>期間別コストと施策・外生イベント</title>"
        + "".join(out)
        + "</svg>"
    )


def line_chart(
    labels, series, colors, unit, *, money_axis=False, grain="weekly", fixed_top=None, precision=0
) -> Html:
    if any(not _COLOR.fullmatch(color) for color in colors.values()):
        raise ValueError("invalid series color")
    width, height = 1150, 230
    mr, mt, mb = 16, 18, 38
    valid = [value for values in series.values() for value in values if value is not None]
    top = fixed_top or _nice_top(max(valid, default=0))

    def fmt(value):
        return money(value) if money_axis else f"{value:,.{precision}f}{unit}"

    ml = max(58, len(fmt(top)) * 7 + 14)
    pw, ph = width - ml - mr, height - mt - mb
    out = []
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" stroke="#2d3648"/><text x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end" fill="#7d8899">{_esc(fmt(value))}</text>'
        )
    for name, values in series.items():
        segments, segment = [], []
        for index, value in enumerate(values):
            if value is None:
                if segment:
                    segments.append(segment)
                    segment = []
                continue
            x = ml + (index + 0.5) * pw / len(labels)
            y = mt + ph - value / top * ph
            segment.append(f"{x:.1f},{y:.1f}")
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colors[name]}"><title>{_esc(_axis_label(labels[index], grain))} {_esc(name)} {_esc(fmt(value))}</title></circle>'
            )
        if segment:
            segments.append(segment)
        for points in segments:
            if len(points) > 1:
                out.append(
                    f'<polyline points="{" ".join(points)}" fill="none" stroke="{colors[name]}" stroke-width="2"/>'
                )
    for index, label in enumerate(labels):
        x = ml + (index + 0.5) * pw / len(labels)
        out.append(
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" fill="#7d8899">{_esc(_axis_label(label, grain))}</text>'
        )
    series_label = "、".join(_esc(name) for name in series)
    return Html(
        f'<svg viewBox="0 0 {width} {height}" role="img"><title>{series_label} の推移</title>'
        + "".join(out)
        + "</svg>"
    )


def shell(*, title: str, period: str, total: Html | str, body: Html, stamp: str) -> str:
    if isinstance(total, Html):
        safe_total = str(total)
    elif isinstance(total, str):
        safe_total = _esc(total)
    else:
        raise TypeError("total must be Html or str")
    template = resources.files("metsuke").joinpath("view_template.html").read_text()
    values = {
        "__VIEW_TITLE__": _esc(title),
        "__VIEW_H1__": _esc(title),
        "__VIEW_PERIOD__": _esc(period),
        "__VIEW_TOTAL__": safe_total,
        "__VIEW_BODY__": _safe(body),
        "__VIEW_STAMP__": _esc(stamp),
    }
    for marker, value in values.items():
        expected = 1
        if template.count(marker) != expected:
            raise ValueError(f"view template must contain {marker} exactly once")
        template = template.replace(marker, value)
    csp_slot = 'content="__VIEW_CSP__"'
    if template.count(csp_slot) != 1:
        raise ValueError("view template must contain exactly one CSP slot")
    template = template.replace(csp_slot, f'content="{CSP}"')
    return template
