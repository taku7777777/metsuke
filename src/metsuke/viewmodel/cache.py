from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from . import common as ui
from .common import Cell, Column, LegacyViewModel, Row, Window

CacheModel = LegacyViewModel

COLORS = {"cache_read": "#2dd4bf", "cache_w5m": "#facc15", "cache_w1h": "#fb923c"}
CAUSE = {
    "interruption": "#f87171",
    "compaction": "#fb923c",
    "model_switch": "#a78bfa",
    "config_change": "#38bdf8",
    "ttl_expiry": "#facc15",
    "unknown": "#6b7280",
}


def _model_group(value):
    lowered = (value or "").lower()
    return next((name for name in ("fable", "opus", "sonnet", "haiku") if name in lowered), "other")


def _columns(values):
    return [Column(label, "left" if left else "", sortable) for label, left, sortable in values]


def query(conn, window: Window) -> CacheModel:
    days = [
        window.start + dt.timedelta(days=index)
        for index in range((window.end - window.start).days + 1)
    ]
    lo, hi = window.sql_bounds()
    indexes = {str(day): index for index, day in enumerate(days)}
    count = len(days)
    factor = "(case when r.service_tier='batch' then p.batch_x else 1 end)*(case when r.speed='fast' then p.fast_x else 1 end)*(case when r.geo like 'us%' then p.geo_us_x else 1 end)"
    project_clause = ""
    params = [lo, hi]
    if window.project is not None:
        project_clause = " and s.project = ?"
        params.append(window.project)
    events = conn.execute(
        f"""select ci.ts,date(ci.ts,'unixepoch','localtime') day,ci.session_id,ci.request_id,ci.cause,ci.gap,ci.gap_seconds,s.project,
    r.cache_write_usd rebuild
    from v_cache_identity ci join v_request_cost r using(request_id) join session s on s.session_id=ci.session_id
    where datetime(ci.ts,'unixepoch','localtime')>=? and datetime(ci.ts,'unixepoch','localtime')<?{project_clause}""",
        params,
    ).fetchall()
    loss_idle = {key: [0] * count for key in CAUSE}
    loss_active = {key: [0] * count for key in CAUSE}
    growth = {key: [0] * count for key in CAUSE}
    cost_idle = {key: [0.0] * count for key in CAUSE}
    cost_active = {key: [0.0] * count for key in CAUSE}
    cost_growth = {key: [0.0] * count for key in CAUSE}
    amounts = Counter()
    classified = 0
    for row in events:
        cause = row["cause"] if row["cause"] in CAUSE else "unknown"
        if (row["gap"] or 0) > 0:
            bucket, cost_bucket = growth, cost_growth
        elif row["gap_seconds"] is not None and row["gap_seconds"] >= 300:
            bucket, cost_bucket = loss_idle, cost_idle
        else:
            bucket, cost_bucket = loss_active, cost_active
        bucket[cause][indexes[row["day"]]] += 1
        cost_bucket[cause][indexes[row["day"]]] += row["rebuild"] or 0
        amounts[cause] += row["rebuild"] or 0
        classified += cause != "unknown"
    where = "datetime(r.ts,'unixepoch','localtime')>=? and datetime(r.ts,'unixepoch','localtime')<?"
    requests = conn.execute(
        f"""select date(r.ts,'unixepoch','localtime') day,r.session_id,s.project,r.model,r.cost_usd,
    coalesce(r.input_tok,0) input,coalesce(r.output_tok,0) output,coalesce(r.cache_read_tok,0) cr,coalesce(r.cache_w5m_tok,0) w5t,coalesce(r.cache_w1h_tok,0) w1t,
    r.cache_read_tok*r.in_usd*r.cache_read_x/1e6*{factor} rd,r.cache_w5m_tok*r.in_usd*r.cache_w5m_x/1e6*{factor} w5,r.cache_w1h_tok*r.in_usd*r.cache_w1h_x/1e6*{factor} w1,
    r.cache_read_tok*r.in_usd*(1-r.cache_read_x)/1e6*{factor} saving
    from v_request_cost r join session s using(session_id) left join price p on p.model=r.model and date(r.ts,'unixepoch','localtime')>=p.valid_from and (p.valid_to is null or date(r.ts,'unixepoch','localtime')<p.valid_to) where {where}{project_clause}""",
        params,
    ).fetchall()
    read = [0.0] * count
    write_5m = [0.0] * count
    write_1h = [0.0] * count
    sessions = defaultdict(
        lambda: {
            "rd": 0.0,
            "w5": 0.0,
            "w1": 0.0,
            "cr": 0,
            "input": 0,
            "w5t": 0,
            "w1t": 0,
            "tok": 0,
            "cost": 0.0,
            "models": Counter(),
            "project": None,
            "causes": Counter(),
        }
    )
    unknown = 0
    read_tokens = write_tokens = 0
    read_cost = write_cost = saving = 0
    for row in requests:
        index = indexes[row["day"]]
        read[index] += row["rd"] or 0
        write_5m[index] += row["w5"] or 0
        write_1h[index] += row["w1"] or 0
        read_cost += row["rd"] or 0
        write_cost += (row["w5"] or 0) + (row["w1"] or 0)
        saving += row["saving"] or 0
        read_tokens += row["cr"]
        write_tokens += row["w5t"] + row["w1t"]
        unknown += row["cost_usd"] is None
        item = sessions[row["session_id"]]
        item["rd"] += row["rd"] or 0
        item["w5"] += row["w5"] or 0
        item["w1"] += row["w1"] or 0
        item["cr"] += row["cr"]
        item["input"] += row["input"]
        item["w5t"] += row["w5t"]
        item["w1t"] += row["w1t"]
        item["tok"] += row["input"] + row["output"] + row["cr"] + row["w5t"] + row["w1t"]
        item["cost"] += row["cost_usd"] or 0
        item["models"][row["model"]] += row["input"] + row["cr"] + row["w5t"] + row["w1t"]
        item["project"] = row["project"]
    for row in events:
        sessions[row["session_id"]]["causes"][row["cause"]] += 1
    lower_ts = dt.datetime.fromisoformat(lo).timestamp()
    upper_ts = dt.datetime.fromisoformat(hi).timestamp()
    overhead_params = [lower_ts, upper_ts]
    overhead_project = ""
    if window.project is not None:
        overhead_project = " and project = ?"
        overhead_params.append(window.project)
    overhead = conn.execute(
        f"select * from v_context_overhead where first_ts>=? and first_ts<?{overhead_project}",
        overhead_params,
    ).fetchall()
    model_counts = Counter(row["model"] for row in overhead)
    startup_model = model_counts.most_common(1)[0][0] if model_counts else None
    startup = [row["startup_context_tok"] for row in overhead if row["model"] == startup_model]
    previous_start = lower_ts - count * 86400
    previous_params = [previous_start, lower_ts, startup_model]
    previous_project = ""
    if window.project is not None:
        previous_project = " and project = ?"
        previous_params.append(window.project)
    previous = (
        [
            row["startup_context_tok"]
            for row in conn.execute(
                f"select * from v_context_overhead where first_ts>=? and first_ts<? and model=?{previous_project}",
                previous_params,
            )
        ]
        if startup_model
        else []
    )
    startup_text = (
        f"{sum(startup) / len(startup):,.0f} tok/session ({_model_group(startup_model)}, {len(startup)} sessions)"
        if startup
        else "—"
    )
    startup_text += f" / 前期 {sum(previous) / len(previous):,.0f}" if previous else ""
    session_rows = []
    for session_id, item in sorted(
        sessions.items(), key=lambda value: value[1]["w5"] + value[1]["w1"], reverse=True
    )[:15]:
        side = item["input"] + item["cr"] + item["w5t"] + item["w1t"]
        hit = item["cr"] / side if side else 0
        dominant, dominant_tokens = (
            item["models"].most_common(1)[0] if item["models"] else ("unknown", 0)
        )
        share = dominant_tokens / sum(item["models"].values()) if item["models"] else 0
        mixed = share < 0.9
        model = ("混合 " if mixed else "") + _model_group(dominant)
        blended = item["cost"] / item["tok"] * 1e6 if item["tok"] else 0
        notes = ", ".join(f"{key} {value}" for key, value in item["causes"].most_common(2)) or "—"
        session_rows.append(
            Row(
                [
                    Cell(session_id[:8], "left"),
                    Cell(ui.project_name(item["project"]), "left", clip="project-clip"),
                    Cell(notes),
                    Cell(f"{ui.money(item['w5'])} / {ui.money(item['w1'])}"),
                    Cell(f"{hit * 100:.1f}%"),
                    Cell(model),
                    Cell(ui.money(blended)),
                    Cell(ui.money(item["rd"])),
                    Cell("", "left", content=ui.code(f"metsuke trace {session_id[:8]} --html")),
                ],
                highlight=not mixed and hit < 0.9 and item["cost"] >= 1,
            )
        )
    loss_rows = []
    losses = sorted(
        (row for row in events if (row["gap"] or 0) <= 0),
        key=lambda row: row["rebuild"] or 0,
        reverse=True,
    )[:15]
    for row in losses:
        idle = row["gap_seconds"] is not None and row["gap_seconds"] >= 300
        sid = row["session_id"][:8]
        gap_sort = row["gap_seconds"] if row["gap_seconds"] is not None else -1
        gap_text = f"{row['gap_seconds'] / 60:.0f}分" if row["gap_seconds"] is not None else "—"
        lost = -(row["gap"] or 0)
        rebuild_cost = row["rebuild"] or 0
        cause = "未分類" if row["cause"] not in CAUSE or row["cause"] == "unknown" else row["cause"]
        loss_rows.append(
            [
                Cell("放置" if idle else "活動中", "left", sort="放置" if idle else "活動中"),
                Cell(sid, "left", sort=sid),
                Cell(
                    ui.project_name(row["project"]),
                    "left",
                    sort=ui.project_name(row["project"]),
                    clip="project-clip",
                ),
                Cell(cause, sort=cause),
                Cell(gap_text, sort=gap_sort),
                Cell(f"{lost:,}", sort=lost),
                Cell(ui.money(rebuild_cost), sort=rebuild_cost),
                Cell(
                    "",
                    "left",
                    content=ui.code(f"metsuke trace {sid} --focus {row['request_id'][:16]} --html"),
                ),
            ]
        )
    ttl_projects = Counter(row["project"] for row in events if row["cause"] == "ttl_expiry")
    repeat_project = ttl_projects.most_common(1)[0][0] if ttl_projects else None
    four_week_start = upper_ts - 28 * 86400
    repeat = (
        conn.execute(
            "select count(*) from v_cache_identity ci join session s using(session_id) where ci.cause='ttl_expiry' and s.project is ? and ci.ts>=? and ci.ts<?",
            (repeat_project, four_week_start, upper_ts),
        ).fetchone()[0]
        if repeat_project is not None
        else 0
    )
    rebuild = sum(amounts.values())
    breakdown = " / ".join(
        f"{'未分類' if key == 'unknown' else key} {ui.money(amounts[key])}" for key in CAUSE
    )
    coverage = classified / len(events) * 100 if events else 0
    insight = ui.insight(
        f"⚡再構築費 推定{ui.money(rebuild)}（原因別: {breakdown}） · 分類カバレッジ {coverage:.1f}%（件数ベース） · {ui.project_name(repeat_project)} 直近4週ttl_expiry {repeat}回"
    )
    hook_start = conn.execute("SELECT MIN(ts) FROM hook_event").fetchone()[0]
    pre_hook = sum(row["ts"] < hook_start for row in events) if hook_start is not None else 0
    partial_hook_note = (
        f"hook記録開始前の⚡が {pre_hook:,}/{len(events):,}件（{pre_hook / len(events) * 100:.1f}%）。"
        "hookに依存する原因は compaction と config_change の2つのみ（他のcauseは非依存）。"
        "この期間でこの2つが0件でも『起きなかった』のではなく『判定できない』。"
        "hook記録はspool由来のため遡及再構築できない。"
        if pre_hook
        else None
    )
    missing_hook_note = (
        "hook記録がまったく無いため、compaction と config_change は期間全体で判定できない。"
        "この2つが0件でも『起きなかった』ことを意味しない。"
        "hookが未登録の可能性があり、scripts/install-claude-hooks.sh で登録できる。"
        if hook_start is None and events
        else None
    )
    hook_note = missing_hook_note or partial_hook_note
    hook_note_html = ui.text_block(hook_note, cls="dim") if hook_note else ui.plain("")
    idle_note = "直前リクエストから5分以上空いた後の喪失。1h/5m キャッシュのTTL失効。回避レバー＝離席前の区切り・再開のまとめ方・モデル切替タイミング。"
    active_note = "5分以内の連続作業中に、内容が変わっていないのに会話キャッシュが一括ミスし全再write（read≈9-18kへ）した喪失。147/169が同量再write（縮小なし）・102件が30秒未満・132/147がtool loop途中。idle TTL/context edit/内容変化はいずれも棄却され、prompt caching が best-effort（TTL内・活動中でも保持保証なし）である挙動と整合＝ユーザー起因ではない（06-open-questions Q14）。provider側evictionかCC側挙動かは transcript から区別不可。"
    growth_note = "gap>0＝別経路で作られたキャッシュを読んだ・喪失ではない。実測で約8割が『直前ターンの output 再読』による会計上の偽陽性、残り約2割は大容量context再アタッチ（別経路で書かれたブロックの読み戻し）。増加側は金額影響が軽微（再構築費総額の約1%）のため補正は見送り。件数とcauseの解釈時のみ留意。"

    def charts(series, money_values):
        prefix = "（再構築費・ドル）" if money_values else ""
        unit = "再構築費" if money_values else "件数"
        return ui.join(
            ui.heading(2, f"日次⚡{unit}（喪失・放置起因 ≥5分間隔）"),
            ui.card(
                ui.stacked_bars(days, series[0], CAUSE, height=240, money_values=money_values)
            ),
            ui.text_block(prefix + idle_note, cls="dim"),
            ui.heading(2, f"日次⚡{unit}（喪失・活動中 <5分間隔＝会話キャッシュ全リセット）"),
            ui.card(
                ui.stacked_bars(days, series[1], CAUSE, height=240, money_values=money_values)
            ),
            ui.text_block(prefix + active_note, cls="dim"),
            ui.heading(2, f"日次⚡{unit}（増加・参考／損失ではない）"),
            ui.card(
                ui.stacked_bars(days, series[2], CAUSE, height=240, money_values=money_values)
            ),
            ui.text_block(prefix + growth_note, cls="dim"),
        )

    loss_table = ui.join(
        ui.heading(2, "⚡喪失 再構築費 上位15（コスト影響順）"),
        ui.table(
            [
                Column("区分", "left", True),
                Column("sid", "left", True),
                Column("project", "left", True),
                Column("原因", sortable=True),
                Column("間隔", sortable=True),
                Column("喪失tok", sortable=True),
                Column("再構築費 ▼", sortable=True, sort_dir="desc"),
                Column("command", "left"),
            ],
            loss_rows,
        ),
        ui.text_block(
            "再構築費＝リセットrequestのwrite費（read の約20倍高い1h/5m単価で書き直したドル）。compaction 行は意図的な要約writeを含むため純粋な無駄ではない点に注意。個別精査は metsuke trace へ。",
            cls="dim",
        ),
    )
    body = ui.join(
        insight,
        hook_note_html,
        ui.legend([("未分類" if key == "unknown" else key, CAUSE[key]) for key in CAUSE]),
        ui.tabs("metric", [("metric-count", "件数", True), ("metric-cost", "再構築費", False)]),
        ui.panel(
            "metric", "metric-count", charts((loss_idle, loss_active, growth), False), active=True
        ),
        ui.panel(
            "metric",
            "metric-cost",
            charts((cost_idle, cost_active, cost_growth), True),
            active=False,
        ),
        loss_table,
        ui.heading(2, "日次費目バランス"),
        ui.legend(
            [
                ("cache read", COLORS["cache_read"]),
                ("cache write", COLORS["cache_w5m"]),
                ("w1h選択率", COLORS["cache_w1h"]),
            ]
        ),
        ui.card(ui.cache_balance(days, read, write_5m, write_1h)),
        ui.heading(2, "write費の多いセッション上位15"),
        ui.table(
            _columns(
                [
                    ("sid", 1, 0),
                    ("project", 1, 0),
                    ("⚡上位原因", 0, 0),
                    ("write 5m / 1h", 0, 0),
                    ("cache_read率", 0, 0),
                    ("主モデル", 0, 0),
                    ("blended単価", 0, 0),
                    ("read費", 0, 0),
                    ("command", 1, 0),
                ]
            ),
            session_rows,
        ),
        ui.text_block(
            "ハイライト: 非混合かつcache_read率<90%かつ総額≥$1。blended単価はモデル間比較不可・生トークン分母（中断output未計上で過小方向）。write→read非対応の期間集計近似。対話のみ判別基準は暫定。",
            cls="dim",
        ),
    )
    unoffset = max(0, write_cost - saving)
    ratio = read_tokens / write_tokens if write_tokens else 0
    total = ui.plain(
        f"read {ui.money(read_cost)} / write {ui.money(write_cost)} · 期間内read/write {ratio:.2f}倍 · 未相殺write費 {ui.money(unoffset)}（推定） · 起動固定費 {startup_text}"
    )
    if unknown:
        total = ui.join(
            total, ui.plain(" · "), ui.warning(f"価格カバレッジ不足 {unknown} requests")
        )
    return LegacyViewModel(
        "metsuke · キャッシュ健全性", window.label, total, body, ui.local_timezone()
    )
