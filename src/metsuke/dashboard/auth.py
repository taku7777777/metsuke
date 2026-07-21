"""Authentication primitives for the loopback dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config

BOOTSTRAP_TTL_SECONDS = 60
COOKIE_TTL_SECONDS = 12 * 60 * 60
COOKIE_NAME = "metsuke_dashboard"
SECRET_BYTES = 32


class DashboardAuthError(RuntimeError):
    """Authentication setup failed without exposing secret material or paths."""


@dataclass(frozen=True)
class AuthClaims:
    expires_at: int
    csrf_token: str


def _base64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _signature(secret: bytes, purpose: bytes, body: str) -> bytes:
    return hmac.new(secret, purpose + b"\0" + body.encode("ascii"), hashlib.sha256).digest()


def _encode_signed(secret: bytes, purpose: bytes, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = _base64_encode(serialized)
    return f"{body}.{_base64_encode(_signature(secret, purpose, body))}"


def _decode_signed(secret: bytes, purpose: bytes, token: str) -> dict[str, Any] | None:
    if not token or len(token) > 2048 or token.count(".") != 1:
        return None
    body, supplied_signature = token.split(".", 1)
    try:
        signature = _base64_decode(supplied_signature)
    except (ValueError, UnicodeError):
        return None
    expected_signature = _signature(secret, purpose, body)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        payload = json.loads(_base64_decode(body))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_or_create_secret(path: Path) -> bytes:
    """Load the stable per-install secret, creating it atomically when absent."""

    try:
        status = path.lstat()
    except FileNotFoundError:
        status = None
    except OSError as exc:
        raise DashboardAuthError("dashboard secret is unavailable") from exc
    if status is not None:
        if not stat.S_ISREG(status.st_mode):
            raise DashboardAuthError("dashboard secret is unavailable")
        try:
            value = path.read_bytes()
            os.chmod(path, config.FILE_MODE)
        except OSError as exc:
            raise DashboardAuthError("dashboard secret is unavailable") from exc
        if len(value) != SECRET_BYTES:
            raise DashboardAuthError("dashboard secret is invalid")
        return value

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, config.DIR_MODE)
    value = secrets.token_bytes(SECRET_BYTES)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        config.FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, config.FILE_MODE)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return value


class AuthManager:
    def __init__(
        self,
        secret: bytes,
        server_instance_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) != SECRET_BYTES:
            raise ValueError("dashboard secret must be 32 bytes")
        self._secret = secret
        self.server_instance_id = server_instance_id
        self._clock = clock
        self._used_nonce_digests: dict[bytes, float] = {}

    def issue_bootstrap_nonce(self) -> str:
        now = int(self._clock())
        return _encode_signed(
            self._secret,
            b"bootstrap-nonce-v1",
            {
                "instance": self.server_instance_id,
                "issued_at": now,
                "random": secrets.token_urlsafe(24),
            },
        )

    def consume_bootstrap_nonce(self, token: str) -> bool:
        now = self._clock()
        self._purge_used_nonces(now)
        payload = _decode_signed(self._secret, b"bootstrap-nonce-v1", token)
        if payload is None:
            return False
        issued_at = payload.get("issued_at")
        instance = payload.get("instance")
        random_value = payload.get("random")
        if (
            not isinstance(issued_at, int)
            or not isinstance(instance, str)
            or not isinstance(random_value, str)
            or not random_value
            or instance != self.server_instance_id
            or not 0 <= now - issued_at <= BOOTSTRAP_TTL_SECONDS
        ):
            return False
        digest = hashlib.sha256(token.encode()).digest()
        if digest in self._used_nonce_digests:
            return False
        self._used_nonce_digests[digest] = now + BOOTSTRAP_TTL_SECONDS
        return True

    def issue_cookie(self) -> str:
        now = int(self._clock())
        return _encode_signed(
            self._secret,
            b"auth-cookie-v1",
            {
                "csrf": secrets.token_urlsafe(32),
                "expires_at": now + COOKIE_TTL_SECONDS,
                "issued_at": now,
                "session": secrets.token_urlsafe(24),
            },
        )

    def validate_cookie(self, token: str) -> AuthClaims | None:
        payload = _decode_signed(self._secret, b"auth-cookie-v1", token)
        if payload is None:
            return None
        issued_at = payload.get("issued_at")
        expires_at = payload.get("expires_at")
        csrf_token = payload.get("csrf")
        session = payload.get("session")
        now = self._clock()
        if (
            not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or not isinstance(csrf_token, str)
            or not csrf_token
            or not isinstance(session, str)
            or not session
            or not issued_at <= now <= expires_at
            or expires_at - issued_at != COOKIE_TTL_SECONDS
        ):
            return None
        return AuthClaims(expires_at, csrf_token)

    @staticmethod
    def validate_csrf(claims: AuthClaims, supplied_token: str | None) -> bool:
        return supplied_token is not None and hmac.compare_digest(
            claims.csrf_token, supplied_token
        )

    def _purge_used_nonces(self, now: float) -> None:
        self._used_nonce_digests = {
            digest: expires_at
            for digest, expires_at in self._used_nonce_digests.items()
            if expires_at > now
        }
