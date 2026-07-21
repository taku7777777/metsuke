"""Archiver correctness — the lifeline of Stage 0.

Covers: incremental append, torn final line, truncate/replace generation bump,
snapshot files, reconstruction fidelity, idempotence.
"""

import json
import os

import pytest

from metsuke import archiver, config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "metsuke-home"
    src = tmp_path / "projects"
    src.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(src))
    return src


def _write(p, data: bytes, mode="wb"):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode) as f:
        f.write(data)


def test_backfill_and_incremental_append(env):
    f = env / "proj" / "sess1.jsonl"
    _write(f, b'{"a":1}\n{"a":2}\n')
    s1 = archiver.run(env)
    assert s1.segments == 1 and s1.bytes_captured == len(b'{"a":1}\n{"a":2}\n')

    # append two more lines -> only the delta is captured
    _write(f, b'{"a":3}\n{"a":4}\n', mode="ab")
    s2 = archiver.run(env)
    assert s2.segments == 1 and s2.bytes_captured == len(b'{"a":3}\n{"a":4}\n')

    # idempotent: nothing new -> no segments
    s3 = archiver.run(env)
    assert s3.segments == 0

    assert archiver.reconstruct("proj/sess1.jsonl") == f.read_bytes()
    assert archiver.verify_against_source("proj/sess1.jsonl", env)


def test_torn_final_line_waits_for_newline(env):
    f = env / "p" / "s.jsonl"
    _write(f, b'{"x":1}\n{"x":2')  # no trailing newline on last record
    s1 = archiver.run(env)
    assert s1.bytes_captured == len(b'{"x":1}\n')  # torn line not consumed

    _write(f, b'}\n', mode="ab")  # line completed
    s2 = archiver.run(env)
    assert s2.bytes_captured == len(b'{"x":2}\n')
    assert archiver.reconstruct("p/s.jsonl") == f.read_bytes()


def test_truncate_bumps_generation_and_recaptures(env):
    f = env / "p" / "s.jsonl"
    _write(f, b'{"x":1}\n{"x":2}\n')
    archiver.run(env)

    _write(f, b'{"compacted":true}\n')  # rewrite smaller (compaction/rewind)
    s2 = archiver.run(env)
    assert s2.generations_bumped == 1 and s2.segments == 1

    # latest generation reconstructs to the new content; old gen still recoverable
    assert archiver.reconstruct("p/s.jsonl") == b'{"compacted":true}\n'
    assert archiver.reconstruct("p/s.jsonl", gen=1) == b'{"x":1}\n{"x":2}\n'


def test_inode_swap_bumps_generation(env):
    f = env / "p" / "s.jsonl"
    _write(f, b'{"x":1}\n')
    archiver.run(env)

    tmp = env / "p" / "s.jsonl.new"
    _write(tmp, b'{"x":1}\n{"x":2}\n')
    os.replace(tmp, f)  # atomic replace -> new inode, larger size
    s2 = archiver.run(env)
    assert s2.generations_bumped == 1
    assert archiver.reconstruct("p/s.jsonl") == f.read_bytes()


def test_snapshot_json_on_change_only(env):
    f = env / "p" / "sess" / "subagents" / "agent-abc.meta.json"
    _write(f, b'{"agentType":"Explore"}')
    s1 = archiver.run(env)
    assert s1.segments == 1

    s2 = archiver.run(env)  # unchanged -> nothing
    assert s2.segments == 0

    _write(f, b'{"agentType":"Explore","status":"done"}')
    s3 = archiver.run(env)
    assert s3.segments == 1 and s3.generations_bumped == 1
    assert archiver.reconstruct("p/sess/subagents/agent-abc.meta.json") == f.read_bytes()


def test_manifest_sha_integrity(env):
    f = env / "p" / "s.jsonl"
    _write(f, b'{"x":1}\n')
    archiver.run(env)
    entries = archiver.manifest_entries()
    assert len(entries) == 1
    seg = config.segments_dir() / entries[0]["seg"]
    assert seg.exists()
    # tamper -> reconstruction must fail loudly
    seg.write_bytes(b"corrupt")
    with pytest.raises(Exception):
        archiver.reconstruct("p/s.jsonl")


def test_cursor_state_is_json(env):
    f = env / "p" / "s.jsonl"
    _write(f, b'{"x":1}\n')
    archiver.run(env)
    d = json.loads(config.cursors_path().read_text())
    assert d["p/s.jsonl"]["offset"] == len(b'{"x":1}\n')


def test_overlapping_archiver_is_quiet_skip(env):
    config.ensure_dirs()
    config.lock_path().write_text("other-pid")
    stats = archiver.run(env)
    assert stats.segments == 0 and stats.errors == []
