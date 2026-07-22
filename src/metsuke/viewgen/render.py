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


def validate_color(value: str) -> str:
    """Return a renderer-safe SVG color or reject it."""

    if not _COLOR.fullmatch(value):
        raise ValueError(f"invalid color: {value}")
    return value


_CLIP_LADDER = "".join(f".cw{step}{{max-width:{step * 20}px}}" for step in range(1, 31))

CHART_CSS = (
    """
/* --- shared chart + cell presentation (single source for both renderers) ---
   Colours resolve through custom properties so the same markup reads correctly
   in light and dark. Each host defines the properties for its themes; the
   fallbacks below are the dark values. */
svg.chart{display:block;width:100%;height:auto}
svg.chart text{font-size:11px}
.ch-grid{stroke:var(--ch-grid,#2d3648)}
.ch-divider{stroke:var(--ch-divider,#394254)}
.ch-axis{fill:var(--ch-axis,#7d8899)}
.ch-weekday{fill:var(--ch-weekday,#aab4c3)}
.ch-value{fill:var(--ch-value,#d6dde8)}
.ch-avg{stroke:var(--ch-avg,#ffffff)}
.ch-regime{stroke:var(--ch-regime,#f87171)}
.ch-marker{fill:var(--ch-marker,#7aa2f7);opacity:.10}
/* Series keep their data-driven fill; the edge stroke guarantees the shape stays
   legible against a light surface, where saturated fills alone can wash out. */
.ch-series{stroke:var(--ch-series-edge,transparent);stroke-width:.5}
.ch-line{stroke-linejoin:round;stroke-linecap:round}
/* Daily context bars: a fixed ~month of days with the selected range highlighted.
   Selection is conveyed three independent ways so it never relies on colour alone:
   a translucent band behind the range, dashed boundary edges, and brighter bars. */
.ch-day{fill:var(--ch-day,#3d4657)}
.ch-day-sel{fill:var(--ch-day-sel,#7aa2f7)}
.ch-selband{fill:var(--ch-sel-band,#7aa2f7);opacity:.14}
.ch-seledge{stroke:var(--ch-sel-edge,#7aa2f7);stroke-width:1.5;stroke-dasharray:4 3}
.dot{display:inline-block;width:8px;height:8px;margin-right:5px;vertical-align:middle}
.barcell{display:inline-flex;width:132px;align-items:center;justify-content:space-between;vertical-align:middle}
.barcell b{display:inline-block;width:65px;text-align:right}
svg.bar{display:inline-block;width:60px;height:7px;vertical-align:middle}
.bar-track{fill:var(--ch-bar-track,#1c2330)}
.bar-fill{fill:var(--ch-bar-fill,#7aa2f7)}
tr.hl{background:var(--ch-highlight,#3a2025)}
.grain-panel{display:none}
.grain-panel.on{display:block}
.clip,.project-clip,.prompt-clip{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.project-clip{max-width:220px}
.prompt-clip{max-width:260px}
"""
    + _CLIP_LADDER
)


def swatch(color: str) -> str:
    """A colour chip as inline SVG.

    Presentation attributes are CSP-safe; an inline ``style=`` attribute is not.
    """

    safe = validate_color(color)
    return (
        '<svg class="dot" viewBox="0 0 10 10" aria-hidden="true">'
        f'<circle cx="5" cy="5" r="5" fill="{safe}"/></svg>'
    )


def bar_svg(ratio: float) -> str:
    """The in-table magnitude bar, drawn as SVG so no inline width style is needed."""

    filled = min(100.0, max(0.0, ratio * 100))
    return (
        '<span class="barcell"><svg class="bar" viewBox="0 0 100 7" preserveAspectRatio="none" '
        f'role="img" aria-label="{filled:.1f}%">'
        '<rect class="bar-track" width="100" height="7"/>'
        f'<rect class="bar-fill" width="{filled:.2f}" height="7"/></svg>'
    )


