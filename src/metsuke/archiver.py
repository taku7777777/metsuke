"""Transcript archiver (Stage 0).

Captures ~/.claude/projects/** into an immutable, append-only local archive *before*
Claude Code's ~30-day cleanup deletes the originals. Raw bytes, no redaction
(redaction happens at read boundaries — ADR 0002 / 01-architecture).

Design:
- *.jsonl files are treated as append-only streams. Each run captures new bytes
  [cursor.offset, last_newline) as one raw *segment* (zstd). A torn final line
  (no trailing newline yet) is left for the next run.
- If a file's inode changes or it shrinks (resume/compaction rewrite — Q11),
  we bump the *generation* and re-capture from offset 0. Nothing is ever lost;
  reconstruction picks segments per (path, gen).
- Non-jsonl files (e.g. subagents' agent-*.meta.json) are captured as whole-file
  snapshots when their content hash changes.
- Every segment gets a line in archive/manifest.jsonl: sha256 + provenance.
  The manifest is the integrity ledger; segments+manifest suffice to rebuild.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

try:
    import zstandard  # type: ignore

    def _compress(data: bytes) -> tuple[bytes, str]:
        return zstandard.ZstdCompressor(level=9).compress(data), "zstd"

    def _decompress(data: bytes, codec: str) -> bytes:
        if codec == "zstd":
            return zstandard.ZstdDecompressor().decompress(data)
        import gzip

        return gzip.decompress(data)

except ImportError:  # pragma: no cover - fallback when zstandard is unavailable
    import gzip

    def _compress(data: bytes) -> tuple[bytes, str]:
        return gzip.compress(data, 6), "gzip"

    def _decompress(data: bytes, codec: str) -> bytes:
        return gzip.decompress(data)


TAIL_WINDOW = 4096  # bytes hashed before the cursor to detect in-place rewrites


@dataclass
class Cursor:
    inode: int = 0
    gen: int = 1
    offset: int = 0  # bytes archived so far within this generation (jsonl)
    sha256: str = ""  # last archived content hash (snapshot files)
    tail_sha: str = ""  # sha256 of the last <=4KB ending at offset (rewrite detection)

    def to_json(self) -> dict:
        return {
            "inode": self.inode,
            "gen": self.gen,
            "offset": self.offset,
            "sha256": self.sha256,
            "tail_sha": self.tail_sha,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Cursor":
        return cls(
            inode=d.get("inode", 0),
            gen=d.get("gen", 1),
            offset=d.get("offset", 0),
            sha256=d.get("sha256", ""),
            tail_sha=d.get("tail_sha", ""),
        )


def _tail_sha(f: Path, offset: int) -> str:
    if offset <= 0:
        return ""
    start = max(0, offset - TAIL_WINDOW)
    with open(f, "rb") as fh:
        fh.seek(start)
        return hashlib.sha256(fh.read(offset - start)).hexdigest()


@dataclass
class RunStats:
    files_seen: int = 0
    segments: int = 0
    bytes_captured: int = 0
    generations_bumped: int = 0
    errors: list[str] = field(default_factory=list)


def _load_cursors() -> dict[str, Cursor]:
    p = config.cursors_path()
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: Cursor.from_json(v) for k, v in data.items()}


def _save_cursors(cursors: dict[str, Cursor]) -> None:
    p = config.cursors_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.to_json() for k, v in cursors.items()}, indent=0))
    os.chmod(tmp, config.FILE_MODE)
    os.replace(tmp, p)


def _write_segment(
    rel: str, gen: int, start: int, end: int, raw: bytes, kind: str, manifest, seq: int
) -> str:
    """Compress raw bytes to a segment file and append a manifest line. Returns seg filename."""
    ym = time.strftime("%Y-%m")
    seg_dir = config.segments_dir() / ym
    seg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(seg_dir, config.DIR_MODE)
    sha = hashlib.sha256(raw).hexdigest()
    compressed, codec = _compress(raw)
    name = f"{time.strftime('%Y%m%dT%H%M%S')}-{seq:05d}-{sha[:12]}.{codec}"
    seg_path = seg_dir / name
    with open(seg_path, "wb") as f:
        f.write(compressed)
    os.chmod(seg_path, config.FILE_MODE)
    manifest.write(
        json.dumps(
            {
                "ts": time.time(),
                "path": rel,
                "gen": gen,
                "start": start,
                "end": end,
                "bytes": end - start,
                "sha256": sha,
                "seg": f"{ym}/{name}",
                "kind": kind,
                "codec": codec,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return name


def archive_bytes(rel: str, raw: bytes, kind: str, manifest, seq: int = 1) -> str:
    """Archive an immutable byte payload and append its provenance to manifest."""
    return _write_segment(rel, 1, 0, len(raw), raw, kind, manifest, seq)


def _iter_source_files(src: Path):
    for p in src.rglob("*"):
        if p.is_file() and p.suffix in (".jsonl", ".json"):
            yield p


class _Lock:
    """O_EXCL lock file; stale locks (>2h) are broken."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, config.FILE_MODE)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
        except FileExistsError:
            if time.time() - self.path.stat().st_mtime > 7200:
                self.path.unlink(missing_ok=True)
                return self.__enter__()
            raise RuntimeError("another archiver run holds the lock")
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)


