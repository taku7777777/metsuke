from __future__ import annotations

import ast
import datetime as dt
import re
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import assert_csp_safe
from metsuke import cli
from metsuke.dashboard import pages, routes, server
from metsuke.viewmodel import cache, dist, overview, period, trend
from metsuke.viewmodel.common import Cell, Column, LegacyViewModel, Page, Row, Window, node
from test_views import FIXTURE_DAY, PROJECT_B, view_env as shared_view_env  # noqa: F401


@pytest.fixture
def page_env(request):
    home, conn, malicious_project = request.getfixturevalue("shared_view_env")
    return home / "ledger.db", conn, malicious_project


def _response(database_path: Path, query: str, today: dt.date | None = None):
    return routes.dashboard_response(query, database_path, today or FIXTURE_DAY)


def test_default_is_yesterday_and_range_redirects_to_canonical(page_env):
    database_path, _, _ = page_env
    today = FIXTURE_DAY + dt.timedelta(days=1)
    response = _response(database_path, "", today)
    assert response.status == 303
    assert response.headers["Location"] == (
        "/v1/dashboard?view=overview&from=2026-07-20&to=2026-07-20"
    )

    response = _response(database_path, "view=period&range=7d", today)
    assert response.status == 303
    location = response.headers["Location"]
    assert parse_qs(urlsplit(location).query) == {
        "view": ["period"],
        "from": ["2026-07-15"],
        "to": ["2026-07-21"],
    }
    canonical = _response(database_path, urlsplit(location).query, today)
    assert canonical.status == 200
    assert "直近7日" in canonical.body.decode()


def test_explicit_window_project_and_tab_match_url_and_content(page_env):
    database_path, _, _ = page_env
    query = f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}&project={PROJECT_B}"
    response = _response(database_path, query, FIXTURE_DAY + dt.timedelta(days=2))
    assert response.status == 200
    text = response.body.decode()
    assert "カスタム" in text
    assert "2026-07-20 — 2026-07-20" in text
    assert PROJECT_B in text
    assert "view=period&amp;from=2026-07-20&amp;to=2026-07-20" in text
    assert "beta prompt" in text

    period_response = _response(
        database_path, f"view=period&from={FIXTURE_DAY}&to={FIXTURE_DAY}&project={PROJECT_B}"
    )
    assert period_response.status == 200
    assert "集中先" in period_response.body.decode()


@pytest.mark.parametrize(
    ("view", "heading", "query_model"),
    [
        ("period", "集中先", period.query),
        ("trend", "推移ビュー", trend.query),
        ("cache", "キャッシュ健全性", cache.query),
        ("dist", "プロンプト分布", dist.query),
    ],
)
def test_all_five_tabs_share_window_project_and_the_exact_viewmodel_projection(
    page_env, view, heading, query_model
):
    database_path, conn, _ = page_env
    window = Window(FIXTURE_DAY, FIXTURE_DAY, PROJECT_B, f"{FIXTURE_DAY} — {FIXTURE_DAY}")
    page = Page()
    model = (
        query_model(conn, window, page)
        if view == "period"
        else query_model(conn, window)
    )
    query = f"view={view}&from={FIXTURE_DAY}&to={FIXTURE_DAY}&project={PROJECT_B}"
    response = _response(database_path, query)
    text = response.body.decode()
    assert response.status == 200
    assert heading in text
    assert PROJECT_B in text
    expected = pages._period(model) if view == "period" else pages._legacy(model, page)
    assert expected in text
    for tab in ("overview", "period", "trend", "cache", "dist"):
        assert f"view={tab}&amp;from={FIXTURE_DAY}&amp;to={FIXTURE_DAY}" in text


