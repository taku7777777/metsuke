import ast
import hashlib
import json
import re
from dataclasses import FrozenInstanceError
from importlib import resources
from pathlib import Path

import pytest

from metsuke.viewmodel import Page, Window, count_cost_bearing_prompts, to_jsonable
from metsuke.viewmodel import cache, dist, overview, period, prompt, session, trend
from metsuke.project import display_name
from test_views import (  # noqa: F401
    FIXTURE_DAY,
    FIXTURE_WINDOW,
    PROJECT_B,
    view_env as shared_view_env,
)


MODEL_GOLDEN_SHA256 = {
    "period": "49a36abdec1f223e175a04c578c9c275ab19cea49633b9485d1ad2d0c61ef42e",
    "trend": "212455a6e90d8f7aea9ebed59353708a9bcd55c28e8cf10d93a2ff0f5136b91e",
    "cache": "298fe9acd27ef6b7467c9c9b1298fb8ac9f9c203de671abe10b8f39f820a5709",
    "dist": "bf03ffa3eb2c9a1aa7a683d6aae4bb10468714dfe167202e9d4703ba10b156b8",
}


@pytest.fixture
def model_env(request):
    return request.getfixturevalue("shared_view_env")


def _digest(model) -> str:
    payload = json.dumps(
        to_jsonable(model), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.parametrize(
    ("name", "query"),
    [("period", period.query), ("trend", trend.query), ("cache", cache.query), ("dist", dist.query)],
)
def test_legacy_viewmodel_golden_is_renderer_independent(model_env, name, query):
    _, conn, _ = model_env
    model = query(conn, FIXTURE_WINDOW)
    json.dumps(to_jsonable(model), ensure_ascii=False)
    assert _digest(model) == MODEL_GOLDEN_SHA256[name]


def _assert_viewmodel_source_is_pure(source: str, name: str) -> None:
    """Catch accidental boundary violations, not deliberately obfuscated Python code."""
    tag = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9-]*[\s/>]")
    forbidden_import_roots = {"os", "pathlib", "webbrowser", "subprocess"}
    forbidden_import_prefixes = ("metsuke.viewgen",)
    forbidden_calls = {
        "open",
        "mkdir",
        "open_browser",
        "unlink",
        "write_bytes",
        "write_text",
    }
    tree = ast.parse(source)
    assert not tag.search(source), f"viewmodel must not contain markup: {name}"

    def forbidden_import(module: str) -> bool:
        root = module.split(".")[0]
        return root in forbidden_import_roots or any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in forbidden_import_prefixes
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not forbidden_import(item.name) for item in node.names), (
                f"viewmodel forbidden import: {name}"
            )
        elif isinstance(node, ast.ImportFrom):
            assert not forbidden_import(node.module or ""), (
                f"viewmodel forbidden import: {name}"
            )
        elif isinstance(node, ast.Call):
            call_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            assert call_name not in forbidden_calls, f"viewmodel I/O call {call_name}: {name}"


def test_viewmodel_package_has_no_io_html_or_renderer_dependencies():
    root = resources.files("metsuke").joinpath("viewmodel")
    for path in root.iterdir():
        if not path.name.endswith(".py"):
            continue
        _assert_viewmodel_source_is_pure(path.read_text(), path.name)
    for source in (
        "import os.path",
        "from os.path import join",
        "import metsuke.viewgen.render",
        "from metsuke.viewgen import render",
    ):
        with pytest.raises(AssertionError, match="forbidden import"):
            _assert_viewmodel_source_is_pure(source, "injected.py")


def test_v1_to_v4_viewgen_modules_are_query_free_renderer_adapters():
    root = resources.files("metsuke").joinpath("viewgen")
    for name in ("v1_period.py", "v2_trend.py", "v3_cache.py", "v4_dist.py"):
        source = root.joinpath(name).read_text()
        assert ".execute(" not in source
        assert not re.search(r"\b(?:select|insert|update|delete)\b", source, re.IGNORECASE)
        assert "render_model(query(conn, window))" in source


def test_common_dtos_are_frozen_validated_and_json_serializable():
    window = Window(FIXTURE_DAY, FIXTURE_DAY, PROJECT_B, str(FIXTURE_DAY))
    page = Page(limit=40, page=2, sort="cost", order="desc")
    assert page.offset == 40
    assert to_jsonable(window)["start"] == str(FIXTURE_DAY)
    with pytest.raises(FrozenInstanceError):
        window.project = None
    for kwargs in (
        {"limit": 0},
        {"limit": 201},
        {"page": 0},
        {"order": "sideways"},
        {"sort": "cost; DROP TABLE request"},
    ):
        with pytest.raises(ValueError):
            Page(**kwargs)
    encoded_home = Path.home().as_posix().replace("/", "-")
    assert display_name(f"{encoded_home}-github-example") == "~github-example"
    assert display_name("-Users-someone-else-example") == "-Users-someone-else-example"
    assert display_name(None) == "—"


def test_prompt_kpi_single_definition_supports_window_and_project(model_env):
    _, conn, _ = model_env
    assert count_cost_bearing_prompts(conn) == 2
    assert count_cost_bearing_prompts(conn, FIXTURE_WINDOW) == 2
    selected = Window(FIXTURE_DAY, FIXTURE_DAY, PROJECT_B, str(FIXTURE_DAY))
    missing = Window(FIXTURE_DAY, FIXTURE_DAY, "missing-project", str(FIXTURE_DAY))
    outside = Window(FIXTURE_DAY.replace(day=19), FIXTURE_DAY.replace(day=19), None, "outside")
    assert count_cost_bearing_prompts(conn, selected) == 1
    assert count_cost_bearing_prompts(conn, missing) == 0
    assert count_cost_bearing_prompts(conn, outside) == 0


