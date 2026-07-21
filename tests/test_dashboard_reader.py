from __future__ import annotations

import hashlib
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from metsuke.dashboard.db import (
    DashboardDatabaseError,
    LedgerAccessDeniedError,
    LedgerBusyError,
    LedgerCorruptError,
    LedgerNotFoundError,
    connect_dashboard,
)


def _initialize_wal_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE item(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO item(value) VALUES ('checkpointed')")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


def _read_values(path: Path) -> list[str]:
    with closing(connect_dashboard(path)) as connection:
        return [row[0] for row in connection.execute("SELECT value FROM item ORDER BY id")]


def _leave_crashed_wal(path: Path, value: str = "crash-committed") -> None:
    script = """
import os
import signal
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("INSERT INTO item(value) VALUES (?)", (sys.argv[2],))
connection.commit()
print("committed", flush=True)
os.kill(os.getpid(), signal.SIGSTOP)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path), value],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "committed"
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _main_file_state(path: Path) -> tuple[str, int, int]:
    stat_result = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat_result.st_mtime_ns,
        stat_result.st_size,
    )


def _schema(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        )
    ]


def test_reader_sees_uncheckpointed_wal_commit(tmp_path):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO item(value) VALUES ('wal-committed')")
    writer.commit()
    try:
        assert path.with_name("ledger.db-wal").stat().st_size > 0
        assert _read_values(path) == ["checkpointed", "wal-committed"]
    finally:
        writer.close()


def test_reader_opens_wal_when_shm_is_missing(tmp_path):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    _leave_crashed_wal(path)
    shm_path = path.with_name("ledger.db-shm")
    shm_path.unlink(missing_ok=True)
    assert not shm_path.exists()
    assert _read_values(path) == ["checkpointed", "crash-committed"]


def test_reader_recovers_latest_commit_after_writer_sigkill(tmp_path):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    _leave_crashed_wal(path, "survived-sigkill")
    assert path.with_name("ledger.db-wal").stat().st_size > 0
    assert _read_values(path) == ["checkpointed", "survived-sigkill"]


def test_reader_coexists_with_normal_wal_writer_and_normalizes_exclusive_busy(tmp_path):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)

    writer = sqlite3.connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO item(value) VALUES ('not-committed')")
    try:
        assert _read_values(path) == ["checkpointed"]
    finally:
        writer.rollback()
        writer.close()

    exclusive_writer = sqlite3.connect(path)
    assert (
        exclusive_writer.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] == "exclusive"
    )
    exclusive_writer.execute("BEGIN EXCLUSIVE")
    exclusive_writer.execute("UPDATE item SET value='locked' WHERE id=1")
    started = time.monotonic()
    reader = None
    try:
        with pytest.raises(LedgerBusyError) as caught:
            reader = connect_dashboard(path)
            reader.execute("SELECT value FROM item").fetchall()
        elapsed = time.monotonic() - started
        assert caught.value.code == "ledger_busy"
        assert 0.20 <= elapsed <= 0.75
    finally:
        if reader is not None:
            reader.close()
        exclusive_writer.rollback()
        exclusive_writer.close()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO item(value) VALUES ('blocked')",
        "UPDATE item SET value='blocked'",
        "DELETE FROM item",
        "CREATE TABLE blocked(value TEXT)",
    ],
)
def test_reader_rejects_write_and_ddl(tmp_path, statement):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    with closing(connect_dashboard(path)) as connection:
        with pytest.raises(LedgerAccessDeniedError):
            connection.execute(statement)
        assert _read_values(path) == ["checkpointed"]


@pytest.mark.parametrize(
    "statement",
    [
        "ATTACH DATABASE ':memory:' AS attached",
        "DETACH DATABASE main",
        "PRAGMA writable_schema=ON",
    ],
)
def test_authorizer_rejects_attach_and_unsafe_pragma(tmp_path, statement):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    with closing(connect_dashboard(path)) as connection:
        with pytest.raises(LedgerAccessDeniedError):
            connection.execute(statement)


def test_query_only_guard_cannot_be_disabled(tmp_path):
    # This specifically catches a regression from the PRAGMA allowlist to a denylist:
    # forgetting query_only=OFF in a denylist would disable the guard itself.
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    with closing(connect_dashboard(path)) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
        with pytest.raises(LedgerAccessDeniedError):
            connection.execute("PRAGMA query_only=OFF")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1


def test_repeated_reader_close_does_not_change_main_database_or_schema(tmp_path):
    path = tmp_path / "ledger.db"
    _initialize_wal_database(path)
    _leave_crashed_wal(path)
    before = _main_file_state(path)
    schemas = []
    for _ in range(5):
        with closing(connect_dashboard(path)) as connection:
            schemas.append(_schema(connection))
            assert connection.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    after = _main_file_state(path)
    assert after == before
    assert all(schema == schemas[0] for schema in schemas)


def test_reader_rejects_missing_nonregular_and_symlink_paths(tmp_path):
    regular = tmp_path / "regular.db"
    _initialize_wal_database(regular)
    symlink = tmp_path / "symlink.db"
    symlink.symlink_to(regular)
    for path in (tmp_path / "missing.db", tmp_path, symlink):
        with pytest.raises(LedgerNotFoundError):
            connect_dashboard(path)
        assert not (tmp_path / "missing.db").exists()


def test_reader_normalizes_corrupt_database_without_exposing_path(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    with closing(connect_dashboard(path)) as connection:
        with pytest.raises(LedgerCorruptError) as caught:
            connection.execute("SELECT * FROM sqlite_schema").fetchall()
    assert caught.value.code == "ledger_corrupt"
    assert str(path) not in str(caught.value)
    assert "SELECT" not in str(caught.value)


def test_normalized_errors_have_safe_public_metadata():
    assert issubclass(LedgerBusyError, DashboardDatabaseError)
    assert LedgerBusyError.retryable is True
    assert LedgerNotFoundError.retryable is False
