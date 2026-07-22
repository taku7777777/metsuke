"""Keep tests independent from the operator's installed metsuke settings."""

import os
import re
from pathlib import Path


for name in tuple(os.environ):
    if name.startswith("METSUKE_"):
        os.environ.pop(name)
os.environ["METSUKE_CONFIG"] = str(Path(__file__).with_name(".nonexistent-config.env"))


# The dashboard CSP is ``script-src 'self'`` with no ``'unsafe-inline'``: the only
# script the browser will run is the served /dashboard.js, and inline ``on*=``
# handlers never fire. These helpers make that a checkable, non-vacuous invariant.
_SCRIPT_OPEN = re.compile(rb"<script[^>]*>", re.IGNORECASE)
# A real inline handler always has a literal quote after ``=``; html.escape() turns
# an attacker-supplied ``"`` into ``&quot;``, so escaped data such as
# ``onerror=alert(1)`` cannot match this and only genuine handlers do.
_INLINE_HANDLER = re.compile(rb'\son\w+="', re.IGNORECASE)
_ALLOWED_SCRIPT = b'<script src="/dashboard.js" defer>'


def assert_csp_safe(body, *, context: str = "") -> None:
    """Assert the markup carries only the served defer script and no inline handlers.

    Progressive enhancement + CSP safety: exactly one ``<script>`` element, and it is
    the external ``/dashboard.js`` reference (no inline ``<script>`` body would ever
    execute); and no inline ``on*=`` event handler attribute anywhere.
    """

    if isinstance(body, str):
        body = body.encode()
    where = f" ({context})" if context else ""
    scripts = _SCRIPT_OPEN.findall(body)
    assert scripts == [_ALLOWED_SCRIPT], f"unexpected <script> tags{where}: {scripts!r}"
    handler = _INLINE_HANDLER.search(body)
    assert handler is None, f"inline event handler{where}: {handler.group()!r}"