def test_overview_model_has_kpis_parts_rankings_and_previous_comparison(model_env):
    _, conn, _ = model_env
    model = overview.query(conn, FIXTURE_WINDOW)
    assert [item.name for item in model.kpis] == [
        "API換算コスト",
        "コスト発生prompt",
        "request",
        "session",
        "project",
    ]
    assert model.kpis[0].value == pytest.approx(0.946755)
    assert model.kpis[0].display == "$0.95"
    assert model.kpis[0].comparison.display == "比較不能"
    assert [part.name for part in model.cost_parts] == [
        "input",
        "output",
        "cache_read",
        "cache_w5m",
        "cache_w1h",
        "server_tool",
    ]
    assert [item.prompt_id for item in model.top_prompts] == ["p2", "p<!--canary"]
    assert [item.session_id for item in model.top_sessions] == ["s2", "s1"]
    assert model.unknown_cost_request_count == 0
    json.dumps(to_jsonable(model), ensure_ascii=False)


def test_prompt_and_session_models_match_numeric_detail_contract(model_env):
    _, conn, _ = model_env
    prompt_model = prompt.query(conn, "p2")
    assert prompt_model is not None
    assert prompt_model.amount.raw == pytest.approx(0.61617)
    assert prompt_model.amount.display == "$0.62"
    assert len(prompt_model.requests) == 1
    assert prompt_model.requests[0].amount.raw == pytest.approx(0.61617)
    assert prompt_model.dominant.term == "input"

    session_model = session.query(conn, "s2")
    assert session_model is not None
    assert session_model.amount.raw == pytest.approx(0.61617)
    assert session_model.request_count == 1
    assert [item.prompt_id for item in session_model.prompts] == ["p2"]
    assert json.loads(json.dumps(to_jsonable(session_model)))["amount"]["display"] == "$0.62"

    class IndexMissingRow:
        def __getitem__(self, key):
            raise IndexError(key)

    assert prompt.dominant_term([{}]).share_pct == 0
    assert prompt.dominant_term([IndexMissingRow()]).share_pct == 0


def test_dominant_term_definition_is_shared_by_prompt_and_period(model_env):
    split_cache_creation = {
        "cache_w5m_tok": 6,
        "cache_w5m_x": 1,
        "cache_w1h_tok": 6,
        "cache_w1h_x": 1,
        "output_tok": 10,
        "out_usd": 1,
        "in_usd": 1,
        "price_factor": 1,
        "cost_usd": 22 / 1e6,
    }
    server_tool = {
        "output_tok": 10,
        "out_usd": 1,
        "price_factor": 1,
        "server_tool_usd": 15 / 1e6,
        "cost_usd": 25 / 1e6,
    }
    assert prompt.dominant_term([split_cache_creation]).term == "cache_creation"
    assert prompt.dominant_term([server_tool]).term == "server_tool"

    _, conn, _ = model_env

    class RecordingConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.statements = []

        def execute(self, sql, parameters=()):
            self.statements.append(sql)
            return self.wrapped.execute(sql, parameters)

    recording = RecordingConnection(conn)
    period.query(recording, FIXTURE_WINDOW)
    assert len(recording.statements) == 1
    ranking_sql = recording.statements[0]
    for name in prompt.dominant_component_names():
        assert f"dominant_{name}" in ranking_sql
    assert "dominant_cache_w5m" not in ranking_sql
    assert "dominant_cache_w1h" not in ranking_sql
    assert not any("where prompt_id=?" in sql.lower() for sql in recording.statements)


def test_dashboard_page_uses_bound_limit_offset_without_changing_legacy_defaults(model_env):
    _, conn, _ = model_env

    class RecordingConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.calls = []

        def execute(self, sql, parameters=()):
            self.calls.append((sql, tuple(parameters)))
            return self.wrapped.execute(sql, parameters)

    legacy = RecordingConnection(conn)
    period.query(legacy, FIXTURE_WINDOW)
    assert len(legacy.calls) == 1
    legacy_sql = "\n".join(sql.lower() for sql, _ in legacy.calls)
    assert legacy_sql.count("from v_request_cost") == 1
    assert "order by cost desc,prompt_id asc limit 40" in legacy_sql
    assert "order by cost desc,session_id desc limit 30" in legacy_sql

    dashboard = RecordingConnection(conn)
    page = Page(limit=1, page=2)
    period.query(dashboard, FIXTURE_WINDOW, page)
    ranking_calls = [
        (sql, parameters)
        for sql, parameters in dashboard.calls
        if "limit ? offset ?" in sql.lower()
    ]
    assert len(ranking_calls) == 1
    assert ranking_calls[0][1][-6:] == (1, 1, 1, 1, 1, 1)
    assert "limit 1" not in ranking_calls[0][0].lower()

    overview_calls = RecordingConnection(conn)
    overview.query(overview_calls, FIXTURE_WINDOW, page)
    assert len(overview_calls.calls) == 1
    assert overview_calls.calls[0][0].lower().count("from v_request_cost") == 1
    ranking_calls = [
        (sql, parameters)
        for sql, parameters in overview_calls.calls
        if "limit ? offset ?" in sql.lower()
    ]
    assert len(ranking_calls) == 1
    assert ranking_calls[0][1][-4:] == (1, 1, 1, 1)
