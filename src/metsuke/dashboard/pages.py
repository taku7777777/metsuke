"""The dashboard's only HTML serializer; all untrusted values are escaped here."""

from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from ..viewmodel import overview, prompt, session
from ..viewmodel.common import Cell, LegacyViewModel, Money, Node, Page, Row, thaw

DETAIL_COMMAND = re.compile(r"metsuke (explain|trace) ([A-Za-z0-9][A-Za-z0-9_-]{7,127}) --html\Z")


@dataclass(frozen=True)
class Freshness:
    last_ingest: float | None
    age_seconds: float | None
    stale: bool


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _url(path: str, values: list[tuple[str, str]]) -> str:
    return _esc(f"{path}?{urlencode(values)}")


def _base_values(request, *, view: str | None = None) -> list[tuple[str, str]]:
    values = [
        ("view", view or request.view),
        ("from", request.window.start.isoformat()),
        ("to", request.window.end.isoformat()),
    ]
    if request.window.project is not None:
        values.append(("project", request.window.project))
    if request.page.limit != 40:
        values.append(("limit", str(request.page.limit)))
    if request.page.order != "desc":
        values.append(("order", request.page.order))
    return values


def _controls(request, today: dt.date) -> str:
    presets = (("昨日", "yesterday"), ("今日", "today"), ("直近7日", "7d"), ("今月", "month"), ("先月", "last-month"))
    preset_values = [("view", request.view)]
    if request.window.project is not None:
        preset_values.append(("project", request.window.project))
    if request.page.limit != 40:
        preset_values.append(("limit", str(request.page.limit)))
    if request.page.order != "desc":
        preset_values.append(("order", request.page.order))
    preset_links = " ".join(
        f'<a class="preset" href="{_url("/dashboard", [*preset_values, ("range", value)])}"'
        f'{" aria-current=\"true\"" if request.preset == value else ""}>{_esc(label)}</a>'
        for label, value in presets
    )
    tabs = " ".join(
        f'<a class="tab" href="{_url("/dashboard", _base_values(request, view=value))}"'
        f'{" aria-current=\"page\"" if request.view == value else ""}>{_esc(label)}</a>'
        for value, label in (
            ("overview", "概要"),
            ("period", "期間"),
            ("trend", "推移"),
            ("cache", "キャッシュ"),
            ("dist", "分布"),
        )
    )
    project = _esc(request.window.project or "")
    order = (
        f'<input type="hidden" name="order" value="{_esc(request.page.order)}">'
        if request.page.order != "desc"
        else ""
    )
    return (
        '<div class="controls">'
        f'<nav class="tabs" aria-label="表示">{tabs}</nav>'
        '<div class="filters">'
        f'<nav class="presets" aria-label="期間プリセット">{preset_links}</nav>'
        '<form method="get" action="/dashboard">'
        f'<input type="hidden" name="view" value="{_esc(request.view)}">'
        f'{order}'
        f'<label class="field">開始 <input type="date" name="from" required max="{request.window.end}" value="{request.window.start}"></label>'
        f'<label class="field">終了 <input type="date" name="to" required min="{request.window.start}" max="{today}" value="{request.window.end}"></label>'
        f'<label class="field">project <input name="project" maxlength="1024" value="{project}"></label>'
        f'<label class="field">件数 <input type="number" name="limit" min="1" max="200" value="{request.page.limit}"></label>'
        '<button type="submit">表示</button></form>'
        '</div></div>'
    )


