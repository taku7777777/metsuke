from __future__ import annotations

import datetime as dt
import sqlite3
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from conftest import assert_csp_safe
from metsuke.dashboard import pages, routes
from metsuke.viewmodel import prompt, session
from test_views import FIXTURE_DAY, view_env as shared_view_env  # noqa: F401

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROMPT_ID = "22222222-2222-4222-8222-222222222222"
REQUEST_ID = "33333333-3333-4333-8333-333333333333"
UNKNOWN_REQUEST_ID = "44444444-4444-4444-8444-444444444444"
STAMP = dt.datetime(2026, 7, 20, 14, tzinfo=dt.UTC).timestamp()


@pytest.fixture
def detail_env(request):
    home, conn, _ = request.getfixturevalue("shared_view_env")
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        (SESSION_ID, "detail-project", STAMP, STAMP + 1),
    )
    conn.execute(
        "INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES (?,?,?,?)",
        (PROMPT_ID, SESSION_ID, STAMP, "detail <script>alert(1)</script>"),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,?,?,?,?,'claude-sonnet-5',1200,34,500,20,10,0,0,'transcript')""",
        (REQUEST_ID, SESSION_ID, SESSION_ID, PROMPT_ID, STAMP),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,is_interrupted,source)
           VALUES (?,?,?,?,?,'not-yet-priced',10,2,0,0,0,0,0,'transcript')""",
        (UNKNOWN_REQUEST_ID, SESSION_ID, SESSION_ID, PROMPT_ID, STAMP + 1),
    )
    conn.execute(
        "INSERT INTO ingest_log(ts,manifest_pos,segments,records,quarantined,parser_version) VALUES (?,?,?,?,?,?)",
        (STAMP, 1, 1, 1, 0, 1),
    )
    conn.commit()
    return home / "ledger.db", conn


def _detail(path: Path, target: str, *, now: float = STAMP + 10):
    return routes.detail_response(target, path, now=now)


def test_prompt_detail_uses_explain_model_and_escapes_ledger_text(detail_env):
    database_path, conn = detail_env
    model = prompt.query(conn, PROMPT_ID)
    assert model is not None
    response = _detail(database_path, f"/prompts/{PROMPT_ID}")
    text = response.body.decode()
    assert response.status == 200
    assert model.amount.display in text
    assert str(len(model.requests)) in text
    assert model.dominant.term in text
    assert f"{model.dominant.share_pct:.0f}%" in text
    for item in model.requests:
        assert item.amount.display in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>alert(1)</script>" not in text


def test_session_detail_uses_shared_model_and_lists_prompt_links(detail_env):
    database_path, conn = detail_env
    model = session.query(conn, SESSION_ID)
    assert model is not None
    response = _detail(database_path, f"/sessions/{SESSION_ID}")
    text = response.body.decode()
    assert response.status == 200
    assert model.amount.display in text
    assert str(model.request_count) in text
    assert f'href="/prompts/{PROMPT_ID}"' in text
    assert "detail &lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_period_prefix_link_canonicalizes_to_full_id_and_reaches_detail(detail_env):
    database_path, _ = detail_env
    query = f"view=period&from={FIXTURE_DAY}&to={FIXTURE_DAY}"
    listing = routes.dashboard_response(query, database_path, FIXTURE_DAY, now=STAMP + 10)
    text = listing.body.decode()
    prefix_path = f"/prompts/{PROMPT_ID[:8]}"
    assert f'href="{prefix_path}"' in text

    redirect = _detail(database_path, prefix_path)
    assert redirect.status == 303
    assert redirect.headers["Location"] == f"/prompts/{PROMPT_ID}"
    detail = _detail(database_path, redirect.headers["Location"])
    assert detail.status == 200
    assert "prompt詳細" in detail.body.decode()


def test_overview_links_and_native_back_contract_keep_window_out_of_detail_url(detail_env):
    database_path, _ = detail_env
    dashboard_url = f"/dashboard?view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}&project=detail-project"
    listing = routes.dashboard_response(
        urlsplit(dashboard_url).query,
        database_path,
        FIXTURE_DAY,
        now=STAMP + 10,
    )
    text = listing.body.decode()
    assert f'href="/prompts/{PROMPT_ID}"' in text
    assert f'href="/sessions/{SESSION_ID}"' in text
    assert "from=" not in f"/prompts/{PROMPT_ID}"
    assert "from=" not in f"/sessions/{SESSION_ID}"
    assert "history." not in text.lower()
    # The browser history retains dashboard_url; details carry no return-state copy.
    assert "project=detail-project" in dashboard_url


