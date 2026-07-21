from __future__ import annotations

import ast
import datetime as dt
import re
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

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
        "/dashboard?view=overview&from=2026-07-20&to=2026-07-20"
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


def test_page_is_no_javascript_get_form_and_real_links(page_env):
    database_path, _, _ = page_env
    text = _response(
        database_path, f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    ).body.decode()
    assert '<form method="get" action="/dashboard">' in text
    assert 'type="date"' in text
    assert 'required max="2026-07-20"' in text
    assert '<a class="tab" href="/dashboard?' in text
    assert '<a href="/prompts/' in text
    assert "<script" not in text.lower()
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
    assert "<script" not in text.lower()


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
