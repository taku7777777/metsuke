#!/usr/bin/env python3
"""Strict read-only JSON query adapter for the weekly analyst."""

from __future__ import annotations

import json
import re
import sqlite3
import sys

ROW_LIMIT = 10_000
_LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)


def _authorizer(action, _arg1, _arg2, _db_name, _trigger):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print('usage: analyst-query.py <db_path> "<SQL>"', file=sys.stderr)
        return 1
    path, sql = args
    stripped = _LEADING_COMMENTS.sub("", sql)
    token = re.match(r"[A-Za-z]+", stripped)
    if token is None or token.group(0).upper() not in {"SELECT", "WITH"}:
        print("error: only SELECT or WITH queries are allowed", file=sys.stderr)
        return 1
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(_authorizer)
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(ROW_LIMIT + 1)
        truncated = len(rows) > ROW_LIMIT
        print(json.dumps([dict(row) for row in rows[:ROW_LIMIT]], ensure_ascii=False))
        if truncated:
            print(f"truncated: result exceeds {ROW_LIMIT} rows", file=sys.stderr)
        conn.close()
        return 0
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
