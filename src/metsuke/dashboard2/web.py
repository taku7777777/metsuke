"""Server-side glue for the client-rendered v2 dashboard.

v2 is a client-rendered TypeScript/Preact app. Its source lives in the repo-root
``frontend/`` directory; the built bundle is committed to ``dashboard2/assets/`` (``app.js``
+ ``app.css``) and is what ships — end users run the uv-only Python runtime and never touch
node. This module is the *entire* Python surface of v2:

* :func:`shell_html` — the data-free HTML shell (``<div id="app">`` + external CSS/JS links).
  It is byte-identical for every authenticated request and carries no ledger data.
* :func:`overview_json` — serializes the tested :class:`OverviewModel` to the JSON the
  client fetches from ``/v2/api/overview``. Serialization is a pure transcription of the
  typed DTOs via :func:`metsuke.viewmodel.common.to_jsonable`; no cost number is computed
  or reformatted here.
* :func:`asset_bytes` — locates a committed static asset by name.

CSP contract: the shell has no inline ``<style>``, no ``style=`` attribute, no inline
``<script>`` body, and no ``on*=`` handler. The bundle loads from same-origin ``/v2/app.js``
(``script-src 'self'``); the app styles from ``/v2/app.css`` (``style-src 'self'``); its only
network call is a same-origin fetch to ``/v2/api/overview`` (``connect-src 'self'``).
"""

from __future__ import annotations

import json
from importlib import resources

from ..dashboard.routes import DashboardRequest, canonical_query
from ..viewmodel import overview
from ..viewmodel.common import to_jsonable

ASSET_CONTENT_TYPES: dict[str, str] = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}

_SHELL = (
    '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>metsuke dashboard v2</title>"
    '<link rel="stylesheet" href="/v2/app.css">'
    '<script src="/v2/app.js" defer></script>'
    '</head><body><div id="app"></div></body></html>'
)


def shell_html() -> str:
    """The data-free HTML shell served at ``/v2/dashboard`` (identical bytes every time)."""

    return _SHELL


def asset_bytes(name: str) -> bytes | None:
    """Read a committed static asset (``app.js`` / ``app.css``) or ``None`` if unknown/absent."""

    if name not in ASSET_CONTENT_TYPES:
        return None
    resource = resources.files(__package__).joinpath("assets", name)
    try:
        return resource.read_bytes()
    except (FileNotFoundError, OSError):
        return None


def _freshness_payload(freshness) -> dict:
    if freshness is None:
        return {"stale": False, "last_ingest": None, "age_seconds": None}
    return {
        "stale": bool(getattr(freshness, "stale", False)),
        "last_ingest": getattr(freshness, "last_ingest", None),
        "age_seconds": getattr(freshness, "age_seconds", None),
    }


def view_payload(request: DashboardRequest, model: object, freshness) -> dict:
    """Assemble the API response: resolved request metadata + freshness + the serialized model.

    ``model`` is transcribed by :func:`to_jsonable` exactly — every number is passed through
    unchanged. The ``request`` block echoes the resolver's decision (preset, window, project,
    limit, order) and the canonical query so the client can canonicalize its URL without
    re-implementing the window maths.

    This is view-agnostic: ``model`` may be the typed :class:`OverviewModel` (overview) or a
    :class:`~metsuke.viewmodel.common.LegacyViewModel` node tree (period/dist). ``to_jsonable``
    already serializes both faithfully, so no per-view serialization code is required.
    """

    window = request.window
    return {
        "request": {
            "view": request.view,
            "preset": request.preset,
            "from": window.start.isoformat(),
            "to": window.end.isoformat(),
            "project": window.project,
            "limit": request.page.limit,
            "order": request.page.order,
            "canonical_query": canonical_query(request),
        },
        "freshness": _freshness_payload(freshness),
        "model": to_jsonable(model),
    }


def view_json(request: DashboardRequest, model: object, freshness) -> bytes:
    """UTF-8 JSON bytes for a ``/v2/api/<view>`` response body (overview/period/dist)."""

    return json.dumps(
        view_payload(request, model, freshness),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def overview_payload(
    request: DashboardRequest, model: overview.OverviewModel, freshness
) -> dict:
    """Assemble the ``/v2/api/overview`` response (see :func:`view_payload`)."""

    return view_payload(request, model, freshness)


def overview_json(
    request: DashboardRequest, model: overview.OverviewModel, freshness
) -> bytes:
    """UTF-8 JSON bytes for the ``/v2/api/overview`` response body."""

    return view_json(request, model, freshness)
