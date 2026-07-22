import datetime as dt
import json
import os
import re
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import pytest

from metsuke import archiver, cli, ingest, ledger, trace_html, viewgen
from metsuke.redaction import REDACTION_VERSION
from metsuke.viewgen import render
from metsuke.viewgen.window import Window, resolve
from metsuke.viewmodel import count_cost_bearing_prompts
from metsuke.viewmodel import overview as overview_viewmodel

from dashboard_values import extract_dashboard_values

PROJECT_B = "project-beta"
ESCAPED_PROJECT_SCRIPT = "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
ESCAPED_PROJECT_IMAGE = "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"
ESCAPED_PROMPT = "&lt;script&gt;prompt_path()&lt;/script&gt;"
ESCAPED_PROMPT_ID = "p&lt;!--can"
FIXTURE_DAY = dt.date(2026, 7, 20)
FIXTURE_WINDOW = Window(
    FIXTURE_DAY - dt.timedelta(days=13),
    FIXTURE_DAY,
    None,
    "2026-07-07 — 2026-07-20",
)
DASHBOARD_FIXTURES = Path(__file__).parent / "fixtures" / "dashboard"


@pytest.fixture()
def view_env(tmp_path, monkeypatch):
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    conn = ledger.connect()
    conn.execute("DELETE FROM price WHERE model='claude-sonnet-5'")
    conn.execute(
        """INSERT INTO price
           (model,valid_from,in_usd,out_usd,cache_read_x,cache_w5m_x,cache_w1h_x,
            batch_x,fast_x,geo_us_x)
           VALUES ('claude-sonnet-5','1970-01-01',3,15,0.1,1.25,2,0.5,2,1.1)"""
    )
    stamp = dt.datetime(2026, 7, 20, 12, tzinfo=dt.UTC).timestamp()
    project = '</script><script>alert(1)</script>"><img src=x onerror=alert(1)>'
    prompt_id = "p<!--canary"
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        ("s1", project, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES (?,?,?,?)",
        (prompt_id, "s1", stamp, "<script>prompt_path()</script> visible prompt"),
    )
    conn.execute(
        """INSERT INTO request
        (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
         cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
        VALUES ('r1','s1','s1',?,?, 'claude-sonnet-5',100000,10,100000,100,10,0,0,
                'transcript')""",
        (prompt_id, stamp),
    )
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        ("s2", PROJECT_B, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p2','s2',?,'beta prompt')",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO request
        (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
         cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
        VALUES ('r2','s2','s2','p2',?,'claude-sonnet-5',200000,20,50000,200,20,0,0,
                'transcript')""",
        (stamp,),
    )
    conn.commit()
    try:
        yield home, conn, project
    finally:
        conn.close()
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def _generate(view_env):
    home, conn, _ = view_env
    today = FIXTURE_DAY
    path = viewgen.generate("dist", Window(today, today, None, f"{today} — {today}"), conn=conn)
    assert path == home / "views" / "dist.html"
    return path


def _dashboard_fixture(name: str) -> dict:
    return json.loads((DASHBOARD_FIXTURES / name).read_text())


@contextmanager
def _local_timezone(name: str):
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_view_security_template_permissions_and_links(view_env):
    path = _generate(view_env)
    html = path.read_text()
    assert ESCAPED_PROJECT_SCRIPT in html
    assert ESCAPED_PROJECT_IMAGE in html
    assert ESCAPED_PROMPT_ID in html
    assert "</script><script>alert(1)</script>" not in html
    assert '"><img src=x onerror=alert(1)>' not in html
    assert "<!--canary" not in html
    assert f'content="{trace_html.CSP}"' in html
    assert html.index("Content-Security-Policy") < html.index("<script")
    for sink in ("innerHTML", "eval(", "Function(", "document.write"):
        assert sink not in html
    assert "http://" not in html and "https://" not in html
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_view_template_markers_are_unique():
    template = resources.files("metsuke").joinpath("view_template.html").read_text()
    for marker in (
        "__VIEW_TITLE__",
        "__VIEW_H1__",
        "__VIEW_PERIOD__",
        "__VIEW_TOTAL__",
        "__VIEW_BODY__",
        "__VIEW_STAMP__",
    ):
        assert template.count(marker) == 1


def test_view_renderer_exposes_keyboard_and_accessibility_semantics():
    markup = str(
        render.join(
            render.tabs("group", [("first", "最初", True), ("second", "次", False)]),
            render.panel("group", "first", render.plain("one"), active=True),
            render.panel("group", "second", render.plain("two"), active=False),
            render.table(
                [render.Column("費用", sortable=True, sort_dir="desc")],
                [[render.Cell("1", sort=1)]],
            ),
            render.line_chart(
                [FIXTURE_DAY], {"費用": [1.0]}, {"費用": "#7aa2f7"}, "$"
            ),
        )
    )
    assert 'role="tablist"' in markup and 'role="tab"' in markup
    assert 'aria-selected="true"' in markup and 'role="tabpanel"' in markup
    assert 'id="second" hidden' in markup
    assert '<th scope="col"' in markup and 'aria-sort="descending"' in markup
    assert '<svg ' in markup and '<title>費用 の推移</title>' in markup
    template = resources.files("metsuke").joinpath("view_template.html").read_text()
    assert "focus-visible" in template and "prefers-reduced-motion" in template
    assert "e.key==='Enter'||e.key===' '" in template


def test_only_renderer_emits_markup():
    tag = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9-]*[\s/>]")
    html_constructor = re.compile(r"\bHtml\s*\(")
    root = resources.files("metsuke").joinpath("viewgen")
    offenders = []
    for path in root.iterdir():
        if path.name.endswith(".py") and path.name != "render.py":
            source = path.read_text()
            if tag.search(source):
                offenders.append(path.name)
            assert not html_constructor.search(source), (
                "view builders must not construct Html directly; use render primitives instead: "
                f"{path.name}"
            )
    assert not offenders, (
        "view builders must pass data to render.py instead of emitting raw markup; "
        f"found tag-like literals in: {', '.join(offenders)}"
    )


def test_renderer_rejects_unsafe_inputs():
    with pytest.raises(ValueError):
        render.table([render.Column("x", cls="attacker")], [])
    with pytest.raises(ValueError):
        render.legend([("x", "red; background:url(x)")])
    with pytest.raises(TypeError):
        render.card("raw")
    escaped = render.table(
        [render.Column('"><img src=x>')],
        [[render.Cell("</script><!--", title='"><img>')]],
    )
    assert "</script>" not in escaped and "<img" not in escaped
    for width in (True, "170", 0, 1001):
        with pytest.raises(ValueError):
            render.clip("text", max_width=width)


def test_purge_and_generation_record_ingests(view_env):
    home, _, _ = view_env
    directory = home / "views"
    directory.mkdir(parents=True, exist_ok=True)
    old = directory / "old.html"
    old.write_text("redaction_version=1")
    future = directory / "future.html"
    future.write_text("redaction_version=999")
    _generate(view_env)
    assert not old.exists() and future.exists()
    records = list((home / "spool" / "hooks").glob("*.ndjson"))
    envelope = json.loads(records[-1].read_text())
    assert envelope["metsuke_event"] == "view_html_generated"
    assert envelope["payload"] == {"view": "dist", "days": 1, "project": None}
    source = home.parent / "source"
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    assert (
        conn.execute("SELECT count(*) FROM hook_event WHERE kind='view_html_generated'").fetchone()[
            0
        ]
        >= 1
    )
    conn.close()


@pytest.mark.parametrize("name", ["../x", "/tmp/evil", ".hidden", "dist/../x"])
def test_view_name_allowlist(view_env, name):
    _, conn, _ = view_env
    today = FIXTURE_DAY
    assert viewgen.generate(name, Window(today, today, None, str(today)), conn=conn) is None


def test_window_resolution_boundaries_and_errors(view_env):
    _, conn, _ = view_env
    anchor = "2026-07-19"
    assert resolve(conn, as_of=anchor).start == dt.date(2026, 7, 6)
    assert resolve(conn, days=3, as_of=anchor).start == dt.date(2026, 7, 17)
    assert resolve(conn, today=True, as_of=anchor).start == dt.date(2026, 7, 19)
    week = resolve(conn, week=True, as_of=anchor)
    assert (week.start, week.end) == (dt.date(2026, 7, 13), dt.date(2026, 7, 19))
    last_week = resolve(conn, week="last", as_of=anchor)
    assert (last_week.start, last_week.end) == (dt.date(2026, 7, 6), dt.date(2026, 7, 12))
    month = resolve(conn, month=True, as_of=anchor)
    assert (month.start, month.end) == (dt.date(2026, 7, 1), dt.date(2026, 7, 19))
    last_month = resolve(conn, month="last", as_of=anchor)
    assert (last_month.start, last_month.end) == (dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    named_month = resolve(conn, month="2026-02", as_of=anchor)
    assert (named_month.start, named_month.end) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    explicit = resolve(conn, date_from="2026-07-01", date_to="2026-07-03", as_of=anchor)
    assert explicit.sql_bounds() == ("2026-07-01 00:00:00", "2026-07-04 00:00:00")
    for kwargs in (
        {"days": 0},
        {"today": True, "week": True},
        {"date_from": "2026-07-02"},
        {"date_to": "2026-07-02"},
        {"days": 3, "date_to": "2026-07-02"},
        {"date_from": "2026-07-03", "date_to": "2026-07-02"},
        {"date_from": "07/01/2026", "date_to": "2026-07-02"},
        {"week": "next"},
        {"month": "2026-13"},
    ):
        with pytest.raises(ValueError):
            resolve(conn, as_of=anchor, **kwargs)


def test_cli_view_path(view_env):
    assert cli.main(["view", "dist", "--days", "3"]) == 0
    assert cli.main(["view", "dist", "--week", "last"]) == 0
    assert cli.main(["view", "dist", "--month", "2026-06"]) == 0


def test_cli_view_names_match_registry():
    assert cli.VIEW_NAMES == tuple(viewgen.VIEWS)


@pytest.mark.parametrize("name", ["period", "cache", "trend"])
def test_round2_views_escape_ledger_text(view_env, name):
    home, conn, _ = view_env
    today = FIXTURE_DAY
    path = viewgen.generate(name, Window(today, today, None, str(today)), conn=conn)
    assert path == home / "views" / f"{name}.html"
    html = path.read_text()
    assert ESCAPED_PROJECT_SCRIPT in html
    assert ESCAPED_PROJECT_IMAGE in html
    assert "</script><script>alert(1)</script>" not in html
    assert '"><img src=x onerror=alert(1)>' not in html
    if name == "period":
        assert ESCAPED_PROMPT in html
        assert ESCAPED_PROMPT_ID in html
        assert "<script>prompt_path()</script>" not in html
        assert "<!--canary" not in html


def test_period_uses_ledger_redacted_prompt_without_plaintext_leak(view_env):
    home, conn, _ = view_env
    redacted = "safe [REDACTED:openai_key:deadbeef]"
    plaintext = "sk-proj-plaintext-secret-not-in-ledger"
    source_secret = home.parent / "source" / "raw.jsonl"
    source_secret.write_text(json.dumps({"message": {"content": plaintext}}))
    conn.execute("UPDATE prompt SET text=?", (redacted,))
    conn.commit()
    today = FIXTURE_DAY
    path = viewgen.generate("period", Window(today, today, None, str(today)), conn=conn)
    assert path is not None
    html = path.read_text()
    assert redacted in html
    assert plaintext not in html


def test_round2_cli_paths(view_env):
    assert cli.main(["view", "period", "--days", "3"]) == 0
    assert cli.main(["view", "cache", "--days", "3"]) == 0
    assert cli.main(["view", "trend", "--days", "14"]) == 0


def test_request_cache_write_cost_and_cache_drilldown_focus(view_env):
    _, conn, _ = view_env
    row = conn.execute(
        "SELECT cache_write_usd,in_usd,cache_w5m_x,cache_w1h_x "
        "FROM v_request_cost WHERE request_id='r1'"
    ).fetchone()
    expected = (
        100 * row["in_usd"] * row["cache_w5m_x"] + 10 * row["in_usd"] * row["cache_w1h_x"]
    ) / 1e6
    assert row["cache_write_usd"] == pytest.approx(expected)
    stamp = conn.execute("SELECT ts FROM request WHERE request_id='r1'").fetchone()[0]
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES ('r1-break','s1','s1',?,?,'claude-sonnet-5',1,1,1,100,10,0,0,
                   'transcript')""",
        (ESCAPED_PROMPT_ID.replace("&lt;", "<").replace("&gt;", ">"), stamp + 10),
    )

    today = FIXTURE_DAY
    path = viewgen.generate("cache", Window(today, today, None, str(today)), conn=conn)
    html = path.read_text()
    assert "metsuke trace s1 --focus r1-break --html" in html
    assert "metsuke trace s1 --html" in html


def test_cache_hook_coverage_note_tracks_window_and_empty_hook_ledger(view_env):
    _, conn, _ = view_env
    stamp = conn.execute("SELECT ts FROM request WHERE request_id='r1'").fetchone()[0]
    yesterday_stamp = stamp - 86400
    conn.executemany(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,'s1','s1',?,?, 'claude-sonnet-5',1,1,?,100,10,0,0,
                   'transcript')""",
        [
            ("r-yesterday", "p<!--canary", yesterday_stamp, 100000),
            ("r-yesterday-break", "p<!--canary", yesterday_stamp + 10, 1),
        ],
    )
    today = FIXTURE_DAY
    yesterday = today - dt.timedelta(days=1)
    broad_window = Window(yesterday, today, None, f"{yesterday} — {today}")
    hook_window = Window(today, today, None, str(today))

    conn.execute(
        "INSERT INTO hook_event VALUES (?,?,?,?,?)",
        (stamp - 43200, "SessionStart", "s1", None, '{"test":"coverage"}'),
    )
    html = viewgen.generate("cache", broad_window, conn=conn).read_text()
    assert "hook記録開始前の⚡が 1/" in html
    assert "hook記録がまったく無い" not in html
    assert "compaction と config_change の2つのみ" in html
    assert "『起きなかった』のではなく『判定できない』" in html
    assert "spool由来のため遡及再構築できない" in html

    html = viewgen.generate("cache", hook_window, conn=conn).read_text()
    assert "hook記録開始前の⚡" not in html
    assert "hook記録がまったく無い" not in html

    conn.execute("DELETE FROM hook_event")
    html = viewgen.generate("cache", broad_window, conn=conn).read_text()
    assert "hook記録開始前の⚡" not in html
    assert "hook記録がまったく無い" in html
    assert "compaction と config_change は期間全体で判定できない" in html
    assert "0件でも『起きなかった』ことを意味しない" in html
    assert "scripts/install-claude-hooks.sh で登録できる" in html


@pytest.mark.parametrize("name", ["period", "cache", "dist", "trend"])
def test_views_generate_for_empty_window(view_env, name):
    _, conn, _ = view_env
    empty = FIXTURE_DAY - dt.timedelta(days=30)
    path = viewgen.generate(name, Window(empty, empty, None, str(empty)), conn=conn)
    assert path is not None and path.is_file()


def test_named_month_rejects_future(view_env):
    _, conn, _ = view_env
    with pytest.raises(ValueError):
        resolve(conn, month="2027-01", as_of="2026-07-19")


def test_trend_has_all_panels_charts_tables_and_drilldown(view_env):
    _, conn, _ = view_env
    end = FIXTURE_DAY
    start = end - dt.timedelta(days=13)
    path = viewgen.generate("trend", Window(start, end, None, f"{start} — {end}"), conn=conn)
    assert path is not None
    html = path.read_text()
    # Charts carry class="chart"; the inline swatch/bar cell SVGs do not.
    assert html.count('<svg class="chart" ') == 30
    assert html.count("<table>") == 3
    assert "① 総量" in html and "② 分布形の推移（対話のみ）" in html
    assert "③ 行動イベントの推移" in html and "④ 挙動サマリ（グラフ＋表）" in html
    assert "metsuke view period --from" in html


def test_trend_contribution_clip_width_and_title_are_on_span(view_env):
    _, conn, _ = view_env
    end = FIXTURE_DAY
    start = end - dt.timedelta(days=13)
    path = viewgen.generate("trend", Window(start, end, None, f"{start} — {end}"), conn=conn)
    assert path is not None
    html = path.read_text()
    summary_start = html.index("④ 挙動サマリ（グラフ＋表）")
    daily_start = html.index('data-grain-panel="daily"', summary_start)
    weekly_start = html.index('data-grain-panel="weekly"', daily_start)
    monthly_start = html.index('data-grain-panel="monthly"', weekly_start)
    summary_end = html.index("</main>", monthly_start)
    # The width is a quantised CSS class now (cw9 == 180px) rather than an inline
    # style, which the dashboard CSP blocks. 170px rounds up one 20px rung.
    expected = 'class="clip cw9"'
    assert html[daily_start:weekly_start].count(expected) == 14
    assert html[weekly_start:monthly_start].count(expected) == 1
    assert html[monthly_start:summary_end].count(expected) == 1
    spans = re.findall(
        r'<span class="clip cw9" title="[^"]*">[^<]*</span>',
        html[summary_start:summary_end],
    )
    assert len(spans) == 16
    assert '<td class="left" title=' not in html[summary_start:summary_end]


@pytest.mark.parametrize(
    ("name", "empty_text"),
    [
        ("period", "$0.00 · 0 requests"),
        ("cache", "read $0.00 / write $0.00"),
        ("dist", "0 prompts"),
        ("trend", "$0.00 · 日次/週次/月次コスト推移"),
    ],
)
def test_project_filter_includes_match_excludes_other_and_rejects_nonmatch(
    view_env, name, empty_text
):
    _, conn, project = view_env
    today = FIXTURE_DAY
    expected = conn.execute(
        """SELECT SUM(r.cost_usd) FROM v_request_cost r
           JOIN session s USING(session_id) WHERE s.project=?""",
        (project,),
    ).fetchone()[0]
    all_cost = conn.execute("SELECT SUM(cost_usd) FROM v_request_cost").fetchone()[0]
    assert expected != all_cost

    unfiltered = viewgen.generate(name, Window(today, today, None, str(today)), conn=conn)
    assert unfiltered is not None
    unfiltered_html = unfiltered.read_text()
    assert PROJECT_B in unfiltered_html

    selected = viewgen.generate(name, Window(today, today, project, str(today)), conn=conn)
    assert selected is not None
    selected_html = selected.read_text()
    assert ESCAPED_PROJECT_SCRIPT in selected_html
    assert PROJECT_B not in selected_html
    if name == "cache":
        expected_read, expected_write = conn.execute(
            """SELECT
               SUM(r.cache_read_tok*r.in_usd*r.cache_read_x/1e6),
               SUM((r.cache_w5m_tok*r.in_usd*r.cache_w5m_x
                   +r.cache_w1h_tok*r.in_usd*r.cache_w1h_x)/1e6)
               FROM v_request_cost r JOIN session s USING(session_id)
               WHERE s.project=?""",
            (project,),
        ).fetchone()
        all_read, all_write = conn.execute(
            """SELECT
               SUM(cache_read_tok*in_usd*cache_read_x/1e6),
               SUM((cache_w5m_tok*in_usd*cache_w5m_x
                   +cache_w1h_tok*in_usd*cache_w1h_x)/1e6)
               FROM v_request_cost"""
        ).fetchone()
        expected_summary = (
            f"read {render.money(expected_read)} / write {render.money(expected_write)}"
        )
        all_summary = f"read {render.money(all_read)} / write {render.money(all_write)}"
        assert expected_summary in selected_html
        assert all_summary in unfiltered_html
        assert expected_summary != all_summary
    else:
        assert render.money(expected) in selected_html
        assert render.money(all_cost) in unfiltered_html

    missing = viewgen.generate(
        name, Window(today, today, "project-does-not-exist", str(today)), conn=conn
    )
    assert missing is not None
    assert empty_text in missing.read_text()


def test_cli_period_project_filter_uses_matching_project_only(view_env):
    home, conn, project = view_env
    expected = conn.execute(
        """SELECT SUM(r.cost_usd) FROM v_request_cost r
           JOIN session s USING(session_id) WHERE s.project=?""",
        (project,),
    ).fetchone()[0]
    assert (
        cli.main(
            [
                "view",
                "period",
                "--from",
                str(FIXTURE_DAY),
                "--to",
                str(FIXTURE_DAY),
                "--project",
                project,
            ]
        )
        == 0
    )
    html = (home / "views" / "period.html").read_text()
    assert render.money(expected) in html
    assert ESCAPED_PROJECT_SCRIPT in html
    assert PROJECT_B not in html


def test_freshness_stamp_is_visible(view_env):
    _, conn, _ = view_env
    future = FIXTURE_DAY + dt.timedelta(days=2)
    path = viewgen.generate("dist", Window(future, future, None, str(future)), conn=conn)
    assert path is not None
    assert "データ最終:" in path.read_text()
    assert f"redaction_version={REDACTION_VERSION}" in path.read_text()


@pytest.mark.parametrize("name", ["period", "trend", "cache", "dist"])
def test_dashboard_value_golden_matches_current_v1_to_v4(view_env, name):
    _, conn, _ = view_env
    path = viewgen.generate(name, FIXTURE_WINDOW, conn=conn)
    assert path is not None
    actual = extract_dashboard_values(path.read_text(), view=name, timezone="UTC")
    golden_path = DASHBOARD_FIXTURES / f"{name}.json"
    if os.environ.get("UPDATE_DASHBOARD_GOLDENS") == "1":
        golden_path.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
    assert actual == _dashboard_fixture(f"{name}.json")


def test_dashboard_prompt_kpi_is_distinct_cost_bearing_non_synthetic_prompt(view_env):
    _, conn, _ = view_env
    stamp = dt.datetime(2026, 7, 20, 13, tzinfo=dt.UTC).timestamp()
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p-control','s1',?,'control')",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p-synthetic','s1',?,'synthetic')",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES ('r-synthetic','s1','s1','p-synthetic',?,'claude-sonnet-5',1,1,
                   0,0,0,1,'transcript')""",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES ('r1-repeat','s1','s1','p<!--canary',?,'claude-sonnet-5',1,1,
                   0,0,0,0,'transcript')""",
        (stamp,),
    )
    assert conn.execute("SELECT COUNT(*) FROM prompt").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 4
    assert count_cost_bearing_prompts(conn) == 2


def _insert_tied_dashboard_rows(conn) -> None:
    stamp = dt.datetime(2026, 7, 20, 14, tzinfo=dt.UTC).timestamp()
    for suffix in ("a", "z"):
        session_id = f"s-tie-{suffix}"
        prompt_id = f"p-tie-{suffix}"
        conn.execute(
            "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
            (session_id, "tie-project", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES (?,?,?,'tie prompt')",
            (prompt_id, session_id, stamp),
        )
        conn.execute(
            """INSERT INTO request
               (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
                cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
               VALUES (?,?,?,?,?,'claude-sonnet-5',500000,0,0,0,0,0,'transcript')""",
            (f"r-tie-{suffix}", session_id, session_id, prompt_id, stamp),
        )
    conn.commit()


def test_dashboard_tie_break_matches_current_prompt_and_session_order(view_env):
    _, conn, _ = view_env
    _insert_tied_dashboard_rows(conn)
    path = viewgen.generate("period", FIXTURE_WINDOW, conn=conn)
    assert path is not None
    values = extract_dashboard_values(path.read_text(), view="period", timezone="UTC")
    session_commands = [row[-1]["text"] for row in values["tables"][0]["rows"]]
    prompt_commands = [row[-1]["text"] for row in values["tables"][1]["rows"]]
    actual = {
        "prompt_order": [item for item in prompt_commands if item.startswith("metsuke explain p-tie-")],
        "session_order": [item for item in session_commands if item.startswith("metsuke trace s-tie-")],
    }
    assert actual == _dashboard_fixture("tie_break.json")["expected"]


def _insert_unknown_cost_request(conn) -> None:
    stamp = dt.datetime(2026, 7, 20, 15, tzinfo=dt.UTC).timestamp()
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES ('s-unknown','unknown-fixture',?,?)",
        (stamp, stamp),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p-unknown','s-unknown',?,'unknown fixture')",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES ('r-unknown','s-unknown','s-unknown','p-unknown',?,'unpriced-model',1,1,
                   0,0,0,0,'transcript')""",
        (stamp,),
    )
    conn.commit()


def test_dashboard_unknown_cost_fixture_defines_incomplete_total(view_env):
    _, conn, _ = view_env
    _insert_unknown_cost_request(conn)
    lo, hi = FIXTURE_WINDOW.sql_bounds()
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd),0),COUNT(*),SUM(cost_usd IS NULL)
           FROM v_request_cost
           WHERE datetime(ts,'unixepoch','localtime')>=?
             AND datetime(ts,'unixepoch','localtime')<?""",
        (lo, hi),
    ).fetchone()
    expected = _dashboard_fixture("unknown_cost.json")["expected"]
    assert row[0] == pytest.approx(expected["known_total_usd"])
    assert row[1] == expected["request_count"]
    assert row[2] == expected["unknown_cost_request_count"]
    assert expected["complete"] is False


def test_dashboard_unknown_cost_is_visible_without_dropping_request(view_env):
    _, conn, _ = view_env
    _insert_unknown_cost_request(conn)
    path = viewgen.generate("period", FIXTURE_WINDOW, conn=conn)
    assert path is not None
    values = extract_dashboard_values(path.read_text(), view="period", timezone="UTC")
    expected = _dashboard_fixture("unknown_cost.json")["expected"]
    assert f"未知価格 {expected['unknown_cost_request_count']} requests" in values["total"]
    assert f"{expected['request_count']:,} requests" in values["total"]


def test_dashboard_timezone_changes_the_local_day_membership(view_env):
    _, conn, _ = view_env
    conn.execute(
        "UPDATE request SET ts=? WHERE request_id='r1'",
        (dt.datetime(2026, 7, 19, 16, tzinfo=dt.UTC).timestamp(),),
    )
    conn.execute(
        "UPDATE request SET ts=? WHERE request_id='r2'",
        (dt.datetime(2026, 7, 20, 15, tzinfo=dt.UTC).timestamp(),),
    )
    lo, hi = Window(FIXTURE_DAY, FIXTURE_DAY, None, str(FIXTURE_DAY)).sql_bounds()
    actual = {}
    for timezone in ("UTC", "Asia/Tokyo"):
        with _local_timezone(timezone):
            actual[timezone] = [
                row[0]
                for row in conn.execute(
                    """SELECT request_id FROM v_request_cost
                       WHERE datetime(ts,'unixepoch','localtime')>=?
                         AND datetime(ts,'unixepoch','localtime')<?
                       ORDER BY request_id""",
                    (lo, hi),
                )
            ]
    assert actual == _dashboard_fixture("timezone.json")["expected_request_ids"]


def test_dashboard_previous_period_fixture_is_immediately_preceding_same_day_count():
    fixture = _dashboard_fixture("previous_period.json")
    selected_start = dt.date.fromisoformat(fixture["selected"]["start"])
    selected_end = dt.date.fromisoformat(fixture["selected"]["end"])
    days = (selected_end - selected_start).days + 1
    previous_end = selected_start - dt.timedelta(days=1)
    previous_start = previous_end - dt.timedelta(days=days - 1)
    assert days == fixture["selected"]["inclusive_days"]
    assert str(previous_start) == fixture["previous"]["start"]
    assert str(previous_end) == fixture["previous"]["end"]
    assert fixture["previous_zero"]["percent_change"] is None
    assert fixture["previous_zero"]["display"] == "比較不能"


def test_dashboard_previous_zero_is_rendered_as_not_comparable(view_env):
    _, conn, _ = view_env
    fixture = _dashboard_fixture("previous_period.json")
    selected = Window(
        dt.date.fromisoformat(fixture["selected"]["start"]),
        dt.date.fromisoformat(fixture["selected"]["end"]),
        None,
        "selected fixture period",
    )
    model = overview_viewmodel.query(conn, selected)
    cost = next(kpi for kpi in model.kpis if kpi.name == "API換算コスト")
    assert str(model.previous_window.start) == fixture["previous"]["start"]
    assert str(model.previous_window.end) == fixture["previous"]["end"]
    assert cost.comparison.percent_change is fixture["previous_zero"]["percent_change"]
    assert cost.comparison.display == fixture["previous_zero"]["display"]


def test_sanitized_ledger_summary_contains_only_allowlisted_aggregates():
    snapshot = _dashboard_fixture("sanitized_ledger_summary.json")
    assert set(snapshot) == {
        "snapshot_schema_version",
        "ledger_schema_version",
        "ledger_schema_sha256",
        "parser_version",
        "ledger_parser_version",
        "redaction_version",
        "ledger_redaction_version",
        "measured_at",
        "timezone",
        "windows",
    }
    allowed_window_fields = {
        "from",
        "to_exclusive",
        "total_usd",
        "request_count",
        "cost_bearing_prompt_count",
        "session_count",
        "project_count",
        "unknown_cost_request_count",
        "prompt_table_rows",
    }
    assert set(snapshot["windows"]) == {"latest_day", "latest_7_days", "all_observed"}
    assert all(set(window) == allowed_window_fields for window in snapshot["windows"].values())


def test_static_html_still_renders_charts_and_needs_no_inline_style(view_env):
    """Test 4: regression guard for moving the chart primitives.

    The static generator and the dashboard now share one chart implementation,
    so the static path must keep drawing real SVG charts, and must keep working
    without the inline styles the dashboard's CSP blocks.
    """

    _, conn, _ = view_env
    for name in ("trend", "cache"):
        path = viewgen.generate(name, FIXTURE_WINDOW, conn=conn)
        assert path is not None
        html = path.read_text()
        body = html[html.index("<main>") : html.index("</main>")]
        assert '<svg class="chart"' in body, f"{name} lost its static charts"
        assert "style=" not in body, f"{name} still emits an inline style"
        # The shared chart stylesheet must be present so the classes resolve.
        assert ".ch-grid{stroke:var(--ch-grid" in html
        assert ".bar-fill{fill:var(--ch-bar-fill" in html