def _table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> str:
    head = "".join(f'<th scope="col">{_esc(value)}</th>' for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _overview(model: overview.OverviewModel) -> str:
    kpis = "".join(
        f'<article class="kpi-card"><h3>{_esc(item.name)}</h3>'
        f'<p class="kpi-value">{_esc(item.display)}</p>'
        f'<p class="kpi-delta">前期比 {_esc(item.comparison.display)}</p></article>'
        for item in model.kpis
    )
    parts = _table(
        ("費目", "金額"),
        [(item.name, item.amount.display) for item in model.cost_parts],
    )
    prompts = []
    for item in model.top_prompts:
        link = f'<a href="/prompts/{quote(item.prompt_id, safe="")}">{_esc(item.text or "—")}</a>'
        prompts.append((item.amount.display, _esc(item.project or "—"), link, item.request_count))
    prompt_table = _trusted_table(("金額", "project", "prompt", "request"), prompts)
    sessions = []
    for item in model.top_sessions:
        link = f'<a href="/sessions/{quote(item.session_id, safe="")}">{_esc(item.session_id[:8])}</a>'
        sessions.append((item.amount.display, _esc(item.project or "—"), link, item.prompt_count, item.request_count))
    session_table = _trusted_table(("金額", "project", "session", "prompt", "request"), sessions)
    cache = _table(
        ("原因", "request", "金額"),
        [(item.cause, item.request_count, item.amount.display) for item in model.cache_rebuilds],
    )
    unknown = (
        f'<aside>未知価格 request: {_esc(model.unknown_cost_request_count)}。集計は不完全です。</aside>'
        if model.unknown_cost_request_count
        else ""
    )
    return (
        f'<section><h2>KPI</h2><div class="kpi-grid">{kpis}</div>{unknown}</section>'
        f'<section><h2>費目構成</h2>{parts}</section>'
        f'<section><h2>高額prompt</h2>{prompt_table}</section>'
        f'<section><h2>高額session</h2>{session_table}</section>'
        f'<section><h2>cache再作成</h2>{cache}</section>'
        '<section><h2>次の確認</h2><p>高額項目から詳細を確認してください。</p></section>'
    )


def _trusted_table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> str:
    """Render rows whose elements have already been escaped or created locally."""
    head = "".join(f'<th scope="col">{_esc(value)}</th>' for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _number(value: object, *, money: bool = False, suffix: str = "") -> str:
    if value is None:
        return "—"
    if money:
        return Money.from_raw(float(value)).display
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value}{suffix}"


def _series_table(
    labels,
    series,
    *,
    money: bool = False,
    suffix: str = "",
) -> str:
    values = thaw(series)
    names = tuple(values)
    rows = [
        tuple([label, *[_number(values[name][index], money=money, suffix=suffix) for name in names]])
        for index, label in enumerate(labels)
    ]
    return _table(("期間", *names), rows)


INTERACTIVE_SUFFIX = "（対話のみ）"
SAME_VALUES_NOTE = "（対話のみも同値）"
_QUANTILE_LINE = re.compile(r"\A(?P<label>.*?)\s*(?P<values>p\d+\s.*)\Z")
_QUANTILE_PAIR = re.compile(r"\A(?P<term>p\d+)\s+(?P<value>\S+)\Z")
# A label is only split off when the prefix cannot be part of a value itself;
# "（原因別: …）" and money amounts must never be mistaken for a label boundary.
_LABEL_STOP = ("（", "$", "/", "・", "、")
_FACT_SEPARATORS = (" · ", " / ", "・")
_OPEN, _CLOSE = "（(", "）)"


def _quantile_pairs(line: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Split "コスト分位点 全体 p50 $1.06 / p90 $3.80" into a label and pXX pairs."""

    match = _QUANTILE_LINE.fullmatch(line)
    if match is None:
        return None
    pairs = []
    for chunk in match.group("values").split(" / "):
        pair = _QUANTILE_PAIR.fullmatch(chunk.strip())
        if pair is None:
            return None
        pairs.append((pair.group("term"), pair.group("value")))
    return match.group("label").strip(), tuple(pairs)


def _split_facts(value: str) -> list[str]:
    """Split on top-level separators only; "（原因別: a · b）" must stay one fact."""

    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character in _OPEN:
            depth += 1
        elif character in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0:
            separator = next(
                (item for item in _FACT_SEPARATORS if value.startswith(item, index)), None
            )
            if separator is not None:
                parts.append(value[start:index])
                index += len(separator)
                start = index
                continue
        index += 1
    parts.append(value[start:])
    return [part.strip() for part in parts if part.strip()]


def _split_label(line: str) -> tuple[str, str]:
    head, separator, tail = line.partition(": ")
    if separator and tail and not any(stop in head for stop in _LABEL_STOP):
        return head, tail
    return "", line


def _fact(label: str, body: str, *, labelled: bool = True) -> str:
    if not labelled:
        # A dl child div must carry a dt, so unlabelled insights skip the dl entirely.
        return f'<p class="fact-value">{body}</p>'
    return (
        f'<div class="fact"><dt class="fact-label">{_esc(label)}</dt>'
        f'<dd class="fact-value">{body}</dd></div>'
    )


def _insight(text: str) -> str:
    """Render an insight as discrete labeled facts; the source text is newline-separated."""

    if any(marker in text for marker in ("未知価格", "価格カバレッジ不足")) and "不完全" not in text:
        text += "\n集計は不完全です。"
    facts: list[tuple[str, tuple[tuple[str, str], ...] | None, str]] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        quantile = _quantile_pairs(line)
        if quantile is not None:
            facts.append((quantile[0], quantile[1], line))
            continue
        label, body = _split_label(line)
        facts.append((label, None, body))
    labelled = any(label for label, _pairs, _body in facts)
    rendered: list[str] = []
    # Maps an already-rendered quantile fact to its position in `rendered`.
    seen: dict[tuple[tuple[str, str], ...], int] = {}
    for label, pairs, body in facts:
        if pairs is not None:
            duplicate = seen.get(pairs) if "対話のみ" in label else None
            if duplicate is not None and "</dt>" in rendered[duplicate]:
                # The values are identical only because this window held no synthetic
                # prompts; annotate the kept row instead of dropping a real distinction.
                rendered[duplicate] = rendered[duplicate].replace(
                    "</dt>", f'<span class="same-note">{SAME_VALUES_NOTE}</span></dt>', 1
                )
                continue
            seen.setdefault(pairs, len(rendered))
            values = "".join(
                f'<span class="quantile"><span class="quantile-term">{_esc(term)}</span>'
                f'<span class="quantile-value">{_esc(value)}</span></span>'
                for term, value in pairs
            )
            rendered.append(
                _fact(label, f'<span class="quantiles">{values}</span>', labelled=labelled)
            )
            continue
        parts = _split_facts(body)
        value = "".join(f'<span class="subfact">{_esc(part)}</span>' for part in parts)
        rendered.append(_fact(label, value or _esc(body), labelled=labelled))
    body_html = "".join(rendered)
    if labelled:
        body_html = f'<dl class="facts">{body_html}</dl>'
    return f'<aside class="insight">{body_html}</aside>'


def _collapse_interactive(rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Merge an "X（対話のみ）" row into "X" only when every other cell is identical."""

    merged: list[tuple[str, ...]] = []
    for row in rows:
        if merged and row and len(row) == len(merged[-1]):
            previous = merged[-1]
            if row[0] == previous[0] + INTERACTIVE_SUFFIX and row[1:] == previous[1:]:
                label = f'{previous[0]}<span class="same-note">{SAME_VALUES_NOTE}</span>'
                merged[-1] = (label, *previous[1:])
                continue
        merged.append(row)
    return merged


def _node(node: Node, page: Page | None = None) -> str:
    args = node.args
    if node.kind == "join":
        return "".join(
            _node(value, page) if isinstance(value, Node) else _esc(value) for value in args
        )
    if node.kind == "insight":
        return _insight(str(args[0]))
    if node.kind in {"plain", "warning", "text_block"}:
        tag = "aside" if node.kind == "warning" else "p"
        value = str(args[0])
        if (
            any(marker in value for marker in ("未知価格", "価格カバレッジ不足"))
            and "不完全" not in value
        ):
            value += "。集計は不完全です。"
        classes = str(thaw(node.kwargs).get("cls") or "").strip()
        attribute = f' class="{_esc(classes)}"' if classes else ""
        return f"<{tag}{attribute}>{_esc(value)}</{tag}>"
    if node.kind == "code":
        value = str(args[0])
        match = DETAIL_COMMAND.fullmatch(value)
        if match is not None:
            category = "prompts" if match.group(1) == "explain" else "sessions"
            href = f"/{category}/{quote(match.group(2), safe='')}"
            return f'<a href="{_esc(href)}"><code>{_esc(value)}</code></a>'
        return f"<code>{_esc(value)}</code>"
    if node.kind == "code_lines":
        return "<p>" + "<br>".join(
            _node(Node("code", (value,)), page) for value in args[0]
        ) + "</p>"
    if node.kind == "heading":
        level = min(6, max(2, int(args[0])))
        return f"<h{level}>{_esc(args[1])}</h{level}>"
    if node.kind == "clip":
        options = thaw(node.kwargs)
        title = options.get("title", args[0])
        return f'<span title="{_esc(title)}">{_esc(args[0])}</span>'
    if node.kind in {"block", "insight_body"}:
        value = args[0]
        body = _node(value, page) if isinstance(value, Node) else _esc(value)
        return f"<div>{body}</div>"
    if node.kind == "card":
        value = args[0]
        body = _node(value, page) if isinstance(value, Node) else _esc(value)
        return f'<div class="card">{body}</div>'
    if node.kind == "legend":
        return "<p>" + " · ".join(_esc(label) for label, _color in args[0]) + "</p>"
    if node.kind == "tabs":
        return '<nav aria-label="集計軸">' + " ".join(
            f'<a href="#{_esc(panel_id)}">{_esc(label)}</a>' for panel_id, label, _active in args[1]
        ) + "</nav>"
    if node.kind == "grain_tabs":
        return '<nav aria-label="集計粒度">' + " ".join(
            f'<a href="#grain-{_esc(grain)}">{_esc(label)}</a>'
            + (f' <span>({_esc(note)})</span>' if note else "")
            for grain, label, _active, note in args[0]
        ) + "</nav>"
    if node.kind == "panel":
        body = _node(args[2], page)
        return f'<section id="{_esc(args[1])}">{body}</section>'
    if node.kind == "grain_panel":
        return f'<section id="grain-{_esc(args[0])}">{_node(args[1], page)}</section>'
    if node.kind == "table":
        columns, rows = args[:2]
        if page is not None:
            rows = rows[page.offset : page.offset + page.limit]
        rendered_rows = []
        for row_value in rows:
            row = row_value.cells if isinstance(row_value, Row) else row_value
            cells = [_cell(cell, page) for cell in row]
            if isinstance(row_value, Row) and row_value.highlight and cells:
                cells[0] = '<span class="threshold">⚠ 注目</span> ' + cells[0]
            rendered_rows.append(tuple(cells))
        empty = '<p class="empty">このページに該当するデータはありません。</p>' if not rows else ""
        return empty + _trusted_table(
            tuple(column.label for column in columns), _collapse_interactive(rendered_rows)
        )
    if node.kind == "stacked_bars":
        options = thaw(node.kwargs)
        return _series_table(args[0], args[1], money=bool(options.get("money_values")))
    if node.kind == "line_chart":
        options = thaw(node.kwargs)
        return _series_table(
            args[0],
            args[1],
            money=bool(options.get("money_axis")),
            suffix=str(args[3] or ""),
        )
    if node.kind == "volume_chart":
        series = thaw(args[1])
        if args[3] is not None:
            series["7日移動平均"] = list(args[3])
        return _series_table(args[0], series, money=True)
    if node.kind == "cache_balance":
        series = {"cache read": args[1], "write 5m": args[2], "write 1h": args[3]}
        return _series_table(args[0], series, money=True)
    raise ValueError(f"unsupported dashboard node: {node.kind}")


def _cell(cell: Cell, page: Page | None = None) -> str:
    if cell.content is not None:
        value = _node(cell.content, page)
    else:
        value = cell.text.display if isinstance(cell.text, Money) else _esc(cell.text)
    if cell.warn:
        value = '<span class="threshold">⚠ 注意</span> ' + value
    elif isinstance(cell.text, str) and "⚠" in cell.text:
        value = '<span class="threshold">⚠ 未知価格を含むため比較は不完全</span> ' + value
    if cell.title:
        value = f'<span title="{_esc(cell.title)}">{value}</span>'
    return value


def _kpi_line(total: object, page: Page | None = None) -> str:
    """Promote the headline figures out of body-text weight without altering them."""

    if isinstance(total, Node):
        return f'<p class="kpi">{_node(total, page)}</p>'
    text = "" if total is None else str(total)
    parts = [part for part in text.split(" · ") if part]
    if len(parts) < 2:
        return f'<p class="kpi">{_esc(text)}</p>'
    separator = '<span class="kpi-sep" aria-hidden="true">·</span>'
    return '<p class="kpi">' + separator.join(
        f'<span class="kpi-item">{_esc(part)}</span>' for part in parts
    ) + "</p>"


def _legacy(model: LegacyViewModel, page: Page | None = None) -> str:
    return (
        f'<section><h2>{_esc(model.title)}</h2>{_kpi_line(model.total, page)}'
        f'{_node(model.body, page)}</section>'
    )


def _period(model: LegacyViewModel) -> str:
    return f'<section><h2>集中先</h2>{_kpi_line(model.total)}{_node(model.body)}</section>'


def _pagination(request) -> str:
    links = []
    if request.page.page > 1:
        previous = _base_values(request) + [("page", str(request.page.page - 1))]
        links.append(f'<a rel="prev" href="{_url("/dashboard", previous)}">前へ</a>')
    following = _base_values(request) + [("page", str(request.page.page + 1))]
    links.append(f'<a rel="next" href="{_url("/dashboard", following)}">次へ</a>')
    return '<nav class="pagination" aria-label="ページング">' + " ".join(links) + "</nav>"


def _freshness_header(freshness: Freshness | None) -> str:
    if freshness is None or not freshness.stale:
        return ""
    if freshness.last_ingest is None:
        stamp = "記録なし"
        elapsed = "不明"
    else:
        stamp = dt.datetime.fromtimestamp(freshness.last_ingest).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        seconds = int(freshness.age_seconds or 0)
        elapsed = f"{seconds // 86400}日{seconds % 86400 // 3600}時間" if seconds >= 86400 else f"{seconds // 60}分"
    return (
        '<aside role="status"><strong>台帳の取込が遅れています。</strong> '
        f'最終正常取込: {_esc(stamp)}（経過 {_esc(elapsed)}）。過去データを表示しています。</aside>'
    )


def _shell(title: str, content: str, freshness: Freshness | None = None, header: str = "") -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/dashboard.css">'
        f'<title>{_esc(title)}</title></head><body><header><h1>{_esc(title)}</h1>'
        f'{_freshness_header(freshness)}{header}</header><main>{content}</main></body></html>'
    )


def stylesheet() -> str:
    """Return local-only CSS; dashboard markup remains centralized in this module."""

    return """
:root {
  color-scheme: light dark;
  font-family: system-ui, -apple-system, "Segoe UI", "Helvetica Neue", "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
  line-height: 1.55;
  --bg: #ffffff;
  --fg: #16181d;
  --muted: #5c6472;
  --rule: #dcdfe6;
  --rule-strong: #b9bfcb;
  --surface: #f5f6f9;
  --surface-alt: #eceef3;
  --accent: #1b4fd8;
  --accent-fg: #ffffff;
  --warn: #8a4b00;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171c;
    --fg: #e6e9ef;
    --muted: #9aa2b1;
    --rule: #2b303a;
    --rule-strong: #414957;
    --surface: #1b1e25;
    --surface-alt: #23262f;
    --accent: #8fb4ff;
    --accent-fg: #15171c;
    --warn: #f0b866;
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 96rem; padding: 1.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg); }
h1 { font-size: 1.05rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); margin: 0 0 .75rem; }
h2 { font-size: 1.15rem; font-weight: 650; margin: 0 0 .5rem; padding-bottom: .35rem; border-bottom: 2px solid var(--rule-strong); }
h3 { font-size: .8rem; font-weight: 600; letter-spacing: .03em; color: var(--muted); margin: 0 0 .25rem; }
section { margin-block: 2rem; }
p { margin-block: .5rem; }
a { color: var(--accent); }
.dim, .lead + .dim { color: var(--muted); font-size: .875rem; }
.backlink { font-size: .875rem; }

/* --- header / controls ------------------------------------------------ */
header { border-bottom: 1px solid var(--rule); padding-bottom: .5rem; margin-bottom: 1.25rem; }
.window { display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem; margin: 0 0 .75rem; }
.window-preset { font-weight: 650; }
.window-range { color: var(--muted); font-variant-numeric: tabular-nums; }
.controls { display: flex; flex-direction: column; gap: .85rem; }
nav, form { display: flex; flex-wrap: wrap; gap: .4rem; margin-block: 0; }
.tabs { gap: 0; border-bottom: 1px solid var(--rule); }
.tabs a { padding: .5rem .95rem; text-decoration: none; color: var(--muted); border: 1px solid transparent; border-bottom: none; border-radius: .35rem .35rem 0 0; margin-bottom: -1px; }
.tabs a:hover { color: var(--fg); background: var(--surface); }
.tabs a[aria-current] { color: var(--fg); font-weight: 700; background: var(--bg); border-color: var(--rule); border-bottom: 1px solid var(--bg); }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1.25rem; }
.presets { gap: .35rem; padding: .2rem; background: var(--surface); border: 1px solid var(--rule); border-radius: .45rem; }
.presets a { padding: .3rem .7rem; font-size: .875rem; text-decoration: none; color: var(--muted); border-radius: .3rem; }
.presets a:hover { color: var(--fg); background: var(--surface-alt); }
.presets a[aria-current] { color: var(--accent-fg); background: var(--accent); font-weight: 700; text-decoration: underline; text-underline-offset: .18em; }
form { align-items: end; gap: .5rem .75rem; padding-left: 1.25rem; border-left: 1px solid var(--rule); }
.field { display: flex; flex-direction: column; gap: .2rem; font-size: .75rem; color: var(--muted); }
input { font: inherit; font-size: .875rem; padding: .3rem .45rem; color: var(--fg); background: var(--bg); border: 1px solid var(--rule-strong); border-radius: .3rem; }
button { font: inherit; font-size: .875rem; font-weight: 600; padding: .4rem 1rem; color: var(--accent-fg); background: var(--accent); border: 1px solid transparent; border-radius: .3rem; cursor: pointer; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: .2rem solid var(--accent); outline-offset: .2rem; }
.pagination { justify-content: flex-end; gap: .75rem; margin-top: 2rem; }

/* --- headline numbers -------------------------------------------------- */
.kpi { display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem; margin: 0 0 1rem; font-variant-numeric: tabular-nums; }
.kpi-item { font-size: 1.05rem; color: var(--muted); }
.kpi-item:first-child { font-size: 1.6rem; font-weight: 700; line-height: 1.2; color: var(--fg); }
.kpi-sep { color: var(--rule-strong); }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem; }
.kpi-card { padding: .75rem .9rem; background: var(--surface); border: 1px solid var(--rule); border-radius: .5rem; }
.kpi-value { margin: 0; font-size: 1.5rem; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; }
.kpi-delta { margin: .2rem 0 0; font-size: .8rem; color: var(--muted); font-variant-numeric: tabular-nums; }

/* --- insight facts ----------------------------------------------------- */
.insight { padding: .9rem 1rem; background: var(--surface); border: 1px solid var(--rule); border-left: .25rem solid var(--rule-strong); border-radius: .35rem; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr)); gap: .6rem 1.75rem; margin: 0; }
.fact { display: flex; flex-direction: column; gap: .15rem; min-width: 0; }
.fact-label { font-size: .75rem; font-weight: 600; letter-spacing: .02em; color: var(--muted); }
.fact-value { display: flex; flex-wrap: wrap; align-items: baseline; gap: .15rem .9rem; margin: 0; font-variant-numeric: tabular-nums; }
.subfact { white-space: nowrap; }
.quantiles { display: flex; flex-wrap: wrap; gap: .1rem .9rem; }
.quantile { display: inline-flex; align-items: baseline; gap: .3rem; }
.quantile-term { font-size: .7rem; font-weight: 600; color: var(--muted); }
.quantile-value { font-weight: 600; font-variant-numeric: tabular-nums; }
.same-note { margin-left: .35rem; font-size: .7rem; font-weight: 400; color: var(--muted); }

/* --- tables ------------------------------------------------------------ */
.table-wrap { max-width: 100%; overflow-x: auto; border: 1px solid var(--rule); border-radius: .4rem; }
table { border-collapse: collapse; width: 100%; font-size: .875rem; }
th, td { padding: .45rem .7rem; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
th { position: sticky; top: 0; z-index: 1; font-size: .75rem; font-weight: 600; letter-spacing: .02em; color: var(--muted); background: var(--surface-alt); border-bottom: 1px solid var(--rule-strong); }
td { border-top: 1px solid var(--rule); }
tbody tr:first-child td { border-top: none; }
tbody tr:nth-child(even) { background: var(--surface); }
tbody tr:hover { background: var(--surface-alt); }
th:first-child, td:first-child { text-align: left; }
.card { max-width: 100%; overflow-x: auto; }
.threshold { font-weight: 700; color: var(--warn); }
.empty { color: var(--muted); font-size: .875rem; }
.empty, aside { padding: .75rem .9rem; background: var(--surface); border: 1px solid var(--rule); border-radius: .35rem; }
.meta { display: grid; grid-template-columns: auto 1fr; gap: .3rem 1rem; margin: 0 0 1.25rem; }
.meta dt { font-size: .75rem; font-weight: 600; color: var(--muted); }
.meta dd { margin: 0; font-variant-numeric: tabular-nums; }

@media (max-width: 40rem) {
  body { padding: .75rem .75rem 3rem; }
  form { padding-left: 0; border-left: none; }
  form label { flex-basis: 100%; }
  .kpi-item:first-child { font-size: 1.35rem; }
  .facts { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation: none !important; } }
""".strip()


def dashboard_page(
    request,
    model,
    today: dt.date,
    freshness: Freshness | None = None,
) -> str:
    if request.view == "overview":
        content = _overview(model)
    elif request.view == "period":
        # period/overview already apply Page in their query models.
        content = _period(model)
    else:
        # The legacy trend/cache/dist DTOs contain complete table rows. Dashboard
        # pagination is a presentation slice and never changes their aggregates.
        content = _legacy(model, request.page)
    preset_labels = {
        "yesterday": "昨日",
        "today": "今日",
        "7d": "直近7日",
        "month": "今月",
        "last-month": "先月",
        "custom": "カスタム",
    }
    header = (
        f'<p class="window"><span class="window-preset">{_esc(preset_labels[request.preset])}</span>'
        f'<span class="window-range">{_esc(request.window.start)} — {_esc(request.window.end)}</span></p>'
        f'{_controls(request, today)}'
    )
    return _shell("metsuke dashboard", f"{content}{_pagination(request)}", freshness, header)


def _timestamp(value: float) -> str:
    return dt.datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _trace_form(session_id: str, csrf_token: str | None, prompt_id: str | None = None) -> str:
    if csrf_token is None:
        return ""
    prompt = (
        f'<input type="hidden" name="prompt_id" value="{_esc(prompt_id)}">'
        if prompt_id is not None
        else ""
    )
    return (
        '<form method="post" action="/trace-jobs">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf_token)}">'
        f'<input type="hidden" name="session_id" value="{_esc(session_id)}">'
        f'{prompt}<button type="submit">traceで見る</button></form>'
    )


def prompt_page(
    model: prompt.PromptModel,
    freshness: Freshness | None = None,
    csrf_token: str | None = None,
) -> str:
    warning = (
        f'<aside>未知価格 request: {_esc(model.unknown_cost_request_count)}。'
        '表示合計は不完全です。</aside>'
        if model.unknown_cost_request_count
        else ""
    )
    session_link = (
        f'<a href="/sessions/{quote(model.session_id, safe="")}">'
        f'{_esc(model.session_id)}</a>'
    )
    rows = []
    for item in model.requests:
        rows.append(
            (
                _esc(_timestamp(item.ts)),
                _esc(item.model or "?"),
                _esc(item.agent_id or "—"),
                _esc(item.input_tok or 0),
                _esc(item.cache_read_tok or 0),
                _esc((item.cache_w5m_tok or 0) + (item.cache_w1h_tok or 0)),
                _esc(item.output_tok or 0),
                _esc(item.tool_count),
                _esc(item.amount.display),
                _esc("中断" if item.interrupted else ""),
            )
        )
    requests = _trusted_table(
        ("時刻", "model", "agent", "input", "cache read", "cache作成", "output", "tools", "金額", "状態"),
        rows,
    )
    content = (
        '<p class="backlink"><a href="/dashboard">dashboardへ戻る</a></p>'
        f'<dl class="meta"><dt>prompt ID</dt><dd>{_esc(model.prompt_id)}</dd>'
        f'<dt>session</dt><dd>{session_link}</dd>'
        f'<dt>API換算コスト</dt><dd>{_esc(model.amount.display)}</dd>'
        f'<dt>request</dt><dd>{_esc(len(model.requests))}</dd>'
        f'<dt>支配項</dt><dd>{_esc(model.dominant.term)} ({model.dominant.share_pct:.0f}%)</dd></dl>'
        f'{warning}<section><h2>prompt</h2><p>{_esc(model.text or "—")}</p></section>'
        f'{_trace_form(model.session_id, csrf_token, model.prompt_id)}'
        f'<section><h2>request内訳</h2>{requests}</section>'
    )
    return _shell("prompt詳細", content, freshness)


def session_page(
    model: session.SessionModel,
    freshness: Freshness | None = None,
    csrf_token: str | None = None,
) -> str:
    warning = (
        f'<aside>未知価格 request: {_esc(model.unknown_cost_request_count)}。'
        '表示合計は不完全です。</aside>'
        if model.unknown_cost_request_count
        else ""
    )
    rows = []
    for item in model.prompts:
        link = f'<a href="/prompts/{quote(item.prompt_id, safe="")}">{_esc(item.text or item.prompt_id)}</a>'
        incomplete = f"未知価格 {item.unknown_cost_request_count}" if item.unknown_cost_request_count else ""
        rows.append((link, _esc(_timestamp(item.ts)), _esc(item.request_count), _esc(item.amount.display), _esc(incomplete)))
    prompts = _trusted_table(("prompt", "時刻", "request", "金額", "状態"), rows)
    models = "、".join(f"{name}: {count}" for name, count in model.models)
    content = (
        '<p class="backlink"><a href="/dashboard">dashboardへ戻る</a></p>'
        f'<dl class="meta"><dt>session ID</dt><dd>{_esc(model.session_id)}</dd>'
        f'<dt>project</dt><dd>{_esc(model.project or "—")}</dd>'
        f'<dt>期間</dt><dd>{_esc(_timestamp(model.first_ts))} — {_esc(_timestamp(model.last_ts))}</dd>'
        f'<dt>API換算コスト</dt><dd>{_esc(model.amount.display)}</dd>'
        f'<dt>request</dt><dd>{_esc(model.request_count)}</dd>'
        f'<dt>models</dt><dd>{_esc(models)}</dd></dl>{warning}'
        f'{_trace_form(model.session_id, csrf_token)}'
        f'<section><h2>prompt一覧</h2>{prompts}</section>'
    )
    return _shell("session詳細", content, freshness)


def trace_url(session_id: str, fragment: str = "") -> str:
    """Build the same-origin trace URL. Callers must pass an allowlist-validated ID."""

    return f"/traces/{session_id}.html{fragment}"


def trace_job_page(status: str, job_id: str, trace_target: str = "") -> str:
    """Render job status; a ready job navigates this same tab to the trace."""

    values = {
        "queued": ("traceを待機しています", "生成の順番を待っています。"),
        "running": ("traceを生成しています", "完了までこのページを更新できます。"),
        "ready": ("traceへ移動します", "生成したtraceをこのタブで開きます。"),
        "failed": ("traceを生成できませんでした", "再試行するか metsuke doctor を確認してください。"),
    }
    title, message = values[status]
    # The Refresh header performs the navigation; this link is the fallback for a
    # browser that does not honour it. No JavaScript is involved either way.
    action = (
        f'<p><a href="{_esc(trace_target)}">traceを開く</a></p>'
        if status == "ready" and trace_target
        else f'<p><a href="/trace-jobs/{_esc(job_id)}">状態を更新</a></p>'
    )
    content = (
        f'<p>{_esc(message)}</p>{action}'
        '<p class="backlink"><a href="/dashboard">dashboardへ戻る</a></p>'
    )
    return _shell(title, content)


def state_page(kind: str, *, retry_path: str = "/dashboard") -> str:
    states = {
        "initial_sync": (
            "初期同期が必要です",
            "台帳がまだありません。Metsuke.appで初期同期を完了してから開き直してください。",
            "metsuke sync を実行し、解決しない場合は metsuke doctor を確認してください。",
        ),
        "busy": (
            "台帳を読み込み中です",
            "ingesterはそのまま動かしてください。短い時間をおいて再試行できます。",
            "",
        ),
        "unavailable": (
            "台帳を読み込めません",
            "ローカル台帳の状態を確認できませんでした。",
            "metsuke doctor を確認してください。",
        ),
        "not_found": (
            "詳細が見つかりません",
            "IDが存在しない、不正なprefixである、またはデータが削除済みです。",
            "",
        ),
        "stale": (
            "このページは古くなっています",
            "このページは古くなっています。再読み込みしてから、もう一度お試しください。",
            "",
        ),
        "port_conflict": (
            "dashboardを起動できません",
            "指定portは別のprocessが使用しています。別サービスへは接続していません。",
            "metsuke doctor を確認してください。",
        ),
        "loading": (
            "表示を準備しています",
            "集計が完了するまでお待ちください。別のタブへ移動することもできます。",
            "",
        ),
        "empty": (
            "選択期間にデータがありません",
            "期間またはprojectを変更して、別の投影を確認できます。",
            "",
        ),
        "response_too_large": (
            "表示件数を減らしてください",
            "安全な応答サイズを超えました。期間または一覧件数を小さくして再試行してください。",
            "",
        ),
    }
    title, message, help_text = states[kind]
    if kind == "busy":
        action = f'<p><a href="{_esc(retry_path)}">再試行</a></p>'
    else:
        action = '<p class="backlink"><a href="/dashboard">dashboardへ戻る</a></p>'
    view_links = " ".join(
        f'<a class="tab" href="/dashboard?view={view}&amp;range=yesterday">{_esc(label)}</a>'
        for view, label in (
            ("overview", "概要"),
            ("period", "期間"),
            ("trend", "推移"),
            ("cache", "キャッシュ"),
            ("dist", "分布"),
        )
    )
    content = (
        f'<nav class="tabs" aria-label="表示">{view_links}</nav>'
        f'<p class="lead">{_esc(message)}</p><p class="dim">{_esc(help_text)}</p>{action}'
    )
    return _shell(title, content)