@pytest.mark.parametrize(
    "target",
    [
        "/prompts/../../etc/passwd",
        "/prompts/%2e%2e%2fetc%2fpasswd",
        "/prompts/short",
        "/prompts/contains.dot",
        "/sessions/%ZZ",
        f"/prompts/{PROMPT_ID}?from=2026-07-20",
    ],
)
def test_invalid_ids_and_path_traversal_are_rejected_before_filesystem_access(
    tmp_path, monkeypatch, target
):
    def unexpected_connect(_path):
        raise AssertionError("invalid URL reached the database/filesystem boundary")

    monkeypatch.setattr(routes, "connect_dashboard", unexpected_connect)
    response = routes.detail_response(target, tmp_path / "must-not-be-opened.db")
    assert response.status == 404
    assert "dashboardへ戻る" in response.body.decode()


def test_missing_and_ambiguous_ids_have_safe_404_with_return_link(detail_env):
    database_path, _ = detail_env
    missing = _detail(database_path, "/prompts/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert missing.status == 404
    text = missing.body.decode()
    assert "見つかりません" in text
    assert 'href="/dashboard"' in text


def test_absent_ledger_is_initial_sync_not_zero_dashboard(tmp_path):
    missing = tmp_path / "ledger.db"
    response = routes.dashboard_response(
        f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}",
        missing,
        FIXTURE_DAY,
    )
    text = response.body.decode()
    assert response.status == 503
    assert "初期同期" in text
    assert "metsuke sync" in text
    assert "$0.00" not in text


def test_stale_warning_keeps_past_data_readable(detail_env):
    database_path, _ = detail_env
    response = routes.dashboard_response(
        f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}",
        database_path,
        FIXTURE_DAY,
        now=STAMP + routes.STALE_AFTER_SECONDS + 1,
    )
    text = response.body.decode()
    assert response.status == 200
    assert "取込が遅れています" in text
    assert "最終正常取込" in text
    assert "detail &lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_unknown_cost_is_counted_and_explicitly_marks_totals_incomplete(detail_env):
    database_path, _ = detail_env
    prompt_response = _detail(database_path, f"/prompts/{PROMPT_ID}")
    overview_response = routes.dashboard_response(
        f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}",
        database_path,
        FIXTURE_DAY,
        now=STAMP + 10,
    )
    projection_responses = [
        routes.dashboard_response(
            f"view={view}&from={FIXTURE_DAY}&to={FIXTURE_DAY}",
            database_path,
            FIXTURE_DAY,
            now=STAMP + 10,
        )
        for view in ("period", "trend", "cache", "dist")
    ]
    for response in (prompt_response, overview_response, *projection_responses):
        text = response.body.decode()
        assert "未知価格" in text or "価格カバレッジ不足" in text
        assert "不完全" in text


def test_busy_is_safe_503_with_retry_and_private_diagnostic(detail_env, tmp_path):
    database_path, fixture_connection = detail_env
    fixture_connection.close()
    writer = sqlite3.connect(database_path)
    assert writer.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] == "exclusive"
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("UPDATE session SET project=project WHERE session_id=?", (SESSION_ID,))
    diagnostic = tmp_path / "state" / "dashboard-errors.log"
    try:
        response = routes.dashboard_response(
            f"view=overview&from={FIXTURE_DAY}&to={FIXTURE_DAY}",
            database_path,
            FIXTURE_DAY,
            now=STAMP + 10,
            diagnostic_path=diagnostic,
        )
    finally:
        writer.rollback()
        writer.close()
    text = response.body.decode()
    assert response.status == 503
    assert "再試行" in text
    assert "SELECT" not in text
    assert str(database_path) not in text
    assert diagnostic.read_text().strip().endswith("ledger_busy")
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert str(database_path) not in diagnostic.read_text()


def test_port_conflict_state_copy_matches_doctor_guidance():
    text = pages.state_page("port_conflict").lower()
    assert "port" in text
    assert "別サービスへは接続していません" in text
    assert "metsuke doctor" in text


def test_detail_pages_emit_no_inline_style_or_script(detail_env):
    """Test 2 (detail half): the CSP gate also covers prompt/session detail pages.

    ``style-src 'self'`` has no ``'unsafe-inline'``, so an inline ``style=``
    attribute would simply never apply in the browser.
    """

    database_path, _ = detail_env
    for target in (f"/prompts/{PROMPT_ID}", f"/sessions/{SESSION_ID}"):
        text = _detail(database_path, target).body.decode()
        assert "style=" not in text, f"{target} emits a CSP-blocked inline style"
        # Detail pages share _shell, so they carry the same one deferred script and
        # no inline script body or handler.
        assert_csp_safe(text, context=target)
