"""Hardened read-only SQLite connection for the local dashboard."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
from typing import Any

BUSY_TIMEOUT_MS = 250


class DashboardDatabaseError(RuntimeError):
    """Safe internal database error that does not expose SQL or local paths."""

    code = "ledger_unavailable"
    retryable = False
    message = "The ledger is unavailable."

    def __init__(self) -> None:
        super().__init__(self.message)


class LedgerBusyError(DashboardDatabaseError):
    code = "ledger_busy"
    retryable = True
    message = "The ledger is busy."


class LedgerNotFoundError(DashboardDatabaseError):
    code = "ledger_not_found"
    message = "The ledger is not available."


class LedgerCorruptError(DashboardDatabaseError):
    code = "ledger_corrupt"
    message = "The ledger cannot be read."


class LedgerAccessDeniedError(DashboardDatabaseError):
    code = "ledger_access_denied"
    message = "The database operation is not allowed."


def _normalize_error(exc: sqlite3.DatabaseError) -> DashboardDatabaseError:
    error_code = getattr(exc, "sqlite_errorcode", 0) or 0
    primary_code = error_code & 0xFF
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return LedgerBusyError()
    if primary_code == sqlite3.SQLITE_CANTOPEN:
        return LedgerNotFoundError()
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return LedgerCorruptError()
    if primary_code in {sqlite3.SQLITE_AUTH, sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY}:
        return LedgerAccessDeniedError()
    return DashboardDatabaseError()


class DashboardCursor(sqlite3.Cursor):
    def __next__(self) -> Any:
        try:
            return super().__next__()
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def execute(self, sql: str, parameters: Any = (), /) -> DashboardCursor:
        try:
            return super().execute(sql, parameters)
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def executemany(self, sql: str, parameters: Any, /) -> DashboardCursor:
        try:
            return super().executemany(sql, parameters)
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def executescript(self, sql_script: str, /) -> DashboardCursor:
        try:
            return super().executescript(sql_script)
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def fetchone(self) -> Any:
        try:
            return super().fetchone()
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def fetchmany(self, size: int | None = None) -> list[Any]:
        try:
            return super().fetchmany() if size is None else super().fetchmany(size)
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc

    def fetchall(self) -> list[Any]:
        try:
            return super().fetchall()
        except sqlite3.DatabaseError as exc:
            raise _normalize_error(exc) from exc


class DashboardConnection(sqlite3.Connection):
    def cursor(self, factory: type[sqlite3.Cursor] = DashboardCursor) -> sqlite3.Cursor:
        return super().cursor(factory)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self.cursor().executescript(sql_script)


_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_SELECT,
    }
)
_ALLOWED_READ_PRAGMAS = frozenset({"busy_timeout", "query_only"})


def _authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    if action == sqlite3.SQLITE_PRAGMA:
        pragma = (arg1 or "").lower()
        return (
            sqlite3.SQLITE_OK
            if pragma in _ALLOWED_READ_PRAGMAS and arg2 is None
            else sqlite3.SQLITE_DENY
        )
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _validate_database_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, OSError) as exc:
        raise LedgerNotFoundError() from exc
    if not stat.S_ISREG(mode):
        raise LedgerNotFoundError()


def connect_dashboard(path: str | Path) -> DashboardConnection:
    """Open one dashboard reader with its complete safety policy installed."""

    database_path = Path(path)
    _validate_database_file(database_path)
    uri = f"{database_path.absolute().as_uri()}?mode=ro"
    connection: DashboardConnection | None = None
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            factory=DashboardConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorizer)
        return connection
    except DashboardDatabaseError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.DatabaseError as exc:
        if connection is not None:
            connection.close()
        raise _normalize_error(exc) from exc
