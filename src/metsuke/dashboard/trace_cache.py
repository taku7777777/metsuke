"""Derived trace cache metadata, freshness checks, and ordered purge policy."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config, ingest, trace_html
from ..redaction import REDACTION_VERSION

MANIFEST_SCHEMA_VERSION = 1
_META_PATTERNS = {
    "session_id": re.compile(r'"session_id"\s*:\s*"([A-Za-z0-9._-]+)"'),
    "parser_version": re.compile(r'"parser_version"\s*:\s*(\d+)'),
    "redaction_version": re.compile(r'"redaction_version"\s*:\s*(\d+)'),
    "template_version": re.compile(r'"trace_template_schema_version"\s*:\s*(\d+)'),
    "session_last_request_at": re.compile(
        r'"session_last_request_at"\s*:\s*([0-9.eE+-]+)'
    ),
    "generated_at": re.compile(r'"generated_at"\s*:\s*"([^"]+)"'),
}


@dataclass(frozen=True)
class TraceFingerprint:
    session_id: str
    session_last_request_at: float
    parser_version: int
    redaction_version: int
    template_version: int


@dataclass(frozen=True)
class CacheEntry:
    fingerprint: TraceFingerprint
    generated_at: float
    last_accessed_at: float
    size_bytes: int


@dataclass(frozen=True)
class CacheStats:
    count: int
    total_bytes: int
    oldest_access: float | None
    purge_failures: int


def current_fingerprint(conn, session_id: str) -> TraceFingerprint | None:
    row = conn.execute(
        """SELECT MAX(CASE WHEN end_ts IS NOT NULL AND end_ts>ts THEN end_ts ELSE ts END)
           FROM request WHERE session_id=?""",
        (session_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return TraceFingerprint(
        session_id=session_id,
        session_last_request_at=float(row[0]),
        parser_version=ingest.PARSER_VERSION,
        redaction_version=REDACTION_VERSION,
        template_version=trace_html.TRACE_TEMPLATE_SCHEMA_VERSION,
    )


class TraceCache:
    """Manage a recoverable manifest; trace HTML files remain the derived data."""

    def __init__(
        self,
        directory: Path,
        manifest_path: Path,
        *,
        clock=time.time,
        max_bytes: int | None = None,
        max_age_days: int | None = None,
    ) -> None:
        self.directory = directory
        self.manifest_path = manifest_path
        self.clock = clock
        self.max_bytes = max_bytes if max_bytes is not None else config.trace_cache_max_bytes()
        self.max_age_seconds = (
            max_age_days if max_age_days is not None else config.trace_cache_max_age_days()
        ) * 86400
        self._lock = threading.RLock()
        self._protected: set[Path] = set()
        self._entries: dict[str, CacheEntry] | None = None
        self._untracked: set[Path] = set()
        self._purge_failures = 0

    def path_for(self, session_id: str) -> Path:
        target = trace_html.target_path(session_id)
        if target is None:
            raise ValueError("invalid session ID")
        return self.directory / target.name

    def _entry_from_dict(self, session_id: str, value: object) -> CacheEntry | None:
        if not isinstance(value, dict) or set(value) != {
            "fingerprint",
            "generated_at",
            "last_accessed_at",
            "size_bytes",
        }:
            return None
        fingerprint = value["fingerprint"]
        if not isinstance(fingerprint, dict) or set(fingerprint) != {
            "session_id",
            "session_last_request_at",
            "parser_version",
            "redaction_version",
            "template_version",
        }:
            return None
        try:
            parsed = TraceFingerprint(
                session_id=str(fingerprint["session_id"]),
                session_last_request_at=float(fingerprint["session_last_request_at"]),
                parser_version=int(fingerprint["parser_version"]),
                redaction_version=int(fingerprint["redaction_version"]),
                template_version=int(fingerprint["template_version"]),
            )
            entry = CacheEntry(
                parsed,
                float(value["generated_at"]),
                float(value["last_accessed_at"]),
                int(value["size_bytes"]),
            )
        except (TypeError, ValueError):
            return None
        numbers = (
            parsed.session_last_request_at,
            entry.generated_at,
            entry.last_accessed_at,
        )
        return (
            entry
            if parsed.session_id == session_id
            and entry.size_bytes >= 0
            and all(math.isfinite(number) for number in numbers)
            else None
        )

    def _recover_file(self, path: Path) -> CacheEntry | None:
        try:
            text = path.read_text(errors="ignore")
            values = {
                name: pattern.search(text) for name, pattern in _META_PATTERNS.items()
            }
            if any(match is None for match in values.values()):
                return None
            generated_text = values["generated_at"].group(1)  # type: ignore[union-attr]
            generated_at = dt.datetime.fromisoformat(generated_text).timestamp()
            stat = path.stat()
            fingerprint = TraceFingerprint(
                values["session_id"].group(1),  # type: ignore[union-attr]
                float(values["session_last_request_at"].group(1)),  # type: ignore[union-attr]
                int(values["parser_version"].group(1)),  # type: ignore[union-attr]
                int(values["redaction_version"].group(1)),  # type: ignore[union-attr]
                int(values["template_version"].group(1)),  # type: ignore[union-attr]
            )
            if fingerprint.session_id != path.stem:
                return None
            return CacheEntry(fingerprint, generated_at, stat.st_atime, stat.st_size)
        except (OSError, TypeError, ValueError):
            return None

    def _load(self) -> dict[str, CacheEntry]:
        if self._entries is not None:
            return self._entries
        parsed: dict[str, CacheEntry] = {}
        valid_manifest = False
        try:
            raw = json.loads(self.manifest_path.read_text())
            if (
                isinstance(raw, dict)
                and raw.get("schema_version") == MANIFEST_SCHEMA_VERSION
                and isinstance(raw.get("entries"), dict)
            ):
                failures = raw.get("purge_failures", 0)
                if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
                    raise ValueError("invalid purge failure count")
                valid_manifest = True
                for session_id, value in raw["entries"].items():
                    entry = self._entry_from_dict(str(session_id), value)
                    path = self.path_for(str(session_id))
                    recovered = self._recover_file(path) if path.is_file() else None
                    if entry is not None and recovered is not None:
                        # The HTML is the source for freshness and size. The
                        # derived manifest contributes only its access time.
                        parsed[str(session_id)] = CacheEntry(
                            recovered.fingerprint,
                            recovered.generated_at,
                            entry.last_accessed_at,
                            recovered.size_bytes,
                        )
                self._purge_failures = failures
        except (OSError, ValueError, TypeError):
            pass
        for path in self.directory.glob("*.html") if self.directory.exists() else ():
            if path.stem in parsed:
                continue
            entry = self._recover_file(path)
            if entry is not None:
                parsed[path.stem] = entry
            else:
                # Old or malformed trace files have no trustworthy freshness
                # metadata. They cannot be reused and are removed in purge's
                # version stage, unless an active job currently protects them.
                self._untracked.add(path)
        self._entries = parsed
        if not valid_manifest and (parsed or self.manifest_path.exists()):
            self._write()
        return parsed

    def _write(self) -> None:
        entries = self._entries or {}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.manifest_path.parent, config.DIR_MODE)
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "entries": {
                session_id: {
                    "fingerprint": asdict(entry.fingerprint),
                    "generated_at": entry.generated_at,
                    "last_accessed_at": entry.last_accessed_at,
                    "size_bytes": entry.size_bytes,
                }
                for session_id, entry in sorted(entries.items())
            },
            "purge_failures": self._purge_failures,
        }
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            config.FILE_MODE,
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
            os.chmod(self.manifest_path, config.FILE_MODE)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @contextlib.contextmanager
    def protect(self, path: Path):
        with self._lock:
            self._protected.add(path)
        try:
            yield
        finally:
            with self._lock:
                self._protected.discard(path)

    def lookup(self, fingerprint: TraceFingerprint) -> Path | None:
        with self._lock:
            entry = self._load().get(fingerprint.session_id)
            path = self.path_for(fingerprint.session_id)
            if entry is None or entry.fingerprint != fingerprint or not path.is_file():
                return None
            now = self.clock()
            self._entries[fingerprint.session_id] = CacheEntry(
                entry.fingerprint,
                entry.generated_at,
                now,
                path.stat().st_size,
            )
            self._write()
            return path

    def register(self, fingerprint: TraceFingerprint, path: Path) -> None:
        with self._lock:
            now = self.clock()
            self._load()[fingerprint.session_id] = CacheEntry(
                fingerprint,
                now,
                now,
                path.stat().st_size,
            )
            self._untracked.discard(path)
            self._write()

    def _delete(self, session_id: str) -> bool:
        path = self.path_for(session_id)
        if path in self._protected:
            return False
        try:
            path.unlink(missing_ok=True)
            self._entries.pop(session_id, None)  # type: ignore[union-attr]
            return True
        except OSError:
            self._purge_failures += 1
            return False

    def purge(self) -> CacheStats:
        with self._lock:
            entries = self._load()
            now = self.clock()
            for path in list(self._untracked):
                if path in self._protected:
                    continue
                try:
                    path.unlink(missing_ok=True)
                    self._untracked.discard(path)
                except OSError:
                    self._purge_failures += 1
            for session_id, entry in list(entries.items()):
                if (
                    entry.fingerprint.redaction_version != REDACTION_VERSION
                    or entry.fingerprint.template_version
                    != trace_html.TRACE_TEMPLATE_SCHEMA_VERSION
                ):
                    self._delete(session_id)
            for session_id, entry in list(entries.items()):
                if now - entry.last_accessed_at > self.max_age_seconds:
                    self._delete(session_id)
            total = sum(entry.size_bytes for entry in entries.values())
            for session_id, entry in sorted(
                entries.items(), key=lambda item: item[1].last_accessed_at
            ):
                if total <= self.max_bytes:
                    break
                if self._delete(session_id):
                    total -= entry.size_bytes
            self._write()
            return self.stats()

    def stats(self) -> CacheStats:
        with self._lock:
            entries = self._load()
            return CacheStats(
                len(entries),
                sum(entry.size_bytes for entry in entries.values()),
                min((entry.last_accessed_at for entry in entries.values()), default=None),
                self._purge_failures,
            )