def test_xss_values_are_escaped_only_at_page_boundary(page_env):
    database_path, _, malicious_project = page_env
    response = _response(database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    text = response.body.decode()
    assert response.status == 200
    assert "&lt;script&gt;prompt_path()&lt;/script&gt;" in text
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>prompt_path()</script>" not in text
    assert malicious_project not in text
    assert "<img src=x onerror=alert(1)>" not in text


@pytest.mark.parametrize(
    "query",
    [
        "view=overview%27%3BDROP+TABLE+request--&from=2026-07-20&to=2026-07-20",
        "view=overview&from=2026-07-20&to=2026-07-20&sort=cost%3BDROP+TABLE+request",
        "view=overview&from=bad&to=2026-07-20",
        "view=overview&from=2026-07-21&to=2026-07-20",
        "view=overview&from=2026-07-20&to=2026-07-21",
        "view=overview&from=2026-07-20&to=2026-07-20&limit=0",
        "view=overview&from=2026-07-20&to=2026-07-20&limit=201",
        "view=overview&from=2026-07-20&to=2026-07-20&page=0",
        "view=overview&from=2026-07-20&to=2026-07-20&page=1000001",
    ],
)
def test_invalid_allowlists_windows_and_pagination_are_rejected(page_env, query):
    database_path, _, _ = page_env
    assert _response(database_path, query).status == 400


def test_project_filter_is_bound_and_does_not_turn_into_sql(page_env):
    database_path, _, _ = page_env
    injection = "project-beta%27+OR+1%3D1--"
    response = _response(
        database_path,
        f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}&project={injection}",
    )
    assert response.status == 200
    text = response.body.decode()
    assert "beta prompt" not in text
    assert "$0.00" in text


