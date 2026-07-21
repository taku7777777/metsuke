from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from . import common as ui
from .common import (
    Cell,
    Column,
    LegacyViewModel,
    Page,
    Window,
    json_real_sql,
    restore_json_reals,
    scoped_requests_cte,
    window_totals_from_row,
    window_totals_sql,
)
from .prompt import (
    dominant_component_name,
    dominant_component_names,
    dominant_component_sql,
)

PeriodModel = LegacyViewModel

COLORS = {
    "cache_read": "#2dd4bf",
    "cache_creation": "#facc15",
    "output": "#f472b6",
    "input": "#94a3b8",
    "server_tool": "#a78bfa",
}


def _duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _columns(values):
    return [Column(label, "left" if left else "", sortable) for label, left, sortable in values]


def _known_sum(rows) -> float:
    return sum(row["cost"] for row in rows if row["cost"] is not None)


def _maximum_cost(rows) -> float:
    return next((row["cost"] for row in rows if row["cost"] is not None), 0)


def _cost_cell(value, maximum) -> Cell:
    return Cell(
        "—" if value is None else ui.money(value),
        sort=-1 if value is None else value,
        bar=0 if value is None or not maximum else value / maximum,
    )


def _dashboard_page_clause(page: Page | None, tie_break: str) -> tuple[str, list[int]]:
    if page is None:
        return "", []
    if page.sort != "cost":
        raise ValueError("period currently supports cost sorting only")
    direction = "desc" if page.order == "desc" else "asc"
    return f" order by cost {direction},{tie_break} limit ? offset ?", [page.limit, page.offset]


