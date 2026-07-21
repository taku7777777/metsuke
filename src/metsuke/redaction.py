"""Secret redaction at the read boundary (ADR 0002: the archive stays raw).

Applied when text enters the ledger (prompt.text) and by the AI-facing archive
accessor. Patterns are versioned: bump REDACTION_VERSION when they change, then
`metsuke rebuild` re-applies to all history. False positives are recoverable — the
raw archive is untouched.
"""

from __future__ import annotations

import hashlib
import re

REDACTION_VERSION = 2

# (name, compiled pattern) — keep patterns high-precision; recall improves per version
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:(?:proj|svcacct|admin|or-v1)-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{60,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)(?:aws_secret_access_key|secretaccesskey)\s*[:=]\s*[A-Za-z0-9/+=]{40}")),
    ("slack_token", re.compile(r"xox[abcdeprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"AIza[A-Za-z0-9_-]{30,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{20,}")),
    ("stripe_key", re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("huggingface_token", re.compile(r"hf_[A-Za-z0-9]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,15000}?-----END [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer_header", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Returns (redacted_text, detections). Detections carry pattern name + hash
    prefix only — never the plaintext."""
    detections: list[str] = []

    def _sub(name):
        def inner(m):
            h = hashlib.sha256(m.group(0).encode()).hexdigest()[:12]
            detections.append(f"{name}:{h}")
            return f"[REDACTED:{name}:{h}]"

        return inner

    out = text
    for name, pat in PATTERNS:
        out = pat.sub(_sub(name), out)
    return out, detections