def test_previous_period_zero_is_not_fabricated_and_sections_are_ordered(page_env):
    database_path, _, _ = page_env
    response = _response(database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    text = response.body.decode()
    assert "比較不能" in text
    headings = ["KPI", "費目構成", "高額prompt", "高額session", "cache再作成", "次の確認"]
    assert [text.index(heading) for heading in headings] == sorted(text.index(h) for h in headings)


def test_ssr_uses_shared_models_and_matches_their_values(page_env):
    database_path, conn, _ = page_env
    window = Window(FIXTURE_DAY, FIXTURE_DAY, None, f"{FIXTURE_DAY} — {FIXTURE_DAY}")
    page = Page()
    overview_model = overview.query(conn, window, page)
    overview_text = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    for kpi in overview_model.kpis:
        assert kpi.display in overview_text
    for prompt_item in overview_model.top_prompts:
        assert prompt_item.amount.display in overview_text

    period_model = period.query(conn, window, page)
    period_text = _response(
        database_path, f"view=period&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    assert period_model.period in period_text
    assert "$0.95" in period_text


def test_progressive_enhancement_get_form_real_links_and_csp_safe_script(page_env):
    """Progressive enhancement is the correctness spine: the SSR page renders real
    data, working links and a working GET filter form on its own, and the only
    JavaScript is the served /dashboard.js loaded with ``defer`` — no inline
    ``<script>`` body and no inline ``on*=`` handler the CSP would block.

    (Rewritten from the former "no JavaScript at all" test: the page now carries one
    external, deferred script by design; the CSP-safety intent is strengthened, not
    weakened.)
    """

    database_path, _, _ = page_env
    text = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    # SSR still stands on its own without any script running.
    assert '<form method="get" action="/v1/dashboard">' in text
    assert 'type="date"' in text
    assert 'required max="2026-07-20"' in text
    assert '<a class="tab" href="/v1/dashboard?' in text
    assert '<a href="/prompts/' in text
    # The one script is external + deferred; no inline body, no inline handlers.
    assert '<script src="/dashboard.js" defer></script>' in text
    assert_csp_safe(text, context="overview")
    # No client routing / history manipulation was introduced.
    assert "history." not in text.lower()


def test_pages_have_no_external_hosts_and_server_supplies_strict_csp(page_env):
    database_path, _, _ = page_env
    response = _response(database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    text = response.body.decode().lower()
    assert "http://" not in text
    assert "https://" not in text
    source = Path(server.__file__).read_text()
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src data:",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in source


def test_overview_uses_csp_safe_svg_charts_theme_aware_and_full_width(page_env):
    database_path, _, _ = page_env
    text = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    css = pages.stylesheet()
    assert "<title>API換算コスト の推移</title>" in text
    assert "<title>費目別コスト構成</title>" in text
    assert 'class="metric-bar"' in text
    assert "style=" not in text
    # The dashboard is theme-aware: charts must read correctly on light and dark.
    assert "color-scheme: light dark" in css
    assert "prefers-color-scheme: dark" in css
    assert ':root[data-theme="dark"]' in css and ':root[data-theme="light"]' in css
    # Chart colours resolve through custom properties defined for both themes.
    assert "--ch-axis: #5c6472" in css and "--ch-axis: #7d8899" in css
    assert "--ch-avg: #16181d" in css and "--ch-avg: #ffffff" in css
    assert "max-width: 96rem" not in css
    assert "body { margin: 0; width: 100%" in css


def test_overview_daily_context_chart_highlights_selection(page_env):
    """Test 6: the daily context chart is an SVG that marks the selected range via
    CSS classes (band + edges + brighter bars), with no inline style or script."""
    database_path, _, _ = page_env
    text = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    assert '<svg class="chart"' in text
    assert 'class="ch-selband"' in text
    assert 'class="ch-seledge"' in text
    assert 'class="ch-day-sel"' in text
    assert "style=" not in text
    assert_csp_safe(text, context="overview daily-context")
    # The highlight colours resolve through custom properties defined for both themes.
    css = pages.stylesheet()
    assert "--ch-day-sel: #1b4fd8" in css and "--ch-day-sel: #7aa2f7" in css
    assert "--ch-sel-band: #1b4fd8" in css and "--ch-sel-band: #7aa2f7" in css


def test_chart_svg_carries_no_hardcoded_theme_colours(page_env):
    """Structural chart colours must come from CSS, not baked-in dark hexes."""

    database_path, _, _ = page_env
    for view in ("overview", "period", "trend", "cache", "dist"):
        text = _response(
            database_path, f"view={view}&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
        ).body.decode()
        for dark_only in ('stroke="#2d3648"', 'fill="#7d8899"', 'fill="#d6dde8"', 'stroke="#fff"'):
            assert dark_only not in text, f"{view} still hardcodes {dark_only}"


def test_every_chart_node_kind_renders_svg_not_a_numeric_table():
    """Test 1: all four chart kinds must reach the SVG renderer in the dashboard.

    Before the shared-renderer change every one of these was degraded to a
    numeric table by ``_series_table`` and produced no ``<svg>`` at all.
    """

    labels = [FIXTURE_DAY - dt.timedelta(days=index) for index in (2, 1, 0)]
    values = [1.0, 2.0, 3.0]
    colors = {"費用": "#7aa2f7"}
    kinds = {
        "stacked_bars": node("stacked_bars", labels, {"費用": values}, colors),
        "line_chart": node(
            "line_chart", labels, {"費用": values}, colors, "", money_axis=True, grain="daily"
        ),
        "volume_chart": node(
            "volume_chart",
            labels,
            {"費用": values},
            colors,
            None,
            "daily",
            0,
            1,
            [],
            [],
        ),
        "cache_balance": node("cache_balance", labels, values, values, values),
    }
    for kind, chart in kinds.items():
        text = pages._node(chart)
        assert '<svg class="chart"' in text, f"{kind} did not render a chart svg"
        assert "<title>" in text, f"{kind} lost its native tooltip title"


def _all_dashboard_markup(database_path) -> dict[str, str]:
    return {
        view: _response(
            database_path, f"view={view}&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
        ).body.decode()
        for view in ("overview", "period", "trend", "cache", "dist")
    }


def test_sortable_table_emits_data_sort_and_aria_sort_without_changing_display():
    """Feature A: the table node path threads Column.sortable / Cell.sort into the
    markup as attributes only — sortable <th> become keyboard-operable and carry
    aria-sort, each <td> carries its raw orderable key in data-sort, and the
    displayed, formatted text is byte-for-byte the same as before.
    """

    columns = (
        Column("原因", sortable=True),
        Column("金額 ▼", sortable=True, sort_dir="desc"),
        Column("時刻"),
    )
    rows = [
        Row(
            [
                Cell("rebuild", sort="rebuild"),
                Cell("$1,702.00", sort=1702.0),
                Cell("12:00"),
            ]
        )
    ]
    text = pages._node(node("table", columns, rows))
    # Sortable headers are focusable and expose their current sort to assistive tech.
    assert 'data-sortable="" tabindex="0" aria-sort="none">原因</th>' in text
    assert (
        'data-sortable="" data-dir="desc" tabindex="0" aria-sort="descending">金額 ▼</th>'
        in text
    )
    # A non-sortable column stays a plain, inert header.
    assert '<th scope="col">時刻</th>' in text
    # The raw key rides in data-sort; the visible cell text is unchanged.
    assert '<td data-sort="rebuild">rebuild</td>' in text
    assert '<td data-sort="1702.0">$1,702.00</td>' in text
    # A cell with no orderable key emits no data-sort attribute at all.
    assert "<td>12:00</td>" in text


def test_sortable_refactor_preserves_interactive_row_collapse():
    """Threading data-sort through the table path must not weaken the existing
    "X（対話のみ）" row-collapse: when an interactive row matches its predecessor on
    every other cell, the two still merge into one annotated row.
    """

    columns = (Column("区分"), Column("金額", sortable=True))
    rows = [
        Row([Cell("全体", sort="全体"), Cell("$1.00", sort=1.0)]),
        Row([Cell("全体（対話のみ）", sort="全体"), Cell("$1.00", sort=1.0)]),
    ]
    text = pages._node(node("table", columns, rows))
    # The two rows collapse to one, annotated with the same-value note.
    assert text.count("<tbody><tr>") == 1
    assert text.count("<tr>") == 2  # header row + the single merged data row
    assert '<span class="same-note">（対話のみも同値）</span>' in text
    # The kept row still carries its own data-sort keys (attributes survived merge).
    assert '<td data-sort="全体">' in text
    assert '<td data-sort="1.0">$1.00</td>' in text


def test_cache_view_marks_ranking_headers_sortable_in_dashboard(page_env):
    """Feature A integration: the cache view's ranking table (the one whose model
    marks columns sortable, exactly as the static renderer already does) reaches the
    dashboard with data-sortable / aria-sort headers and data-sort cells.
    """

    database_path, _, _ = page_env
    text = _response(
        database_path, f"view=cache&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    assert 'data-sortable=""' in text
    assert 'aria-sort="descending"' in text
    assert 'tabindex="0"' in text
    assert_csp_safe(text, context="cache")


def test_chart_svg_marks_carry_hover_data_attributes(page_env):
    """Feature B: chart marks carry the data-* attributes the hover code reads, so
    emphasis / crosshair / readout are driven from the DOM, not a duplicated JSON
    blob. With JS off these attributes are inert and the native <title> stays.
    """

    database_path, _, _ = page_env
    # The overview daily-context bars and cost-parts segments both carry them.
    overview = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    for attribute in ("data-series=", "data-label=", "data-value="):
        assert attribute in overview, f"overview chart lacks {attribute}"
    # And the chart-bearing legacy views do too (stacked bars / line / cache).
    for view in ("trend", "cache"):
        text = _response(
            database_path, f"view={view}&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
        ).body.decode()
        assert 'data-series="' in text, f"{view} chart lacks data-series"
        assert 'data-value="' in text, f"{view} chart lacks data-value"


def test_csp_safety_gate_is_not_vacuous():
    """The assert_csp_safe gate must reject a real inline handler and a real inline
    script body — otherwise tests 2 / 5 would pass no matter what.
    """

    good = (
        '<!doctype html><head><script src="/dashboard.js" defer></script></head>'
        "<body><table><th>x</th></table></body>"
    )
    assert_csp_safe(good)  # the admissible shape passes
    with pytest.raises(AssertionError):
        assert_csp_safe(good.replace("<th>x</th>", '<th onclick="sort()">x</th>'))
    with pytest.raises(AssertionError):
        assert_csp_safe(good.replace("</body>", "<script>evil()</script></body>"))
    # Escaped attacker data containing the substring "onerror=" must NOT trip it:
    # html.escape turns the quote into &quot;, so no genuine handler syntax remains.
    assert_csp_safe(good.replace("x</th>", "onerror=alert(1)&gt;</th>"))


def test_no_view_emits_inline_style_or_inline_script_or_handler(page_env):
    """Test 2: the browser-free CSP gate across all five views.

    ``style-src 'self'`` with no ``'unsafe-inline'`` means an inline ``style=``
    attribute is silently dropped by the browser; ``script-src 'self'`` means an
    inline ``<script>`` body and any inline ``on*=`` handler never run. The only
    admissible script is the served, deferred /dashboard.js.
    """

    database_path, _, _ = page_env
    for view, text in _all_dashboard_markup(database_path).items():
        assert "style=" not in text, f"{view} emits a CSP-blocked inline style"
        assert '<script src="/dashboard.js" defer></script>' in text, (
            f"{view} lost the progressive-enhancement script"
        )
        assert_csp_safe(text, context=view)


def test_bar_cells_render_visibly_and_encode_the_right_magnitude():
    """Test 3: bar cells are real geometry, not an inline width style."""

    for ratio in (0.0, 0.25, 0.5, 1.0):
        text = pages._cell(Cell("$1.00", bar=ratio))
        assert "style=" not in text
        assert 'class="bar-track"' in text and 'class="bar-fill"' in text
        # The fill width is the magnitude, on a 0..100 viewBox.
        assert f'class="bar-fill" width="{ratio * 100:.2f}"' in text

    # A themed class, not a hardcoded colour, so the bar is visible in both themes.
    css = pages.stylesheet()
    assert ".bar-fill{fill:var(--ch-bar-fill" in css
    assert "--ch-bar-fill: #1b4fd8" in css and "--ch-bar-fill: #7aa2f7" in css


def test_chart_nodes_render_svg_with_validated_legends_and_ignore_page_offset():
    labels = [
        FIXTURE_DAY - dt.timedelta(days=2),
        FIXTURE_DAY - dt.timedelta(days=1),
        FIXTURE_DAY,
    ]
    chart = node(
        "line_chart",
        labels,
        {"費用": [1.0, 2.0, 3.0]},
        {"費用": "#7aa2f7"},
        "",
        money_axis=True,
        grain="daily",
    )
    text = pages._node(chart, Page(limit=1, page=3))
    assert "<svg " in text and text.count("<circle ") == 3
    assert all(day.strftime("%m-%d") in text for day in labels)
    assert text.count("<tbody><tr>") == 1 and text.count("<tr>") == 4
    assert "<details" in text

    legend = pages._node(node("legend", [("費用", "#7aa2f7")]))
    assert "<svg " in legend and 'fill="#7aa2f7"' in legend
    with pytest.raises(ValueError):
        pages._node(node("legend", [("危険", "red; background:url(x)")]))


def test_cell_bar_and_dot_are_inline_svg_without_inline_style():
    text = pages._cell(Cell("$1.00", bar=0.5, dot="#2dd4bf"))
    assert text.count("<svg ") == 2
    assert 'width="50.00"' in text and 'fill="#2dd4bf"' in text
    assert "style=" not in text
    with pytest.raises(ValueError):
        pages._cell(Cell("x", dot="url(evil)"))


def test_trend_and_cache_restore_existing_svg_primitives(page_env):
    database_path, _, _ = page_env
    trend_text = _response(
        database_path, f"view=trend&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    cache_text = _response(
        database_path, f"view=cache&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    assert "期間別コストと施策・外生イベント" in trend_text
    assert " の推移</title>" in trend_text
    assert "期間別件数の積み上げ棒グラフ" in cache_text
    assert "キャッシュ読み書き費用と1時間キャッシュ比率" in cache_text
    assert "<details" in trend_text and "<details" in cache_text


def test_accessibility_has_text_thresholds_keyboard_narrow_and_reduced_motion(page_env):
    database_path, _, _ = page_env
    response = _response(database_path, f"view=dist&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    text = response.body.decode()
    css = pages.stylesheet()
    assert "≥200k" in text
    assert "件数シェア" in text and "コストシェア" in text
    assert "focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "max-width: 40rem" in css
    assert "overflow-x: auto" in css
    assert_csp_safe(text, context="dist")


@pytest.mark.parametrize(
    "kind", ["loading", "empty", "initial_sync", "busy", "unavailable", "not_found"]
)
def test_state_pages_keep_the_same_five_view_navigation(kind):
    text = pages.state_page(kind)
    for view in ("overview", "period", "trend", "cache", "dist"):
        assert f"view={view}&amp;range=yesterday" in text


def test_dashboard_table_pagination_and_response_size_are_bounded(page_env, monkeypatch):
    rows = tuple(Row([Cell(str(index))]) for index in range(250))
    model = LegacyViewModel(
        "bounded",
        "period",
        "total",
        node("table", (Column("row"),), rows, foot=None),
        "UTC",
    )
    request = routes.DashboardRequest(
        "trend",
        Window(FIXTURE_DAY, FIXTURE_DAY, None, str(FIXTURE_DAY)),
        Page(),
        "custom",
    )
    default_html = pages.dashboard_page(request, model, FIXTURE_DAY)
    assert default_html.count("<tbody><tr>") == 1
    assert default_html.count("<tr>") == 41

    request_200 = routes.DashboardRequest(
        "trend",
        request.window,
        Page(limit=200),
        "custom",
    )
    assert pages.dashboard_page(request_200, model, FIXTURE_DAY).count("<tr>") == 201

    database_path, _, _ = page_env
    monkeypatch.setattr(pages, "dashboard_page", lambda *_args: "x" * 1_000_000)
    response = _response(database_path, f"view=trend&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    assert response.status == 503
    assert len(response.body) < 1_000_000


def test_unknown_view_is_rejected_by_the_five_view_allowlist(page_env):
    database_path, _, _ = page_env
    response = _response(database_path, f"view=trace&from={FIXTURE_DAY}&to={FIXTURE_DAY}")
    assert response.status == 400


def test_pages_is_the_only_dashboard_module_that_emits_markup():
    root = Path(pages.__file__).parent
    markup = re.compile(r"<\s*(?:!doctype|/?[a-zA-Z][a-zA-Z0-9-]*)(?:\s|/?>)")
    for path in root.glob("*.py"):
        if path.name == "pages.py":
            continue
        tree = ast.parse(path.read_text())
        literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        assert not any(markup.search(value) for value in literals), path.name


def test_dashboard_serve_open_passes_nonce_directly_to_browser(monkeypatch, capsys, caplog):
    nonce = "bootstrap-secret-nonce"
    opened = []
    fake_server = SimpleNamespace(
        port=49123,
        auth=SimpleNamespace(issue_bootstrap_nonce=lambda: nonce),
    )

    def fake_serve(**kwargs):
        kwargs["on_started"](fake_server)

    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert cli.main(["dashboard", "serve", "--open"]) == 0
    assert opened == [f"http://127.0.0.1:49123/bootstrap?nonce={nonce}"]
    captured = capsys.readouterr()
    assert nonce not in captured.out + captured.err + caplog.text
