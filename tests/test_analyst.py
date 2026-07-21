import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
QUERY = ROOT / "scripts/analyst-query.py"


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "query.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO item(name) VALUES (?)", [("一",), ("two",)])
    conn.commit()
    conn.close()
    return path


def _query(database, sql):
    return subprocess.run(
        [sys.executable, str(QUERY), str(database), sql], text=True, capture_output=True
    )


def test_analyst_query_select_and_with(database):
    selected = _query(database, "-- safe\nSELECT id,name FROM item ORDER BY id")
    assert selected.returncode == 0
    assert json.loads(selected.stdout) == [{"id": 1, "name": "一"}, {"id": 2, "name": "two"}]
    cte = _query(database, "/* read only */ WITH x(v) AS (SELECT 7) SELECT v FROM x")
    assert cte.returncode == 0 and json.loads(cte.stdout) == [{"v": 7}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO item(name) VALUES ('bad')",
        "UPDATE item SET name='bad'",
        "PRAGMA table_info(item)",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "SELECT 1; SELECT 2",
        "WITH x AS (DELETE FROM item RETURNING id) SELECT * FROM x",
    ],
)
def test_analyst_query_rejects_writes_and_multiple_statements(database, sql):
    result = _query(database, sql)
    assert result.returncode == 1 and "error:" in result.stderr
    conn = sqlite3.connect(database)
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    conn.close()


def test_analyst_query_row_limit(database):
    result = _query(
        database,
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10001) SELECT x FROM n",
    )
    assert result.returncode == 0
    assert len(json.loads(result.stdout)) == 10_000
    assert "truncated" in result.stderr and "10000" in result.stderr


def test_analyst_runner_has_no_write_escape():
    source = (ROOT / "scripts/run-analyst.sh").read_text()
    assert "WebFetch" in source and "WebSearch" in source
    assert source.count("Bash(") == 1
    bash_rule = source.split("Bash(", 1)[1].split(")", 1)[0]
    assert bash_rule == "python3 $repo/scripts/analyst-query.py:*"
    assert "Bash(*)" not in source
    # File-write grants use the Edit(path) rule class — Write(path) rules are NOT
    # matched by file permission checks (verified live against claude 2.1.212).
    # "/$var" renders as //abs/path — permission rule paths need the double-slash
    # filesystem-absolute form; a single leading slash means project-relative and
    # silently never matches (verified live against claude 2.1.212).
    edit_rules = [part.split(")", 1)[0] for part in source.split("Edit(")[1:]]
    assert edit_rules == ["/$reports/**", "/$proposals/**"]
    assert "Write(" not in source
    assert "Task" in source.split("--disallowedTools", 1)[1].splitlines()[0]
    # The live ledger is touched exactly once — a mode=ro backup into the snapshot —
    # and never appears on the claude invocation surface (tools/paths the analyst gets).
    assert source.count("ledger.db") == 1
    ledger_line = next(line for line in source.splitlines() if "ledger.db" in line)
    assert "mode=ro" in ledger_line and "backup" in ledger_line
    claude_surface = source.split('"$claude_bin" -p', 1)[1]
    assert "ledger.db" not in claude_surface
    assert "traces" not in claude_surface
    assert "snapshot.db" in source and 'chmod 400 "$snapshot"' in source
    assert "--permission-mode dontAsk" in source
