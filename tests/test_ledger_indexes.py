from __future__ import annotations

from importlib import resources

from metsuke import ledger

INDEX_NAME = "idx_request_session_ts"


def _index_columns(conn) -> tuple[str, ...]:
    return tuple(row[2] for row in conn.execute(f"PRAGMA index_info({INDEX_NAME})"))


def _context_rows(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM v_context_overhead ORDER BY session_id"
        ).fetchall()
    ]


def _seed_context_rows(conn) -> None:
    for number in range(3):
        session_id = f"session-{number}"
        base = 1_800_000_000 + number * 100
        conn.execute(
            "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
            (session_id, f"project-{number}", base, base + 3),
        )
        for suffix, agent_id, synthetic, timestamp in (
            ("agent", "agent-1", 0, base),
            ("synthetic", None, 1, base + 1),
            ("main", None, 0, base + 2),
        ):
            conn.execute(
                """INSERT INTO request
                   (request_id,session_id,agent_id,lineage_id,ts,model,input_tok,
                    cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic)
                   VALUES (?,?,?,?,?,'claude-sonnet-5',10,20,30,40,?)""",
                (
                    f"request-{number}-{suffix}",
                    session_id,
                    agent_id,
                    session_id,
                    timestamp,
                    synthetic,
                ),
            )
    conn.commit()


def test_request_session_ts_index_is_in_schema_and_fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    schema = resources.files("metsuke").joinpath("schema.sql").read_text()
    assert (
        "CREATE INDEX IF NOT EXISTS idx_request_session_ts ON request(session_id, ts);"
        in schema
    )

    conn = ledger.connect(tmp_path / "fresh.db")
    try:
        assert _index_columns(conn) == ("session_id", "ts")
    finally:
        conn.close()


def test_old_database_migration_is_idempotent_and_preserves_context_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    path = tmp_path / "old.db"
    old = ledger.connect(path)
    _seed_context_rows(old)
    old.execute(f"DROP INDEX {INDEX_NAME}")
    old.commit()
    before = _context_rows(old)
    assert _index_columns(old) == ()
    old.close()

    migrated = ledger.connect(path)
    try:
        assert _index_columns(migrated) == ("session_id", "ts")
        assert _context_rows(migrated) == before
        plan = "\n".join(
            row[3]
            for row in migrated.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM v_context_overhead"
            )
        )
        assert INDEX_NAME in plan
    finally:
        migrated.close()

    migrated_again = ledger.connect(path)
    try:
        assert _index_columns(migrated_again) == ("session_id", "ts")
        assert _context_rows(migrated_again) == before
    finally:
        migrated_again.close()
