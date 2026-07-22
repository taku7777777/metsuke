"""Tests for the v2 dashboard — a CLIENT-RENDERED TypeScript/Preact app.

v2's presentation is a browser bundle (source in ``frontend/``, built output committed to
``dashboard2/assets/``). These Python tests cover only the *server* contract, which is all
the Python runtime owns: the data-free HTML shell, the committed static assets, and the
``/v2/api/overview`` JSON — all fronted by the SAME security as v1. They deliberately do NOT
invoke npm and do NOT prove the UI renders/looks right; that is the browser-free jsdom gate
(``frontend/test/render.test.mjs``) and, for appearance, the user's own eyes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from contextlib import closing
from pathlib import Path

import pytest

from metsuke import ledger
from metsuke.dashboard import server
from metsuke.dashboard.db import connect_dashboard
from metsuke.dashboard2 import web
from metsuke.viewmodel import cache, dist, overview, period, trend
from metsuke.viewmodel.common import Page, Window, to_jsonable
from test_dashboard_auth import (  # noqa: F401  (helpers + fixture deps reused)
    MutableClock,
    _bootstrap,
    _headers,
    _request,
    _seed_dashboard_data,
    _wait_until_healthy,
)

TODAY = dt.date(2027, 1, 15)
SEED_DAY = dt.date(2027, 1, 14)  # _seed_dashboard_data stamps clock.value - 86400
ASSETS = Path(server.__file__).resolve().parent.parent / "dashboard2" / "assets"

_SCRIPT_OPEN = re.compile(rb"<script[^>]*>", re.IGNORECASE)
_INLINE_HANDLER = re.compile(rb'\son\w+="', re.IGNORECASE)
_SHELL_SCRIPT = b'<script src="/v2/app.js" defer>'


def _assert_shell_csp_safe(body: bytes) -> None:
    """The shell carries no inline style, exactly one script (the external app.js), no on*=."""

    assert b"style=" not in body, "shell emits a CSP-blocked inline style"
    scripts = _SCRIPT_OPEN.findall(body)
    assert scripts == [_SHELL_SCRIPT], f"unexpected <script> tags: {scripts!r}"
    assert _INLINE_HANDLER.search(body) is None, "shell emits an inline on*= handler"


@pytest.fixture
def v2_server(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    clock = MutableClock()
    state_path = tmp_path / "state" / "dashboard-state.json"
    secret_path = tmp_path / "state" / "dashboard-secret"
    database_path = tmp_path / "ledger.db"
    connection = ledger.connect(database_path)
    _seed_dashboard_data(connection, clock.value - 86400)
    connection.close()
    dashboard = server.create_server(
        port=0,
        state_path=state_path,
        secret_path=secret_path,
        clock=clock,
        database_path=database_path,
        today=lambda: TODAY,
    )
    thread = threading.Thread(target=dashboard.serve_forever)
    thread.start()
    try:
        _wait_until_healthy(dashboard.port)
        yield dashboard
    finally:
        dashboard.shutdown()
        thread.join(timeout=3)
        dashboard.close_lifecycle()
        assert not thread.is_alive()


# --- committed assets exist (built by `npm run build`, NOT by this suite) ----------


def test_committed_client_bundle_exists_on_disk():
    for name in ("app.js", "app.css"):
        path = ASSETS / name
        assert path.is_file(), f"missing committed asset {path}; run `cd frontend && npm run build`"
        assert path.stat().st_size > 0


# --- static assets: served without auth, correct content-types --------------------


def test_v2_app_js_and_css_served_without_auth_with_correct_types(v2_server):
    js_status, js_body, js_headers = _request(v2_server.port, "/v2/app.js")
    assert js_status == 200
    assert _headers(js_headers)["Content-Type"] == "text/javascript; charset=utf-8"
    assert js_body == (ASSETS / "app.js").read_bytes()

    css_status, css_body, css_headers = _request(v2_server.port, "/v2/app.css")
    assert css_status == 200
    assert _headers(css_headers)["Content-Type"] == "text/css; charset=utf-8"
    assert css_body == (ASSETS / "app.css").read_bytes()


# --- shell: authenticated, data-free, CSP-safe, links the app bundle --------------


def test_v2_dashboard_shell_requires_auth_like_v1(v2_server):
    status, body, _ = _request(v2_server.port, "/v2/dashboard")
    assert status == 401
    v1_status, v1_body, _ = _request(v2_server.port, "/dashboard")
    assert (status, body) == (v1_status, v1_body)


def test_v2_dashboard_shell_links_bundle_and_is_csp_safe(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    status, body, headers = _request(
        v2_server.port, "/v2/dashboard", headers={"Cookie": cookie}
    )
    assert status == 200
    assert _headers(headers)["Content-Type"] == "text/html; charset=utf-8"
    assert "X-CSRF-Token" in _headers(headers)
    text = body.decode()
    assert '<div id="app"></div>' in text
    assert '<link rel="stylesheet" href="/v2/app.css">' in text
    assert '<script src="/v2/app.js" defer></script>' in text
    # The shell is data-free: it must be byte-identical to the static template.
    assert body == web.shell_html().encode()
    _assert_shell_csp_safe(body)


# --- API: authenticated JSON that matches overview.query --------------------------


def test_v2_api_overview_requires_auth(v2_server):
    status, _, _ = _request(
        v2_server.port, "/v2/api/overview?view=overview&from=2027-01-14&to=2027-01-14"
    )
    assert status == 401


def test_v2_api_overview_returns_json_matching_query(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    path = "/v2/api/overview?view=overview&from=2027-01-14&to=2027-01-14"
    status, body, headers = _request(v2_server.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert _headers(headers)["Cache-Control"] == "no-store"
    payload = json.loads(body)

    # The serialized model equals a direct overview.query with the same window/today.
    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = overview.query(conn, window, Page(), today=TODAY)
    assert payload["model"] == to_jsonable(model)

    # Request metadata echoes the resolver, including the canonical query for the client.
    assert payload["request"]["view"] == "overview"
    assert payload["request"]["from"] == "2027-01-14"
    assert payload["request"]["to"] == "2027-01-14"
    assert payload["request"]["canonical_query"] == (
        "view=overview&from=2027-01-14&to=2027-01-14"
    )
    assert len(payload["model"]["daily_costs"]) == 31
    assert len(payload["model"]["cost_parts"]) == 6


def test_v2_api_overview_bare_query_resolves_without_redirect(v2_server):
    """Bare query is resolved server-side (no 303); the client canonicalizes its own URL."""

    _, cookie, _ = _bootstrap(v2_server)
    status, body, _ = _request(v2_server.port, "/v2/api/overview", headers={"Cookie": cookie})
    assert status == 200
    payload = json.loads(body)
    # Default preset is "yesterday" -> 2027-01-14 relative to today 2027-01-15.
    assert payload["request"]["preset"] == "yesterday"
    assert payload["request"]["canonical_query"].startswith("view=overview&from=")


def test_v2_api_overview_rejects_bad_query_as_json(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    status, body, headers = _request(
        v2_server.port, "/v2/api/overview?view=nonsense", headers={"Cookie": cookie}
    )
    assert status == 400
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"error": "bad_request"}


# --- serializer purity: web.overview_json is exactly to_jsonable(model) -----------


def test_overview_json_is_pure_transcription_of_the_model(v2_server):
    from metsuke.dashboard.routes import DashboardRequest

    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = overview.query(conn, window, Page(), today=TODAY)
    request = DashboardRequest("overview", window, Page(), "custom")
    payload = json.loads(web.overview_json(request, model, None))
    assert payload["model"] == to_jsonable(model)


# --- period / dist API: same security + serializer purity as overview -------------


def test_v2_api_period_requires_auth(v2_server):
    status, _, _ = _request(
        v2_server.port, "/v2/api/period?view=period&from=2027-01-14&to=2027-01-14"
    )
    assert status == 401


def test_v2_api_dist_requires_auth(v2_server):
    status, _, _ = _request(
        v2_server.port, "/v2/api/dist?view=dist&from=2027-01-14&to=2027-01-14"
    )
    assert status == 401


def test_v2_api_period_returns_json_matching_query(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    path = "/v2/api/period?view=period&from=2027-01-14&to=2027-01-14"
    status, body, headers = _request(v2_server.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert _headers(headers)["Cache-Control"] == "no-store"
    payload = json.loads(body)

    # The serialized node tree equals a direct period.query with the same window + page.
    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = period.query(conn, window, Page())
    assert payload["model"] == to_jsonable(model)
    assert payload["request"]["view"] == "period"
    assert payload["request"]["canonical_query"] == (
        "view=period&from=2027-01-14&to=2027-01-14"
    )
    # A node tree, not overview's typed DTOs: it carries a body + title.
    assert payload["model"]["body"]["kind"] in {"join", "card"}
    assert payload["model"]["title"]


def test_v2_api_dist_returns_json_matching_query(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    path = "/v2/api/dist?view=dist&from=2027-01-14&to=2027-01-14"
    status, body, headers = _request(v2_server.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(body)

    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = dist.query(conn, window)
    assert payload["model"] == to_jsonable(model)
    assert payload["request"]["view"] == "dist"
    assert payload["model"]["body"]["kind"] in {"join", "card"}


def test_v2_api_period_rejects_bad_query_as_json(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    status, body, headers = _request(
        v2_server.port, "/v2/api/period?view=nonsense", headers={"Cookie": cookie}
    )
    assert status == 400
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"error": "bad_request"}


# --- trend / cache API: same security + serializer purity as period/dist ----------


def test_v2_api_trend_requires_auth(v2_server):
    status, _, _ = _request(
        v2_server.port, "/v2/api/trend?view=trend&from=2027-01-14&to=2027-01-14"
    )
    assert status == 401


def test_v2_api_cache_requires_auth(v2_server):
    status, _, _ = _request(
        v2_server.port, "/v2/api/cache?view=cache&from=2027-01-14&to=2027-01-14"
    )
    assert status == 401


def test_v2_api_trend_returns_json_matching_query(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    path = "/v2/api/trend?view=trend&from=2027-01-14&to=2027-01-14"
    status, body, headers = _request(v2_server.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert _headers(headers)["Cache-Control"] == "no-store"
    payload = json.loads(body)

    # The serialized node tree equals a direct trend.query with the same window (no page).
    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = trend.query(conn, window)
    assert payload["model"] == to_jsonable(model)
    assert payload["request"]["view"] == "trend"
    assert payload["request"]["canonical_query"] == (
        "view=trend&from=2027-01-14&to=2027-01-14"
    )
    assert payload["model"]["body"]["kind"] in {"join", "card"}
    assert payload["model"]["title"]


def test_v2_api_cache_returns_json_matching_query(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    path = "/v2/api/cache?view=cache&from=2027-01-14&to=2027-01-14"
    status, body, headers = _request(v2_server.port, path, headers={"Cookie": cookie})
    assert status == 200
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(body)

    window = Window(SEED_DAY, SEED_DAY, None, f"{SEED_DAY} — {SEED_DAY}")
    with closing(connect_dashboard(v2_server.database_path)) as conn:
        model = cache.query(conn, window)
    assert payload["model"] == to_jsonable(model)
    assert payload["request"]["view"] == "cache"
    assert payload["model"]["body"]["kind"] in {"join", "card"}


def test_v2_api_trend_rejects_bad_query_as_json(v2_server):
    _, cookie, _ = _bootstrap(v2_server)
    status, body, headers = _request(
        v2_server.port, "/v2/api/trend?view=nonsense", headers={"Cookie": cookie}
    )
    assert status == 400
    assert _headers(headers)["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"error": "bad_request"}