def _clip_width_class(max_width: int) -> str:
    """Quantise a pixel width onto a fixed CSS class ladder (20px steps)."""

    step = min(30, max(1, math.ceil(max_width / 20)))
    return f"cw{step}"


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
                content = swatch(cell.dot) + content
            if cell.clip:
                if cell.clip not in {"clip", "project-clip", "prompt-clip"}:
                    raise ValueError(f"invalid clip kind: {cell.clip}")
                content = f'<span class="{cell.clip}">{content}</span>'
            if cell.bar is not None:
                content = f"{bar_svg(cell.bar)}<b>{content}</b></span>"
            cells.append(f"<td{attrs(cell.cls, cell.sort, cell.title)}>{content}</td>")
        highlight = ' class="hl"' if isinstance(row_value, Row) and row_value.highlight else ""
        body_rows.append(f"<tr{highlight}>" + "".join(cells) + "</tr>")
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
        parts.append(f"<span>{swatch(color)}{_esc(label)}</span>")
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
    cls = "grain-panel on" if active else "grain-panel"
    hidden = "" if active else " hidden"
    return Html(
        f'<div class="{cls}" role="tabpanel" data-grain-panel="{grain}"{hidden}>'
        f"{_safe(body)}</div>"
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
        f'<span class="clip {_clip_width_class(max_width)}"{title_attr}>{_esc(text)}</span>'
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
            f'<line class="ch-grid" x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}"/><text class="ch-axis" x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end">{_esc(label)}</text>'
        )
    bar_width = max(3, pw / max(1, len(days)) * 0.68)
    for index, day in enumerate(days):
        x = ml + (index + 0.5) * pw / len(days)
        base = mt + ph
        if day.weekday() == 0:
            out.append(
                f'<line class="ch-divider" x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}"/>'
            )
        for key, values in series.items():
            value = values[index]
            segment_height = value / top * ph
            base -= segment_height
            value_label = money(value) if money_values else f"{value:g}件"
            out.append(
                f'<rect class="ch-series" x="{x - bar_width / 2:.1f}" y="{base:.1f}" width="{bar_width:.1f}" height="{max(0, segment_height):.2f}" fill="{colors[key]}" data-series="{_esc(key)}" data-label="{_esc(day.strftime("%m-%d"))}" data-value="{_esc(value_label)}"><title>{_esc(day.strftime("%m-%d"))} {_esc(key)} {_esc(value_label)}</title></rect>'
            )
        if totals[index] > 0.8 * max(totals, default=1):
            total_label = money(totals[index]) if money_values else f"{totals[index]:g}"
            out.append(
                f'<text class="ch-value" x="{x:.1f}" y="{base - 4:.1f}" text-anchor="middle">{_esc(total_label)}</text>'
            )
        if index % 2 == 0:
            cls = "ch-axis" if day.weekday() > 4 else "ch-axis ch-weekday"
            out.append(
                f'<text class="{cls}" x="{x:.1f}" y="{height - 12}" text-anchor="middle">{_esc(day.strftime("%m-%d"))}</text>'
            )
    title = "期間別コストの積み上げ棒グラフ" if money_values else "期間別件数の積み上げ棒グラフ"
    return Html(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img"><title>{title}</title>'
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
            f'<line class="ch-grid" x1="{ml}" y1="{y}" x2="{width - mr}" y2="{y}"/><text class="ch-axis" x="{ml - 6}" y="{y + 4}" text-anchor="end">${value:.2f}</text>'
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
                f'<rect class="ch-series" x="{x + offset - bar_width / 2:.1f}" y="{mt + ph - bar_height:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}" data-series="{_esc(name)}" data-label="{_esc(day.strftime("%m-%d"))}" data-value="{_esc(money(value))}"><title>{_esc(day.strftime("%m-%d"))} {name} {_esc(money(value))}</title></rect>'
            )
        points.append(f"{x:.1f},{mt + ph - ratio / 100 * ph:.1f}")
        out.append(
            f'<text class="ch-axis" x="{x:.1f}" y="{height - 10}" text-anchor="middle">{_esc(day.strftime("%m-%d"))}</text>'
        )
    out.append(
        f'<polyline class="ch-line" points="{" ".join(points)}" fill="none" stroke="{colors["ratio"]}" stroke-width="2"/>'
    )
    return Html(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">'
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
            f'<rect class="ch-marker" x="{x1:.1f}" y="{mt}" width="{max(1, x2 - x1):.1f}" height="{ph}"><title>{_esc(marker["category"] or "marker")} · {_esc(marker["verdict"] or "pending")}</title></rect>'
        )
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        out.append(
            f'<line class="ch-grid" x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}"/><text class="ch-axis" x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end">{money(value)}</text>'
        )
    bar_width = max(8, pw / max(1, len(labels)) * 0.68)
    for index, label in enumerate(labels):
        x = ml + (index + 0.5) * pw / len(labels)
        base = mt + ph
        for key, values in data.items():
            segment = values[index] / top * ph
            base -= segment
            out.append(
                f'<rect class="ch-series" x="{x - bar_width / 2:.1f}" y="{base:.1f}" width="{bar_width:.1f}" height="{max(0, segment):.2f}" fill="{colors[key]}" data-series="{_esc(key)}" data-label="{_esc(_axis_label(label, grain))}" data-value="{_esc(money(values[index]))}"><title>{_esc(_axis_label(label, grain))} {_esc(key)} {money(values[index])}</title></rect>'
            )
        if grain != "daily" or index % 2 == 0:
            out.append(
                f'<text class="ch-axis" x="{x:.1f}" y="{height - 12}" text-anchor="middle">{_esc(_axis_label(label, grain))}</text>'
            )
    if moving:
        points = " ".join(
            f"{ml + (i + 0.5) * pw / len(labels):.1f},{mt + ph - value / top * ph:.1f}"
            for i, value in enumerate(moving)
        )
        out.append(
            f'<polyline class="ch-avg" points="{points}" fill="none" stroke-width="2"><title>7日移動平均</title></polyline>'
        )
    for regime in regimes:
        x = xpos(regime["ts"])
        out.append(
            f'<line class="ch-regime" x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke-dasharray="4 3"><title>{_esc(regime["kind"])}</title></line>'
        )
    return Html(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">'
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
            f'<line class="ch-grid" x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}"/><text class="ch-axis" x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end">{_esc(fmt(value))}</text>'
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
                f'<circle class="ch-series" cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colors[name]}" data-series="{_esc(name)}" data-label="{_esc(_axis_label(labels[index], grain))}" data-value="{_esc(fmt(value))}"><title>{_esc(_axis_label(labels[index], grain))} {_esc(name)} {_esc(fmt(value))}</title></circle>'
            )
        if segment:
            segments.append(segment)
        for points in segments:
            if len(points) > 1:
                out.append(
                    f'<polyline class="ch-line" points="{" ".join(points)}" fill="none" stroke="{colors[name]}" stroke-width="2"/>'
                )
    for index, label in enumerate(labels):
        x = ml + (index + 0.5) * pw / len(labels)
        out.append(
            f'<text class="ch-axis" x="{x:.1f}" y="{height - 12}" text-anchor="middle">{_esc(_axis_label(label, grain))}</text>'
        )
    series_label = "、".join(_esc(name) for name in series)
    return Html(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img"><title>{series_label} の推移</title>'
        + "".join(out)
        + "</svg>"
    )