def run(src: Path | None = None) -> RunStats:
    """One archiver pass. Idempotent; safe to run any time."""
    config.ensure_dirs()
    src = src or config.source_dir()
    stats = RunStats()
    cursors = _load_cursors()
    seq = 0

    try:
        lock = _Lock(config.lock_path())
        lock.__enter__()
    except RuntimeError:
        return stats
    try:
        manifest = open(config.manifest_path(), "a")
        with manifest:
            os.chmod(config.manifest_path(), config.FILE_MODE)
            for f in _iter_source_files(src):
                rel = str(f.relative_to(src))
                stats.files_seen += 1
                try:
                    st = f.stat()
                    cur = cursors.get(rel) or Cursor(inode=st.st_ino)
                    if f.suffix == ".jsonl":
                        seq = _archive_jsonl(f, rel, st, cur, manifest, stats, seq)
                    else:
                        seq = _archive_snapshot(f, rel, st, cur, manifest, stats, seq)
                    cursors[rel] = cur
                except OSError as e:
                    stats.errors.append(f"{rel}: {e}")
            for f in sorted(config.otel_dir().glob("*.json")):
                rel = f"__otel__/{f.name}"
                stats.files_seen += 1
                try:
                    st = f.stat()
                    cur = cursors.get(rel) or Cursor(inode=st.st_ino)
                    seq = _archive_jsonl(f, rel, st, cur, manifest, stats, seq, kind="otel")
                    cursors[rel] = cur
                except OSError as e:
                    stats.errors.append(f"{rel}: {e}")
            manifest.flush()
            os.fsync(manifest.fileno())
            _save_cursors(cursors)
            _write_last_run(stats)
    finally:
        lock.__exit__(None, None, None)
    return stats


def _archive_jsonl(
    f: Path,
    rel: str,
    st,
    cur: Cursor,
    manifest,
    stats: RunStats,
    seq: int,
    kind: str = "jsonl",
) -> int:
    rewritten = (
        st.st_ino != cur.inode  # atomic replace
        or st.st_size < cur.offset  # truncation
        or (cur.offset > 0 and _tail_sha(f, cur.offset) != cur.tail_sha)  # in-place rewrite
    )
    if rewritten:
        # new generation, recapture from 0 — never silently mis-read a rewrite as an append
        if cur.offset > 0 or cur.inode not in (0, st.st_ino):
            cur.gen += 1
            stats.generations_bumped += 1
        cur.inode = st.st_ino
        cur.offset = 0
        cur.tail_sha = ""
    if st.st_size <= cur.offset:
        return seq
    with open(f, "rb") as fh:
        fh.seek(cur.offset)
        raw = fh.read(st.st_size - cur.offset)
    # capture only up to the last newline; a torn final line waits for the next run
    cut = raw.rfind(b"\n")
    if cut < 0:
        return seq
    raw = raw[: cut + 1]
    seq += 1
    _write_segment(rel, cur.gen, cur.offset, cur.offset + len(raw), raw, kind, manifest, seq)
    cur.offset += len(raw)
    cur.inode = st.st_ino
    cur.tail_sha = _tail_sha(f, cur.offset)
    stats.segments += 1
    stats.bytes_captured += len(raw)
    return seq


def _archive_snapshot(f: Path, rel: str, st, cur: Cursor, manifest, stats: RunStats, seq: int) -> int:
    raw = f.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha == cur.sha256:
        return seq
    if cur.sha256:
        cur.gen += 1
        stats.generations_bumped += 1
    cur.inode = st.st_ino
    cur.sha256 = sha
    seq += 1
    _write_segment(rel, cur.gen, 0, len(raw), raw, "snapshot", manifest, seq)
    stats.segments += 1
    stats.bytes_captured += len(raw)
    return seq


def _write_last_run(stats: RunStats) -> None:
    p = config.last_run_path()
    p.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "files_seen": stats.files_seen,
                "segments": stats.segments,
                "bytes_captured": stats.bytes_captured,
                "generations_bumped": stats.generations_bumped,
                "errors": stats.errors[:20],
            }
        )
    )
    os.chmod(p, config.FILE_MODE)


# ---------- reconstruction / verification ----------


def manifest_entries(path_filter: str | None = None) -> list[dict]:
    entries = []
    mp = config.manifest_path()
    if not mp.exists():
        return entries
    with open(mp) as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            if path_filter is None or e["path"] == path_filter:
                entries.append(e)
    return entries


def reconstruct(rel: str, gen: int | None = None, entries: list[dict] | None = None) -> bytes:
    """Rebuild archived bytes for a path (latest generation by default)."""
    entries = manifest_entries(rel) if entries is None else entries
    if not entries:
        return b""
    target_gen = gen if gen is not None else max(e["gen"] for e in entries)
    parts = sorted((e for e in entries if e["gen"] == target_gen), key=lambda e: e["start"])
    out = bytearray()
    for e in parts:
        seg_path = config.segments_dir() / e["seg"]
        raw = _decompress(seg_path.read_bytes(), e.get("codec", "zstd"))
        if hashlib.sha256(raw).hexdigest() != e["sha256"]:
            raise ValueError(f"sha mismatch for segment {e['seg']}")
        if e["start"] != len(out):
            raise ValueError(f"gap in segments for {rel} gen {target_gen} at {e['start']}")
        out += raw
    return bytes(out)


def verify_against_source(rel: str, src: Path | None = None) -> bool:
    """Archived bytes must equal the source file's prefix [0, archived_end)."""
    src = src or config.source_dir()
    archived = reconstruct(rel)
    if not archived:
        return False
    f = src / rel
    if not f.exists():  # source already cleaned up — archive is now the only copy
        return True
    with open(f, "rb") as fh:
        prefix = fh.read(len(archived))
    return prefix == archived
