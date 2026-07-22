from __future__ import annotations

import math
from collections import Counter, defaultdict

from . import common as ui
from .common import Cell, Column, LegacyViewModel, Window

DistModel = LegacyViewModel


def _model_group(value: str | None) -> str:
    lowered = (value or "").lower()
    return next(
        (name for name in ("fable", "opus", "sonnet", "haiku") if name in lowered),
        "other",
    )


def _automatic(prompt: dict) -> bool:
    project = prompt["project"] or ""
    return "sandbox-experiments" in project or project.startswith("-private-tmp-")


def _percentile(values, quantile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    point = (len(ordered) - 1) * quantile
    lower = int(point)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def _examples(items: list[dict]) -> ui.Node | None:
    chosen = sorted(items, key=lambda prompt: (-prompt["cost"], prompt["prompt_id"]))[:2]
    commands = [f"metsuke explain {prompt['prompt_id'][:8]} --html" for prompt in chosen]
    return ui.code_lines(commands) if commands else None


def _bar_cell(value: float, maximum: float) -> Cell:
    return Cell(ui.money(value), sort=value, bar=0 if not maximum else value / maximum)


def _share_cell(value: float) -> Cell:
    return Cell(f"{value:.1f}%", sort=value, bar=min(1, max(0, value / 100)))


def _columns(items: list[tuple[str, bool]]) -> list[Column]:
    return [Column(label, cls="left" if left else "") for label, left in items]


def query(conn, window: Window) -> DistModel:
    """Return title, period label, total markup, and body markup."""
    lower, upper = window.sql_bounds()
    where = "datetime(r.ts,'unixepoch','localtime')>=? and datetime(r.ts,'unixepoch','localtime')<?"
    params: list[object] = [lower, upper]
    project_where = ""
    if window.project is not None:
        project_where = " and s.project = ?"
        params.append(window.project)

    if window.project is None:
        total, unknown = conn.execute(
            f"select coalesce(sum(r.cost_usd),0),sum(r.cost_usd is null) "
            f"from v_request_cost r where {where}",
            (lower, upper),
        ).fetchone()
    else:
        total, unknown = conn.execute(
            f"select coalesce(sum(r.cost_usd),0),sum(r.cost_usd is null) "
            f"from v_request_cost r join session s using(session_id) "
            f"where {where}{project_where}",
            params,
        ).fetchone()
    prompts = [
        dict(row)
        for row in conn.execute(
            f"""select r.prompt_id,s.project,sum(r.cost_usd) cost,count(*) nr,
            max(r.ts)-min(r.ts) span,max(r.is_interrupted) intr,
            sum(case when r.agent_id is not null then r.cost_usd else 0 end) delegated
            from v_request_cost r join session s using(session_id)
            where {where}{project_where} and r.prompt_id is not null group by r.prompt_id""",
            params,
        )
    ]
    peak = {}
    for row in conn.execute(
        f"""select r.prompt_id,r.model,coalesce(r.input_tok,0)+coalesce(r.cache_read_tok,0)
        +coalesce(r.cache_w5m_tok,0)+coalesce(r.cache_w1h_tok,0) context
        from v_request_cost r join session s using(session_id)
        where {where}{project_where} and r.prompt_id is not null and r.agent_id is null""",
        params,
    ):
        candidate = (row["context"], _model_group(row["model"]))
        if row["prompt_id"] not in peak or candidate[0] > peak[row["prompt_id"]][0]:
            peak[row["prompt_id"]] = candidate

    costs = [prompt["cost"] for prompt in prompts]
    attributed = sum(costs)
    interactive = [prompt for prompt in prompts if not _automatic(prompt)]
    count = len(prompts)
    ranked = sorted(prompts, key=lambda prompt: (-prompt["cost"], prompt["prompt_id"]))
    top_count = math.ceil(count * 0.1)
    high, rest = ranked[:top_count], ranked[top_count:]
    with_peak = [prompt for prompt in prompts if prompt["prompt_id"] in peak]
    over_200 = [prompt for prompt in with_peak if peak[prompt["prompt_id"]][0] >= 200000]
    count_share = len(over_200) / len(with_peak) * 100 if with_peak else 0
    cost_share = (
        sum(prompt["cost"] for prompt in over_200)
        / sum(prompt["cost"] for prompt in with_peak)
        * 100
        if with_peak
        else 0
    )
    insight_text = (
        f"≥200k: 件数 {count_share:.1f}% → コスト {cost_share:.1f}%\n"
        f"cost上位10%: req中央値 {_percentile([p['nr'] for p in high], 0.5):.1f}"
        f"（残りは {_percentile([p['nr'] for p in rest], 0.5):.1f}）・観測スパン "
        f"{_percentile([p['span'] / 60 for p in high], 0.5):.1f}分\n"
        f"コスト分位点 全体 p50 {ui.money(_percentile(costs, 0.5))} / "
        f"p90 {ui.money(_percentile(costs, 0.9))} / "
        f"p95 {ui.money(_percentile(costs, 0.95))} / "
        f"p99 {ui.money(_percentile(costs, 0.99))}\n"
        f"全体（対話のみ） p50 {ui.money(_percentile([p['cost'] for p in interactive], 0.5))} / "
        f"p90 {ui.money(_percentile([p['cost'] for p in interactive], 0.9))} / "
        f"p95 {ui.money(_percentile([p['cost'] for p in interactive], 0.95))} / "
        f"p99 {ui.money(_percentile([p['cost'] for p in interactive], 0.99))}"
    )

    by_project: dict[str | None, list[dict]] = defaultdict(list)
    for prompt in prompts:
        by_project[prompt["project"]].append(prompt)
    ordered = sorted(
        by_project, key=lambda key: sum(prompt["cost"] for prompt in by_project[key]), reverse=True
    )
    groups = [(ui.project_name(key), by_project[key], True) for key in ordered[:7]]
    groups.append(("その他", [p for key in ordered[7:] for p in by_project[key]], False))
    groups.extend((("全体", prompts, False), ("全体（対話のみ）", interactive, False)))
    maximum = max((sum(prompt["cost"] for prompt in items) for _, items, _ in groups), default=0)
    quantile_max = max(
        (_percentile([prompt["cost"] for prompt in items], 0.95) for _, items, _ in groups),
        default=0,
    )
    project_rows = []
    for name, items, is_project in groups:
        values = [prompt["cost"] for prompt in items]
        unstable = is_project and len(items) < 20
        project_rows.append(
            [
                Cell(name, "left"),
                Cell(str(len(items)), sort=len(items)),
                _bar_cell(sum(values), maximum),
                _bar_cell(_percentile(values, 0.5), quantile_max),
                Cell("—") if unstable else _bar_cell(_percentile(values, 0.9), quantile_max),
                Cell("—") if unstable else _bar_cell(_percentile(values, 0.95), quantile_max),
            ]
        )
    section1 = ui.join(
        ui.heading(2, "コスト分位点 × プロジェクト"),
        ui.table(
            _columns(
                [
                    ("project", True),
                    ("n", False),
                    ("合計", False),
                    ("p50", False),
                    ("p90", False),
                    ("p95", False),
                ]
            ),
            project_rows,
        ),
    )

    bands = [("<200k", 0, 200000), ("≥200k–<500k", 200000, 500000), ("≥500k", 500000, float("inf"))]
    band_groups = [
        (name, [p for p in with_peak if low <= peak[p["prompt_id"]][0] < high_bound])
        for name, low, high_bound in bands
    ]
    maximum = max((sum(prompt["cost"] for prompt in items) for _, items in band_groups), default=0)
    band_rows = []
    for name, members in band_groups:
        choices = [
            ("", members, with_peak, with_peak),
            (
                "（対話のみ）",
                [p for p in members if not _automatic(p)],
                [p for p in with_peak if not _automatic(p)],
                [p for p in with_peak if not _automatic(p)],
            ),
        ]
        for label, items, population, cost_population in choices:
            models = Counter(peak[p["prompt_id"]][1] for p in items)
            main = " · ".join(f"{key} {value}" for key, value in models.most_common(2)) or "—"
            value = sum(prompt["cost"] for prompt in items)
            cost_total = sum(prompt["cost"] for prompt in cost_population)
            count_share_value = len(items) / len(population) * 100 if population else 0
            cost_share_value = value / cost_total * 100 if cost_total else 0
            examples = _examples(items)
            band_rows.append(
                [
                    Cell(name + label, "left"),
                    Cell(str(len(items))),
                    _share_cell(count_share_value),
                    _bar_cell(value, maximum),
                    _share_cell(cost_share_value),
                    Cell(ui.money(_percentile([p["cost"] for p in items], 0.5))),
                    Cell(main, "left"),
                    Cell("—" if examples is None else "", "left", content=examples),
                ]
            )
    missing = count - len(with_peak)
    million = sum(peak[p["prompt_id"]][0] >= 1000000 for p in with_peak)
    section2 = ui.join(
        ui.heading(2, "ピークコンテキスト帯"),
        ui.table(
            _columns(
                [
                    ("帯", True),
                    ("prompts", False),
                    ("件数シェア", False),
                    ("コスト", False),
                    ("コストシェア", False),
                    ("$/prompt中央値", False),
                    ("主モデル", True),
                    ("代表例", True),
                ]
            ),
            band_rows,
        ),
        ui.text_block(
            f"メインレーンrequestなし {missing} prompts（帯表から除外） · ≥1M {million} prompts",
            cls="dim",
        ),
    )

    def profile(name: str, items: list[dict], show_examples: bool = False) -> list[Cell]:
        delegated = sum(prompt["delegated"] for prompt in items)
        value = sum(prompt["cost"] for prompt in items)
        examples = _examples(items) if show_examples else None
        return [
            Cell(name, "left"),
            Cell(str(len(items))),
            Cell(f"{_percentile([p['nr'] for p in items], 0.5):.1f}"),
            Cell(f"{_percentile([p['span'] / 60 for p in items], 0.5):.1f}分"),
            Cell(f"{sum(p['intr'] for p in items) / len(items) * 100 if items else 0:.1f}%"),
            Cell(f"{delegated / value * 100 if value else 0:.1f}%"),
            Cell(f"{_percentile([p['nr'] for p in items], 0.9):.1f}"),
            Cell("—" if examples is None else "", "left", content=examples),
        ]

    rest_median = _percentile([prompt["nr"] for prompt in rest], 0.5)
    req_ratio = (
        _percentile([prompt["nr"] for prompt in high], 0.5) / rest_median if rest_median else 0
    )
    rest_span = _percentile([prompt["span"] for prompt in rest], 0.5)
    span_ratio = (
        _percentile([prompt["span"] for prompt in high], 0.5) / rest_span if rest_span else 0
    )
    delegate_high = (
        sum(prompt["delegated"] for prompt in high) / sum(prompt["cost"] for prompt in high) * 100
        if high
        else 0
    )
    delegate_rest = (
        sum(prompt["delegated"] for prompt in rest) / sum(prompt["cost"] for prompt in rest) * 100
        if rest
        else 0
    )
    section3 = ui.join(
        ui.heading(2, "cost上位10%のプロファイル"),
        ui.table(
            _columns(
                [
                    ("", True),
                    ("n", False),
                    ("req中央値", False),
                    ("観測スパン中央値", False),
                    ("中断率", False),
                    ("委任費率", False),
                    ("req p90", False),
                    ("代表例", True),
                ]
            ),
            [
                profile("cost上位10%", high, True),
                profile("残り90%", rest),
                profile("全体", prompts),
            ],
        ),
        ui.text_block(
            f"上位10%は残り90%よりreq中央値が {req_ratio:.1f}倍・観測スパン中央値が {span_ratio:.1f}倍・委任費率が {delegate_high - delegate_rest:+.1f}pt。",
            cls="dim",
        ),
    )
    warning = f" · 未知価格 {unknown} requests" if unknown else ""
    total_text = (
        f"{count:,} prompts · 合計{ui.money(attributed)} · "
        f"帰属カバレッジ {attributed / total * 100 if total else 0:.1f}%{warning}"
    )
    footer = ui.text_block(
        "対話のみ判別は暫定: projectに sandbox-experiments を含む、または -private-tmp- で始まるものを自動実行として除外。注記は機械生成。",
        cls="dim",
    )
    body = ui.join(ui.insight(insight_text), section1, section2, section3, footer)
    return LegacyViewModel(
        "metsuke · プロンプト分布",
        window.label,
        ui.plain(total_text),
        body,
        ui.local_timezone(),
    )