def query(conn, window: Window, page: Page | None = None) -> PeriodModel:
    scoped_cte, params = scoped_requests_cte(window)
    dominant_columns = dominant_component_sql("r")
    prompt_page_sql, prompt_page_params = _dashboard_page_clause(page, "r.prompt_id asc")
    session_page_sql, session_page_params = _dashboard_page_clause(page, "r.session_id desc")
    project_page_sql, project_page_params = _dashboard_page_clause(page, "r.project asc")
    prompt_order = (
        prompt_page_sql
        if page is not None
        else " order by cost desc,prompt_id asc limit 40"
    )
    session_order = (
        session_page_sql
        if page is not None
        else " order by cost desc,session_id desc limit 30"
    )
    project_order = project_page_sql if page is not None else " order by cost desc"
    component_names = dominant_component_names()
    component_json = ",".join(
        f"'dominant_{name}',{json_real_sql(f'dominant_{name}')}"
        for name in component_names
    )
    rows = conn.execute(
        f"""WITH {scoped_cte},
        prompt_agg AS MATERIALIZED (
            SELECT r.prompt_id,MIN(r.ts) ts,SUM(r.cost_usd) cost,COUNT(*) nr,
              COUNT(DISTINCT r.agent_id) na,MAX(r.is_interrupted) intr,
              r.scoped_project project,p.text,MAX(r.ts)-MIN(r.ts) dur,{dominant_columns}
            FROM scoped r LEFT JOIN prompt p USING(prompt_id)
            WHERE r.prompt_id IS NOT NULL AND r.scoped_session_id IS NOT NULL
            GROUP BY r.prompt_id
        ),
        prompt_cost_agg AS MATERIALIZED (
            SELECT r.prompt_id,COALESCE(SUM(r.cost_usd),0) cost,
              MAX(r.is_interrupted) intr
            FROM scoped r WHERE r.prompt_id IS NOT NULL GROUP BY r.prompt_id
        ),
        prompt_rank AS MATERIALIZED (
            SELECT * FROM prompt_agg r{prompt_order}
        ),
        session_agg AS MATERIALIZED (
            SELECT r.session_id,SUM(r.cost_usd) cost,MIN(r.ts) first_ts,
              MAX(r.ts)-MIN(r.ts) dur,r.scoped_project project,
              COUNT(DISTINCT r.prompt_id) np,COUNT(*) nr
            FROM scoped r WHERE r.scoped_session_id IS NOT NULL GROUP BY r.session_id
        ),
        session_rank AS MATERIALIZED (
            SELECT * FROM session_agg r{session_order}
        ),
        peak_agg AS MATERIALIZED (
            SELECT r.session_id,r.prompt_id,r.scoped_project project,
              MAX(COALESCE(r.input_tok,0)+COALESCE(r.cache_read_tok,0)
                  +COALESCE(r.cache_w5m_tok,0)+COALESCE(r.cache_w1h_tok,0)) peak
            FROM scoped r WHERE r.scoped_session_id IS NOT NULL
              AND r.agent_id IS NULL AND r.prompt_id IS NOT NULL
            GROUP BY r.session_id,r.prompt_id
        ),
        project_agg AS MATERIALIZED (
            SELECT r.scoped_project project,SUM(r.cost_usd) cost,
              COUNT(DISTINCT r.session_id) ns,COUNT(DISTINCT r.prompt_id) np,COUNT(*) nr
            FROM scoped r WHERE r.scoped_session_id IS NOT NULL GROUP BY r.scoped_project
        ),
        project_rank AS MATERIALIZED (
            SELECT * FROM project_agg r{project_order}
        )
        SELECT 'totals',json_object(
          'cost_usd',{json_real_sql('cost_usd')},'request_count',request_count,
          'session_count',session_count,'project_count',project_count,
          'unknown_cost_request_count',unknown_cost_request_count)
        FROM (SELECT {window_totals_sql()} FROM scoped r)
        UNION ALL
        SELECT 'prompt',json_object(
          'prompt_id',prompt_id,'ts',{json_real_sql('ts')},
          'cost',{json_real_sql('cost')},'nr',nr,'na',na,'intr',intr,
          'project',project,'text',text,'dur',{json_real_sql('dur')},{component_json})
          FROM prompt_rank
        UNION ALL
        SELECT 'prompt_cost',json_object(
          'cost',{json_real_sql('COALESCE(cost,0)')},'intr',intr)
          FROM prompt_cost_agg
        UNION ALL
        SELECT 'session',json_object(
          'session_id',session_id,'cost',{json_real_sql('cost')},
          'first_ts',{json_real_sql('first_ts')},'dur',{json_real_sql('dur')},
          'project',project,'np',np,'nr',nr) FROM session_rank
        UNION ALL
        SELECT 'session_cost',json_object(
          'cost',{json_real_sql('COALESCE(cost,0)')}) FROM session_agg
        UNION ALL
        SELECT 'peak',json_object(
          'session_id',session_id,'prompt_id',prompt_id,'project',project,'peak',peak)
          FROM peak_agg
        UNION ALL
        SELECT 'project',json_object(
          'project',project,'cost',{json_real_sql('cost')},
          'ns',ns,'np',np,'nr',nr) FROM project_rank""",
        [
            *params,
            *prompt_page_params,
            *session_page_params,
            *project_page_params,
        ],
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for kind, payload in rows:
        grouped.setdefault(kind, []).append(json.loads(payload))
    restore_json_reals(grouped["totals"], "cost_usd")
    restore_json_reals(
        grouped.get("prompt", []),
        "ts",
        "cost",
        "dur",
        *(f"dominant_{name}" for name in component_names),
    )
    restore_json_reals(grouped.get("prompt_cost", []), "cost")
    restore_json_reals(grouped.get("session", []), "cost", "first_ts", "dur")
    restore_json_reals(grouped.get("session_cost", []), "cost")
    restore_json_reals(grouped.get("project", []), "cost")
    totals = window_totals_from_row(grouped["totals"][0])
    prompts = grouped.get("prompt", [])
    all_prompt_rows = grouped.get("prompt_cost", [])
    all_prompt_costs = [row["cost"] for row in all_prompt_rows]
    sessions = grouped.get("session", [])
    all_session_costs = [row["cost"] for row in grouped.get("session_cost", [])]
    peak_values = defaultdict(list)
    project_peaks = defaultdict(list)
    for row in grouped.get("peak", []):
        peak_values[row["session_id"]].append(row["peak"])
        project_peaks[row["project"]].append(row["peak"])
    average_peak = {key: sum(values) / len(values) for key, values in peak_values.items()}
    average_project_peak = {key: sum(values) / len(values) for key, values in project_peaks.items()}
    projects = grouped.get("project", [])

    names = component_names
    attributed = sum(all_prompt_costs)
    maximum = _maximum_cost(prompts)
    running = 0
    prompt_rows = []
    first_rank = 1 if page is None else page.offset + 1
    for index, row in enumerate(prompts, first_rank):
        if row["cost"] is not None:
            running += row["cost"]
        dominant = dominant_component_name(
            {name: row[f"dominant_{name}"] for name in names}
        )
        full = (row["text"] or "").replace("\n", " ")
        text = full[:60] + ("…" if len(full) > 60 else "")
        cumulative = running / attributed * 100 if attributed else 0
        shown_project = ui.project_name(row["project"])
        prompt_rows.append(
            [
                Cell(str(index), sort=index),
                _cost_cell(row["cost"], maximum),
                Cell(f"{cumulative:.1f}%", sort=cumulative),
                Cell(dt.datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M"), sort=row["ts"]),
                Cell(_duration(row["dur"]), sort=row["dur"]),
                Cell(shown_project, "left", sort=shown_project, clip="project-clip"),
                Cell(
                    text, "left", sort=text, title=full, clip="prompt-clip", warn=bool(row["intr"])
                ),
                Cell(str(row["nr"]), sort=row["nr"]),
                Cell(str(row["na"]), sort=row["na"]),
                Cell("", sort=dominant, title=dominant, dot=COLORS[dominant]),
                Cell("", "left", content=ui.code(f"metsuke explain {row['prompt_id'][:8]} --html")),
            ]
        )
    prompt_top = _known_sum(prompts[:10])
    prompt_max = next((row for row in prompts if row["cost"] is not None), None)
    interrupted_rows = [row for row in all_prompt_rows if row["intr"] == 1]
    interrupted = (sum(row["cost"] for row in interrupted_rows), len(interrupted_rows))
    unattributed = max(0, totals.cost_usd - attributed)
    prompt_insight = ui.join(
        ui.plain(
            f"上位10件で {ui.money(prompt_top)}（帰属済み総額の {prompt_top / attributed * 100 if attributed else 0:.1f}%） / 最大 {ui.money(prompt_max['cost'] if prompt_max else 0)}（{ui.project_name(prompt_max['project']) if prompt_max else '—'}） / 中断プロンプト支出 {ui.money(interrupted[0])}（{interrupted[1]}件） / 帰属不能 {ui.money(unattributed)}（{unattributed / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}%） · "
        ),
        ui.code("metsuke view dist"),
    )
    prompt_body = ui.join(
        ui.insight_body(prompt_insight),
        ui.legend([(name, COLORS[name]) for name in names]),
        ui.table(
            _columns(
                [
                    ("順位", 0, 1),
                    ("コスト", 0, 1),
                    ("累積", 0, 1),
                    ("日時", 0, 1),
                    ("期間", 0, 1),
                    ("project", 1, 1),
                    ("prompt", 1, 1),
                    ("req", 0, 1),
                    ("agent", 0, 1),
                    ("費目", 0, 1),
                    ("command", 1, 0),
                ]
            ),
            prompt_rows,
        ),
    )

    maximum = _maximum_cost(sessions)
    running = 0
    session_rows = []
    for index, row in enumerate(sessions, first_rank):
        if row["cost"] is not None:
            running += row["cost"]
        unit = row["cost"] / row["np"] if row["cost"] is not None and row["np"] else None
        peak = average_peak.get(row["session_id"])
        cumulative = running / totals.cost_usd * 100 if totals.cost_usd else 0
        shown_project = ui.project_name(row["project"])
        session_rows.append(
            [
                Cell(str(index), sort=index),
                _cost_cell(row["cost"], maximum),
                Cell(f"{cumulative:.1f}%", sort=cumulative),
                Cell(
                    dt.datetime.fromtimestamp(row["first_ts"]).strftime("%m-%d %H:%M"),
                    sort=row["first_ts"],
                ),
                Cell(_duration(row["dur"]), sort=row["dur"]),
                Cell(shown_project, "left", sort=shown_project, clip="project-clip"),
                Cell(str(row["np"]), sort=row["np"]),
                Cell(str(row["nr"]), sort=row["nr"]),
                Cell(
                    ui.money(unit) if unit is not None else "—",
                    sort=unit if unit is not None else -1,
                ),
                Cell(
                    f"{peak / 1000:,.0f}k" if peak is not None else "—",
                    sort=peak if peak is not None else -1,
                ),
                Cell("", "left", content=ui.code(f"metsuke trace {row['session_id'][:8]} --html")),
            ]
        )
    top_sessions = _known_sum(sessions[:10])
    over_one = [value for value in all_session_costs if value >= 1]
    session_body = ui.join(
        ui.insight(
            f"上位10件で全request総額の {top_sessions / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}% / $1以上は {len(over_one)}件（全{len(all_session_costs)}中）で {sum(over_one) / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}%"
        ),
        ui.table(
            _columns(
                [
                    ("順位", 0, 1),
                    ("コスト", 0, 1),
                    ("累積", 0, 1),
                    ("開始", 0, 1),
                    ("期間", 0, 1),
                    ("project", 1, 1),
                    ("prompts", 0, 1),
                    ("req", 0, 1),
                    ("$/prompt", 0, 1),
                    ("平均ピークctx", 0, 1),
                    ("command", 1, 0),
                ]
            ),
            session_rows,
        ),
    )

    maximum = _maximum_cost(projects)
    shown = projects if page is not None else projects[:20]
    rest = [] if page is not None else projects[20:]
    project_rows = []
    for row in shown:
        command = f"metsuke view period --from {window.start} --to {window.end} --project {row['project'] or ''}"
        unit = row["cost"] / row["np"] if row["cost"] is not None and row["np"] else None
        peak = average_project_peak.get(row["project"])
        shown_project = ui.project_name(row["project"])
        project_rows.append(
            [
                Cell(shown_project, "left", sort=shown_project, clip="project-clip"),
                _cost_cell(row["cost"], maximum),
                Cell(
                    "—"
                    if row["cost"] is None
                    else f"{row['cost'] / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}%",
                    sort=(
                        row["cost"] / totals.cost_usd
                        if row["cost"] is not None and totals.cost_usd
                        else -1
                    ),
                ),
                Cell(str(row["ns"]), sort=row["ns"]),
                Cell(str(row["np"]), sort=row["np"]),
                Cell(str(row["nr"]), sort=row["nr"]),
                Cell(
                    ui.money(unit) if unit is not None else "—",
                    sort=unit if unit is not None else -1,
                ),
                Cell(
                    f"{peak / 1000:,.0f}k" if peak is not None else "—",
                    sort=peak if peak is not None else -1,
                ),
                Cell("", "left", title=command, content=ui.code(command)),
            ]
        )
    if rest:
        rest_cost = _known_sum(rest)
        rest_prompts = sum(row["np"] for row in rest)
        peaks = [value for row in rest for value in project_peaks.get(row["project"], [])]
        peak = sum(peaks) / len(peaks) if peaks else None
        label = f"その他 ({len(rest)} projects)"
        project_rows.append(
            [
                Cell(label, "left", sort=label, clip="project-clip"),
                Cell(
                    ui.money(rest_cost),
                    sort=rest_cost,
                    bar=0 if not maximum else rest_cost / maximum,
                ),
                Cell(
                    f"{rest_cost / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}%",
                    sort=rest_cost / totals.cost_usd if totals.cost_usd else 0,
                ),
                Cell(str(sum(row["ns"] for row in rest)), sort=sum(row["ns"] for row in rest)),
                Cell(str(rest_prompts), sort=rest_prompts),
                Cell(str(sum(row["nr"] for row in rest)), sort=sum(row["nr"] for row in rest)),
                Cell(
                    ui.money(rest_cost / rest_prompts) if rest_prompts else "—",
                    sort=rest_cost / rest_prompts if rest_prompts else -1,
                ),
                Cell(
                    f"{peak / 1000:,.0f}k" if peak is not None else "—",
                    sort=peak if peak is not None else -1,
                ),
                Cell("—"),
            ]
        )
    project_top = _known_sum(projects[:3])
    under_one = sum(row["cost"] < 1 for row in projects if row["cost"] is not None)
    project_body = ui.join(
        ui.insight(
            f"上位3件で期間全体の {project_top / totals.cost_usd * 100 if totals.cost_usd else 0:.1f}% / 全{len(projects)}プロジェクト中 $1未満が {under_one}件"
        ),
        ui.table(
            _columns(
                [
                    ("project", 1, 1),
                    ("コスト", 0, 1),
                    ("share", 0, 1),
                    ("sessions", 0, 1),
                    ("prompts", 0, 1),
                    ("req", 0, 1),
                    ("$/prompt", 0, 1),
                    ("平均ピークctx", 0, 1),
                    ("command", 1, 0),
                ]
            ),
            project_rows,
        ),
    )
    orientation = ui.join(
        ui.code(f"metsuke trace {'<'}session>"),
        ui.plain("=セッション全体の背景（fan-out・時間構造）を見る / "),
        ui.code(f"metsuke explain {'<'}prompt>"),
        ui.plain("=1プロンプトのコスト内訳を見る"),
    )
    body = ui.join(
        ui.tabs(
            "v1",
            [("s", "セッション", True), ("p", "プロンプト", False), ("j", "プロジェクト", False)],
        ),
        ui.block(orientation, cls="dim"),
        ui.panel("v1", "s", session_body, active=True),
        ui.panel("v1", "p", prompt_body, active=False),
        ui.panel("v1", "j", project_body, active=False),
        ui.text_block(
            "対話のみ判別は暫定: projectに sandbox-experiments を含む、または -private-tmp- で始まるものを自動実行として扱う。",
            cls="dim",
        ),
    )
    total_html = ui.plain(
        f"{ui.money(totals.cost_usd)} · {totals.request_count:,} requests · {totals.session_count} sessions · {totals.project_count} projects"
    )
    if totals.unknown_cost_request_count:
        total_html = ui.join(
            total_html,
            ui.plain(" · "),
            ui.warning(f"未知価格 {totals.unknown_cost_request_count} requests"),
        )
    return LegacyViewModel(
        "metsuke · 期間ビュー", window.label, total_html, body, ui.local_timezone()
    )
