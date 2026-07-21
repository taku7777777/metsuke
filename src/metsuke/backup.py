"""Encrypted backup of metsuke data to an explicitly configured restic repo.

The destination is never inferred from cloud-storage folders. The password is
generated once and stored 0600 under the data home — the threat model is laptop
loss / repo leak, not a local attacker with user privileges.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from . import config

RESTIC_CANDIDATES = (
    Path("/opt/homebrew/bin/restic"),
    Path("/usr/local/bin/restic"),
)


def restic_binary() -> str | None:
    found = shutil.which("restic")
    if found:
        return found
    for candidate in RESTIC_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def repo_path() -> Path:
    env = config.value("METSUKE_RESTIC_REPO")
    if env:
        return Path(env)
    raise RuntimeError("backup is not configured: set METSUKE_RESTIC_REPO explicitly")


def password_file() -> Path:
    p = config.state_dir() / "restic.pass"
    if not p.exists():
        config.ensure_dirs()
        p.write_text(secrets.token_urlsafe(32))
        os.chmod(p, config.FILE_MODE)
    return p


def _restic(args: list[str]) -> subprocess.CompletedProcess:
    binary = restic_binary()
    if binary is None:
        return subprocess.CompletedProcess(
            ["restic", *args], 127, stdout="", stderr="restic not installed"
        )
    return subprocess.run(
        [binary, "-r", str(repo_path()), "--password-file", str(password_file()), *args],
        capture_output=True,
        text=True,
    )


def run() -> int:
    if restic_binary() is None:
        print("restic not installed (brew install restic)")
        return 1
    try:
        repository = repo_path()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    if _restic(["cat", "config"]).returncode != 0:
        r = _restic(["init"])
        if r.returncode != 0:
            print(f"restic init failed: {r.stderr.strip()}")
            return 1
        print(f"initialized restic repo at {repo_path()}")
    targets = [
        path
        for path in (
            config.archive_dir(), config.reports_dir(), config.proposals_dir(),
            config.handoffs_dir(), config.config_path(),
        )
        if path.exists()
    ]
    r = _restic(["backup", *(str(path) for path in targets), "--tag", "metsuke"])
    if r.returncode != 0:
        print(f"backup failed: {r.stderr.strip()}")
        return 1
    marker = config.state_dir() / "last_backup.json"
    marker.write_text(
        json.dumps(
            {"ts": time.time(), "repo": str(repository), "targets": [str(p) for p in targets]}
        )
    )
    os.chmod(marker, config.FILE_MODE)
    print(f"backup ok → {repo_path()}")
    print("NOTE: keep a copy of the password somewhere safe:", password_file())
    return 0


def verify_restore() -> int:
    """Restore the manifest and one segment, then verify its decompressed SHA."""
    import tempfile

    from . import archiver

    try:
        repo_path()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    entries = archiver.manifest_entries()
    if not entries:
        print("archive has no segments to verify")
        return 1
    entry = entries[-1]

    r = _restic(["snapshots", "--json"])
    if r.returncode != 0 or not json.loads(r.stdout or "[]"):
        print("no snapshots to verify")
        return 1
    with tempfile.TemporaryDirectory() as td:
        segment_name = Path(entry["seg"]).name
        r = _restic(
            [
                "restore", "latest", "--target", td,
                "--include", "manifest.jsonl", "--include", segment_name,
            ]
        )
        if r.returncode != 0:
            print(f"restore failed: {r.stderr.strip()}")
            return 1
        manifests = list(Path(td).rglob("manifest.jsonl"))
        segments = list(Path(td).rglob(segment_name))
        if manifests and segments:
            restored_entries = [
                json.loads(line) for line in manifests[0].read_text().splitlines() if line.strip()
            ]
            restored = next(
                (row for row in restored_entries if row.get("seg") == entry["seg"]), None
            )
            if restored:
                raw = archiver._decompress(
                    segments[0].read_bytes(), restored.get("codec", "zstd")
                )
                if hashlib.sha256(raw).hexdigest() == restored["sha256"]:
                    print(
                        f"restore verified: {restored['seg']} ({len(raw)} decompressed bytes)"
                    )
                    return 0
    print("restore verification failed")
    return 1
