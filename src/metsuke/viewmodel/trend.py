from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict

from . import common as ui
from .common import Cell, Column, LegacyViewModel, Window

TrendModel = LegacyViewModel

COLORS = {
    "cache_read": "#2dd4bf",
    "cache_w5m": "#facc15",
    "cache_w1h": "#fb923c",
    "input": "#94a3b8",
    "output": "#f472b6",
    "fable": "#a78bfa",
    "opus": "#f59e0b",
    "sonnet": "#38bdf8",
    "haiku": "#34d399",
    "other": "#6b7280",
}


def _model(value):
    lowered = (value or "").lower()
    return next((name for name in ("fable", "opus", "sonnet", "haiku") if name in lowered), "other")


def _auto(value):
    value = value or ""
    return "sandbox-experiments" in value or value.startswith("-private-tmp-")


def _pct(values, quantile):
    if not values:
        return 0
    ordered = sorted(values)
    point = (len(ordered) - 1) * quantile
    lower = int(point)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def _blank(keys, count):
    return {key: [0.0] * count for key in keys}


def _monday(day):
    return day - dt.timedelta(days=day.weekday())


def _month(day):
    return day.replace(day=1)


def _prev_month(day):
    return (day - dt.timedelta(days=1)).replace(day=1)


def query(conn, window: Window) -> TrendModel:
    days = [
        window.start + dt.timedelta(days=index)
        for index in range((window.end - window.start).days + 1)
    ]
    count = len(days)
    lo, hi = window.sql_bounds()
    indexes = {str(day): index for index, day in enumerate(days)}
    measured_row = conn.execute(
        "select date(min(ts),'unixepoch','localtime') from v_request_cost"
    ).fetchone()
    measured = (
        dt.date.fromisoformat(measured_row[0]) if measured_row and measured_row[0] else window.start
    )
    where = "datetime(r.ts,'unixepoch','localtime')>=? and datetime(r.ts,'unixepoch','localtime')<?"
    params = [lo, hi]
    project_clause = ""
    if window.project is not None:
        project_clause = " and s.project = ?"
        params.append(window.project)
    rows = conn.execute(
        f"""select date(r.ts,'unixepoch','localtime') day,r.model,s.project,
    sum(r.input_tok*r.in_usd/1e6) input,sum(coalesce(r.output_tok,0)*r.out_usd/1e6) output,sum(r.cache_read_tok*r.in_usd*r.cache_read_x/1e6) cache_read,sum(r.cache_w5m_tok*r.in_usd*r.cache_w5m_x/1e6) cache_w5m,sum(r.cache_w1h_tok*r.in_usd*r.cache_w1h_x/1e6) cache_w1h,sum(r.cost_usd) cost
    from v_request_cost r left join session s using(session_id) where {where}{project_clause} group by day,r.model,s.project""",
        params,
    ).fetchall()
    fee = _blank(["cache_read", "cache_w5m", "cache_w1h", "input", "output"], count)
    models = _blank(["fable", "opus", "sonnet", "haiku", "other"], count)
    project_totals = defaultdict(float)
    for row in rows:
        project_totals[row["project"]] += row["cost"] or 0
    tops = [
        key for key, _ in sorted(project_totals.items(), key=lambda item: item[1], reverse=True)[:6]
    ]
    projects = _blank([ui.project_name(key) for key in tops] + ["その他"], count)
    for row in rows:
        index = indexes[row["day"]]
        for key in fee:
            fee[key][index] += row[key] or 0
        models[_model(row["model"])][index] += row["cost"] or 0
        projects[ui.project_name(row["project"]) if row["project"] in tops else "その他"][index] += (
            row["cost"] or 0
        )
    daily_total = [sum(fee[key][index] for key in fee) for index in range(count)]
    moving = [
        sum(daily_total[max(0, index - 6) : index + 1]) / min(7, index + 1)
        for index in range(count)
    ]
    grain_labels = {
        "daily": days,
        "weekly": sorted({_monday(day) for day in days}),
        "monthly": sorted({_month(day) for day in days}),
    }

    def aggregate(series, grain):
        labels = grain_labels[grain]
        positions = {value: index for index, value in enumerate(labels)}
        output = _blank(series.keys(), len(labels))
        key_fn = (
            (lambda day: day) if grain == "daily" else (_monday if grain == "weekly" else _month)
        )
        for name, values in series.items():
            for day, value in zip(days, values):
                output[name][positions[key_fn(day)]] += value
        return output

    tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    markers = []
    regimes = []
    lo_ts = dt.datetime.fromisoformat(lo).timestamp()
    hi_ts = dt.datetime.fromisoformat(hi).timestamp()
    if "marker" in tables:
        markers = conn.execute(
            "select ts_start,ts_end,category,verdict from marker where ts_start<? and coalesce(ts_end,?)>=?",
            (hi_ts, hi_ts, lo_ts),
        ).fetchall()
    if "regime_event" in tables:
        regimes = conn.execute(
            "select ts,kind from regime_event where ts>=? and ts<?", (lo_ts, hi_ts)
        ).fetchall()
    marker_notes = []
    for index, marker in enumerate(markers):
        after_end = min(marker["ts_end"] or hi_ts, marker["ts_start"] + 7 * 86400, hi_ts)
        elapsed = max(0, (after_end - marker["ts_start"]) / 86400)
        overlap = any(
            index != other_index
            and other["ts_start"] < after_end
            and (other["ts_end"] or hi_ts) > marker["ts_start"]
            for other_index, other in enumerate(markers)
        )
        marker_notes.append(
            f"{marker['category'] or 'marker'}: after窓 {f'暫定（{int(elapsed)}日経過）' if elapsed < 7 else '7日経過'}{'・交絡あり' if overlap else ''}"
        )
    summary_start = window.end - dt.timedelta(days=27)
    summary_lo = f"{summary_start} 00:00:00"
    raw_params = [summary_lo, hi]
    raw_project = ""
    if window.project is not None:
        raw_project = " and s.project = ?"
        raw_params.append(window.project)
    raw = conn.execute(
        f"""select date(r.ts,'unixepoch','localtime') day,r.ts,r.session_id,r.prompt_id,r.agent_id,r.model,s.project,r.is_interrupted,r.cost_usd,
    coalesce(r.input_tok,0)+coalesce(r.output_tok,0)+coalesce(r.cache_read_tok,0)+coalesce(r.cache_w5m_tok,0)+coalesce(r.cache_w1h_tok,0) tokens,
    coalesce(r.input_tok,0)+coalesce(r.cache_read_tok,0)+coalesce(r.cache_w5m_tok,0)+coalesce(r.cache_w1h_tok,0) context
    from v_request_cost r join session s using(session_id) where datetime(r.ts,'unixepoch','localtime')>=? and datetime(r.ts,'unixepoch','localtime')<?{raw_project}""",
        raw_params,
    ).fetchall()

    def new_bucket():
        return {
            "total": 0.0,
            "days": set(),
            "project": defaultdict(float),
            "delegated": 0.0,
            "premium": 0.0,
            "unknown": 0,
            "prompt": defaultdict(
                lambda: {
                    "cost": 0.0,
                    "peak": 0,
                    "req": 0,
                    "intr": 0,
                    "project": None,
                    "first": None,
                    "last": None,
                }
            ),
            "interactive_cost": 0.0,
            "interactive_tokens": 0,
            "gap": 0,
            "compaction": 0,
        }

    buckets = {grain: defaultdict(new_bucket) for grain in ("daily", "weekly", "monthly")}
    day_projects = defaultdict(lambda: defaultdict(float))

    def keys(day):
        return {"daily": day, "weekly": _monday(day), "monthly": _month(day)}

    for row in raw:
        day = dt.date.fromisoformat(row["day"])
        cost = row["cost_usd"] or 0
        day_projects[day][row["project"]] += cost
        for grain, key in keys(day).items():
            item = buckets[grain][key]
            item["total"] += cost
            item["days"].add(day)
            item["project"][row["project"]] += cost
            item["delegated"] += cost if row["agent_id"] is not None else 0
            item["premium"] += cost if _model(row["model"]) in ("fable", "opus") else 0
            item["unknown"] += row["cost_usd"] is None
            if not _auto(row["project"]):
                item["interactive_cost"] += cost
                item["interactive_tokens"] += row["tokens"]
            if row["prompt_id"] is not None:
                prompt = item["prompt"][row["prompt_id"]]
                prompt["cost"] += cost
                prompt["req"] += 1
                prompt["intr"] = max(prompt["intr"], row["is_interrupted"])
                prompt["project"] = row["project"]
                prompt["first"] = (
                    row["ts"] if prompt["first"] is None else min(prompt["first"], row["ts"])
                )
                prompt["last"] = (
                    row["ts"] if prompt["last"] is None else max(prompt["last"], row["ts"])
                )
                if row["agent_id"] is None:
                    prompt["peak"] = max(prompt["peak"], row["context"])

    def add_event(day, name):
        for grain, key in keys(day).items():
            buckets[grain][key][name] += 1

    gap_sql = "select ts from (select r.ts,r.session_id,r.agent_id,s.project,lag(r.ts) over(partition by r.session_id order by r.ts) prev from v_request_cost r left join session s using(session_id) where r.agent_id is null) where ts>=? and ts<? and ts-prev>3600"
    gap_params = [dt.datetime.fromisoformat(summary_lo).timestamp(), hi_ts]
    if window.project is not None:
        gap_sql = gap_sql.replace(
            "where r.agent_id is null", "where r.agent_id is null and s.project = ?"
        )
        gap_params.insert(0, window.project)
    for row in conn.execute(gap_sql, gap_params):
        add_event(dt.datetime.fromtimestamp(row["ts"]).date(), "gap")
    comp_sql = "select ci.ts from v_cache_identity ci"
    comp_params = []
    if window.project is not None:
        comp_sql += " join session s using(session_id)"
    comp_sql += " where ci.cause='compaction' and ci.ts>=? and ci.ts<?"
    comp_params.extend([dt.datetime.fromisoformat(summary_lo).timestamp(), hi_ts])
    if window.project is not None:
        comp_sql += " and s.project = ?"
        comp_params.append(window.project)
    for row in conn.execute(comp_sql, comp_params):
        add_event(dt.datetime.fromtimestamp(row["ts"]).date(), "compaction")
    unit_labels = {
        "daily": days,
        "weekly": sorted(key for key in buckets["weekly"] if buckets["weekly"][key]["total"])[-4:],
        "monthly": sorted(key for key in buckets["monthly"] if buckets["monthly"][key]["total"]),
    }
    distribution_colors = {"平均": "#facc15", "p50": "#34d399", "p90": "#7aa2f7", "p95": "#f472b6"}
    context_colors = {"p50": "#34d399", "p90": "#fb923c"}
    span_colors = {"p50": "#34d399", "p90": "#7aa2f7", "p95": "#f472b6"}
    event_colors = {"gap>1h再開": "#7aa2f7", "中断プロンプト": "#f87171", "compaction": "#fb923c"}

    def series_for(grain):
        labels = unit_labels[grain]
        cost = {key: [] for key in ("平均", "p50", "p90", "p95")}
        context = {key: [] for key in ("p50", "p90")}
        span = {key: [] for key in ("p50", "p90", "p95")}
        events = {key: [] for key in event_colors}
        for key in labels:
            item = buckets[grain][key]
            interactive = [
                prompt for prompt in item["prompt"].values() if not _auto(prompt["project"])
            ]
            costs = [prompt["cost"] for prompt in interactive]
            contexts = [prompt["peak"] / 1000 for prompt in interactive if prompt["peak"]]
            spans = [(prompt["last"] - prompt["first"]) / 60 for prompt in interactive]
            stable = grain != "daily" or len(interactive) >= 20
            cost["平均"].append(sum(costs) / len(costs) if stable and costs else None)
            for name, quantile in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95)):
                cost[name].append(_pct(costs, quantile) if stable else None)
            for name, quantile in (("p50", 0.5), ("p90", 0.9)):
                context[name].append(_pct(contexts, quantile) if stable else None)
            for name, quantile in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95)):
                span[name].append(_pct(spans, quantile) if stable else None)
            events["gap>1h再開"].append(item["gap"])
            events["中断プロンプト"].append(
                sum(prompt["intr"] for prompt in item["prompt"].values())
            )
            events["compaction"].append(item["compaction"])
        return labels, cost, context, span, events

    def previous_key(key, grain):
        return (
            key - dt.timedelta(days=1)
            if grain == "daily"
            else key - dt.timedelta(days=7)
            if grain == "weekly"
            else _prev_month(key)
        )

    def elapsed(key, grain):
        if grain == "daily":
            return 1
        if grain == "weekly":
            return (
                min(7, (window.end - key).days + 1)
                if key <= window.end <= key + dt.timedelta(days=6)
                else 7
            )
        return (
            min(calendar.monthrange(key.year, key.month)[1], window.end.day)
            if key.year == window.end.year and key.month == window.end.month
            else calendar.monthrange(key.year, key.month)[1]
        )

    def scoped(key, grain, duration):
        output = defaultdict(float)
        for day in [key + dt.timedelta(days=index) for index in range(duration)]:
            for project, value in day_projects[day].items():
                output[project] += value
        return output

    def metrics(grain):
        output = []
        today = dt.date.today()
        for key in unit_labels[grain]:
            item = buckets[grain][key]
            duration = elapsed(key, grain)
            previous = previous_key(key, grain)
            current_projects = scoped(key, grain, duration)
            previous_projects = scoped(previous, grain, duration)
            current = sum(current_projects.values())
            old = sum(previous_projects.values())
            comparable = all(
                previous + dt.timedelta(days=index) >= measured for index in range(duration)
            )
            if grain == "daily" and (key >= today or previous >= today):
                comparable = False
            change = f"{(current / old - 1) * 100:+.1f}%" if comparable and old else "—"
            delta = current - old
            differences = {
                project: current_projects.get(project, 0) - previous_projects.get(project, 0)
                for project in set(current_projects) | set(previous_projects)
            }
            same = [(project, value) for project, value in differences.items() if value * delta > 0]
            contribution = max(same, key=lambda value: abs(value[1])) if same else ("—", 0)
            direction = "増加" if delta >= 0 else "減少"
            contribution_text = (
                f"最大{direction}寄与 {ui.project_name(contribution[0])} {contribution[1]:+.2f}$"
            )
            interactive = [
                prompt for prompt in item["prompt"].values() if not _auto(prompt["project"])
            ]
            prompt_cost = sum(prompt["cost"] for prompt in item["prompt"].values())
            over = sum(
                prompt["cost"] for prompt in item["prompt"].values() if prompt["peak"] >= 200000
            )
            blended = (
                item["interactive_cost"] / item["interactive_tokens"] * 1e6
                if item["interactive_tokens"]
                else 0
            )
            denominator = (
                1
                if grain == "daily"
                else 7
                if grain == "weekly"
                else calendar.monthrange(key.year, key.month)[1]
            )
            label = (
                str(key)
                if grain == "daily"
                else f"{key} — {key + dt.timedelta(days=6)}"
                if grain == "weekly"
                else key.strftime("%Y-%m")
            )
            progress = "（進行中）" if grain == "daily" and key == today else ""
            warning = " ⚠" if item["unknown"] else ""
            period_end = (
                key
                if grain == "daily"
                else key + dt.timedelta(days=6)
                if grain == "weekly"
                else key.replace(day=calendar.monthrange(key.year, key.month)[1])
            )
            command = f"metsuke view period --from {key} --to {period_end}"
            if comparable and contribution[0] != "—":
                command += f" --project {contribution[0]}"
            output.append(
                {
                    "key": key,
                    "label": label + progress,
                    "total": item["total"],
                    "change": change + warning,
                    "days": f"{len(item['days'])}/{denominator}",
                    "contribution": contribution_text,
                    "command": command,
                    "interactive_n": len(interactive),
                    "delegated": item["delegated"] / item["total"] * 100 if item["total"] else 0,
                    "premium": item["premium"] / item["total"] * 100 if item["total"] else 0,
                    "over200": over / prompt_cost * 100 if prompt_cost else 0,
                    "blended": blended,
                    "high_req": sum(prompt["req"] >= 20 for prompt in interactive),
                }
            )
        return output

    def summary_panel(grain):
        values = metrics(grain)
        labels = [value["key"] for value in values]
        compare = {"daily": "前日比", "weekly": "前週比", "monthly": "前月比"}[grain]
        table_rows = []
        for value in values:
            table_rows.append(
                [
                    Cell(value["label"], "left"),
                    Cell(ui.money(value["total"])),
                    Cell(value["change"]),
                    Cell(value["days"]),
                    Cell(
                        "",
                        "left",
                        content=ui.clip(
                            value["contribution"],
                            max_width=170,
                            title=value["contribution"],
                        ),
                    ),
                    Cell(str(value["interactive_n"])),
                    Cell(f"{value['delegated']:.1f}%"),
                    Cell(f"{value['premium']:.1f}%"),
                    Cell(f"{value['over200']:.1f}%"),
                    Cell(ui.money(value["blended"])),
                    Cell(str(value["high_req"])),
                    Cell("", "left", content=ui.code(value["command"])),
                ]
            )
        rate = {
            "委任費率": [value["delegated"] for value in values],
            "fable+opus費率": [value["premium"] for value in values],
            "≥200k費率": [value["over200"] for value in values],
        }
        counts = {
            "対話n": [value["interactive_n"] for value in values],
            "高reqプロンプト数": [value["high_req"] for value in values],
        }
        blended = {"blended$(対話)": [value["blended"] for value in values]}
        rate_colors = {"委任費率": "#7aa2f7", "fable+opus費率": "#f472b6", "≥200k費率": "#fb923c"}
        count_colors = {"対話n": "#34d399", "高reqプロンプト数": "#facc15"}
        blend_colors = {"blended$(対話)": "#a78bfa"}
        return ui.join(
            ui.legend([(key, rate_colors[key]) for key in rate]),
            ui.card(
                ui.line_chart(
                    labels, rate, rate_colors, "%", grain=grain, fixed_top=100, precision=1
                )
            ),
            ui.legend([(key, count_colors[key]) for key in counts]),
            ui.card(ui.line_chart(labels, counts, count_colors, "件", grain=grain)),
            ui.legend([(key, blend_colors[key]) for key in blended]),
            ui.card(
                ui.line_chart(labels, blended, blend_colors, "", money_axis=True, grain=grain)
            ),
            ui.table(
                [
                    Column("期間", "left"),
                    Column("合計"),
                    Column(compare),
                    Column("利用日数"),
                    Column("最大寄与project", "left"),
                    Column("対話n"),
                    Column("委任費率"),
                    Column("fable+opus費率"),
                    Column("≥200k費率"),
                    Column("blended$(対話)"),
                    Column("高req"),
                    Column("command", "left"),
                ],
                table_rows,
            ),
        )

    palette = ["#7aa2f7", "#a78bfa", "#34d399", "#f59e0b", "#f472b6", "#38bdf8", "#6b7280"]
    project_colors = {key: palette[index] for index, key in enumerate(projects)}
    axes = {"fee": (fee, COLORS), "model": (models, COLORS), "project": (projects, project_colors)}

    def axis_panel(panel_id):
        base, colors = axes[panel_id]
        parts = []
        for grain in ("daily", "weekly", "monthly"):
            data = aggregate(base, grain)
            legend_items = [(key, colors[key]) for key in data] + (
                [("7日移動平均", "#fff")] if grain == "daily" else []
            )
            parts.append(
                ui.grain_panel(
                    grain,
                    ui.join(
                        ui.legend(legend_items),
                        ui.card(
                            ui.volume_chart(
                                grain_labels[grain],
                                data,
                                colors,
                                moving if grain == "daily" else None,
                                grain,
                                lo_ts,
                                hi_ts,
                                markers,
                                regimes,
                            )
                        ),
                    ),
                    active=grain == "daily",
                )
            )
        return ui.panel("v2-axis", panel_id, ui.join(*parts), active=panel_id == "fee")

    def distribution_panel(grain):
        labels, cost, context, span, _ = series_for(grain)
        guard = (
            "n<20の日は非表示（線も中断）。"
            if grain == "daily"
            else "データ蓄積とともに有効。"
            if grain == "monthly"
            else ""
        )
        return ui.join(
            ui.legend([(key, distribution_colors[key]) for key in cost]),
            ui.card(
                ui.line_chart(
                    labels, cost, distribution_colors, "", money_axis=True, grain=grain
                )
            ),
            ui.legend([(key, context_colors[key]) for key in context]),
            ui.card(ui.line_chart(labels, context, context_colors, "k", grain=grain)),
            ui.heading(2, "実行時間（観測スパン）"),
            ui.legend([(key, span_colors[key]) for key in span]),
            ui.card(
                ui.line_chart(labels, span, span_colors, "分", grain=grain, precision=1)
            ),
            ui.text_block(guard + "対話のみ判別は暫定project名分類。", cls="dim"),
        )

    def events_panel(grain):
        labels, _, _, _, events = series_for(grain)
        return ui.join(
            ui.legend([(key, event_colors[key]) for key in events]),
            ui.card(ui.line_chart(labels, events, event_colors, "件", grain=grain)),
        )

    this_monday = _monday(window.end)
    item = buckets["weekly"][this_monday]
    duration = elapsed(this_monday, "weekly")
    previous = this_monday - dt.timedelta(days=7)
    current_projects = scoped(this_monday, "weekly", duration)
    previous_projects = scoped(previous, "weekly", duration)
    current = sum(current_projects.values())
    old = sum(previous_projects.values())
    delta = current - old
    differences = {
        project: current_projects.get(project, 0) - previous_projects.get(project, 0)
        for project in set(current_projects) | set(previous_projects)
    }
    same = [(project, value) for project, value in differences.items() if value * delta > 0]
    contribution = max(same, key=lambda value: abs(value[1])) if same else ("—", 0)
    change = "比較不可" if not old else f"{(current / old - 1) * 100:+.1f}%"
    total_section = ui.join(
        ui.heading(2, "① 総量"),
        ui.tabs(
            "v2-axis",
            [("fee", "費目", True), ("model", "モデル", False), ("project", "プロジェクト", False)],
        ),
        *(axis_panel(key) for key in ("fee", "model", "project")),
    )
    distribution_section = ui.join(
        ui.heading(2, "② 分布形の推移（対話のみ）"),
        *(
            ui.grain_panel(grain, distribution_panel(grain), active=grain == "daily")
            for grain in ("daily", "weekly", "monthly")
        ),
    )
    events_section = ui.join(
        ui.heading(2, "③ 行動イベントの推移"),
        *(
            ui.grain_panel(grain, events_panel(grain), active=grain == "daily")
            for grain in ("daily", "weekly", "monthly")
        ),
    )
    summary_section = ui.join(
        ui.heading(2, "④ 挙動サマリ（グラフ＋表）"),
        *(
            ui.grain_panel(grain, summary_panel(grain), active=grain == "daily")
            for grain in ("daily", "weekly", "monthly")
        ),
    )
    note = f"観測日以後の欠行は$0補完（取込停止との区別は metsuke doctor）。marker={len(markers)}件、regime_event={len(regimes)}件。{' / '.join(marker_notes)}"
    body = ui.join(
        ui.insight(
            f"今週 {ui.money(current)}（先週同時点比 {change}） / 最大{'増加' if delta >= 0 else '減少'}寄与: {ui.project_name(contribution[0])} {contribution[1]:+.2f}$"
        ),
        ui.grain_tabs(
            [
                ("daily", "日次", True, None),
                ("weekly", "週次", False, None),
                ("monthly", "月次", False, "データ蓄積とともに有効"),
            ]
        ),
        total_section,
        ui.text_block(note, cls="dim"),
        distribution_section,
        events_section,
        summary_section,
    )
    title, period, total = (
        "metsuke · 推移ビュー",
        f"{window.label} · 計測開始 {measured}",
        ui.plain(f"{ui.money(sum(daily_total))} · 日次/週次/月次コスト推移"),
    )
    return LegacyViewModel(title, period, total, body, ui.local_timezone())