def daily_context(days, values, selected, *, width=1150, height=230) -> Html:
    """A fixed-window daily cost bar chart with the selected range highlighted.

    ``days``/``values``/``selected`` are parallel lists (one entry per day; a value
    of ``None`` is an unknown-price day). Colours come entirely from CSS classes so
    the chart reads correctly in light and dark, and the selection is marked with a
    band + dashed boundary edges + brighter bars — never colour alone.
    """

    if not (len(days) == len(values) == len(selected)):
        raise ValueError("daily_context lists must be the same length")
    mr, mt, mb = 16, 18, 38
    valid = [value for value in values if value is not None]
    top = _nice_top(max(valid, default=0))
    ml = max(58, len(money(top)) * 7 + 14)
    pw, ph = width - ml - mr, height - mt - mb
    count = max(1, len(days))
    out = []
    chosen = [index for index, flag in enumerate(selected) if flag]
    if chosen:
        lo, hi = chosen[0], chosen[-1]
        x1 = ml + lo * pw / count
        x2 = ml + (hi + 1) * pw / count
        out.append(
            f'<rect class="ch-selband" x="{x1:.1f}" y="{mt}" width="{max(0, x2 - x1):.1f}" '
            f'height="{ph}"><title>選択範囲 {_esc(days[lo].strftime("%m-%d"))}〜'
            f'{_esc(days[hi].strftime("%m-%d"))}</title></rect>'
        )
        out.append(
            f'<line class="ch-seledge" x1="{x1:.1f}" y1="{mt}" x2="{x1:.1f}" y2="{mt + ph}"/>'
        )
        out.append(
            f'<line class="ch-seledge" x1="{x2:.1f}" y1="{mt}" x2="{x2:.1f}" y2="{mt + ph}"/>'
        )
    for index in range(5):
        value = top * index / 4
        y = mt + ph - value / top * ph
        out.append(
            f'<line class="ch-grid" x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}"/>'
            f'<text class="ch-axis" x="{ml - 7}" y="{y + 4:.1f}" text-anchor="end">{money(value)}</text>'
        )
    bar_width = max(3, pw / count * 0.68)
    for index, day in enumerate(days):
        x = ml + (index + 0.5) * pw / count
        value = values[index]
        height_px = (value or 0.0) / top * ph
        cls = "ch-day-sel" if selected[index] else "ch-day"
        label = money(value) if value is not None else "—"
        out.append(
            f'<rect class="{cls}" x="{x - bar_width / 2:.1f}" y="{mt + ph - height_px:.1f}" '
            f'width="{bar_width:.1f}" height="{max(0, height_px):.2f}" '
            f'data-series="cost" data-label="{_esc(day.strftime("%m-%d"))}" data-value="{_esc(label)}">'
            f'<title>{_esc(day.strftime("%m-%d"))} {_esc(label)}</title></rect>'
        )
        if index % 3 == 0 or index == len(days) - 1:
            out.append(
                f'<text class="ch-axis" x="{x:.1f}" y="{height - 12}" '
                f'text-anchor="middle">{_esc(day.strftime("%m-%d"))}</text>'
            )
    return Html(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">'
        "<title>API換算コスト の推移</title>" + "".join(out) + "</svg>"
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
        "__VIEW_CHART_CSS__": CHART_CSS,
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
