from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
import threading
import time
from pathlib import Path

import pytest

from metsuke import config, doctor, ingest, ledger, trace_html
from metsuke.dashboard.trace_cache import TraceCache, TraceFingerprint, current_fingerprint
from metsuke.dashboard.db import LedgerAccessDeniedError
from metsuke.dashboard.trace_jobs import (
    JOB_TIMEOUT_SECONDS,
    MAX_CONCURRENT_JOBS,
    MAX_CONCURRENT_PER_SESSION,
    TraceJobManager,
)
from metsuke.redaction import REDACTION_VERSION

SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@dataclasses.dataclass
class Clock:
    value: float = 200_000.0

    def __call__(self) -> float:
        return self.value


def _database(path: Path) -> None:
    conn = ledger.connect(path)
    conn.execute(
        "INSERT INTO session(session_id,project,first_ts,last_ts) VALUES (?,?,?,?)",
        (SESSION_ID, "private-project", 100.0, 100.0),
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,ts,end_ts,model,input_tok,is_synthetic,source)
           VALUES (?,?,?,?,?,'claude-sonnet-5',1,0,'transcript')""",
        ("request-private-0001", SESSION_ID, SESSION_ID, 100.0, 101.0),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def trace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    database = tmp_path / "ledger.db"
    _database(database)
    cache = TraceCache(
        config.traces_dir(),
        config.trace_cache_manifest_path(),
        clock=Clock(),
    )
    conn = ledger.connect_readonly(database)
    fingerprint = current_fingerprint(conn, SESSION_ID)
    conn.close()
    assert fingerprint is not None
    return database, cache, fingerprint


def _metadata(fingerprint: TraceFingerprint) -> str:
    return json.dumps(
        {
            "session_id": fingerprint.session_id,
            "parser_version": fingerprint.parser_version,
            "redaction_version": fingerprint.redaction_version,
            "trace_template_schema_version": fingerprint.template_version,
            "session_last_request_at": fingerprint.session_last_request_at,
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
        separators=(",", ":"),
    )


def _artifact(
    cache: TraceCache, fingerprint: TraceFingerprint, body: str | None = None
) -> Path:
    path = cache.path_for(fingerprint.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_metadata(fingerprint) if body is None else body)
    cache.register(fingerprint, path)
    return path


def _wait(manager: TraceJobManager, job_id: str, status: str = "ready"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status == status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {status}: {manager.get(job_id)!r}")


def test_fingerprint_reuses_only_all_five_matching_values(trace_env):
    _, cache, fingerprint = trace_env
    path = _artifact(cache, fingerprint)
    assert cache.lookup(fingerprint) == path
    assert cache.lookup(dataclasses.replace(fingerprint, session_id="other-session")) is None
    assert (
        cache.lookup(
            dataclasses.replace(
                fingerprint,
                session_last_request_at=fingerprint.session_last_request_at + 1,
            )
        )
        is None
    )


@pytest.mark.parametrize("field", ["parser_version", "redaction_version", "template_version"])
def test_each_trace_version_mismatch_independently_forces_a_miss(trace_env, field):
    _, cache, fingerprint = trace_env
    _artifact(cache, fingerprint)
    changed = dataclasses.replace(fingerprint, **{field: getattr(fingerprint, field) + 1})
    assert cache.lookup(changed) is None
    assert cache.lookup(fingerprint) is not None


def test_later_session_request_changes_fingerprint_and_forces_regeneration(trace_env):
    database, cache, fingerprint = trace_env
    _artifact(cache, fingerprint)
    conn = ledger.connect(database)
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,ts,end_ts,model,is_synthetic,source)
           VALUES (?,?,?,?,?,'claude-sonnet-5',0,'transcript')""",
        ("request-private-0002", SESSION_ID, SESSION_ID, 200.0, 205.0),
    )
    conn.commit()
    conn.close()
    reader = ledger.connect_readonly(database)
    changed = current_fingerprint(reader, SESSION_ID)
    reader.close()
    assert changed is not None
    assert changed.session_last_request_at == 205.0
    assert cache.lookup(changed) is None


def test_generated_trace_metadata_matches_cache_fingerprint(trace_env):
    database, _, fingerprint = trace_env
    conn = ledger.connect_readonly(database)
    data = trace_html.build_trace_data(conn, SESSION_ID)
    conn.close()
    assert data is not None
    assert {
        "session_id": data["session_id"],
        "session_last_request_at": data["session_last_request_at"],
        "parser_version": data["parser_version"],
        "redaction_version": data["redaction_version"],
        "template_version": data["trace_template_schema_version"],
    } == dataclasses.asdict(fingerprint)


def test_manifest_is_atomic_0600_and_corruption_recovers_from_html(trace_env):
    _, cache, fingerprint = trace_env
    content = _metadata(fingerprint)
    path = _artifact(cache, fingerprint, content)
    assert stat.S_IMODE(cache.manifest_path.stat().st_mode) == 0o600
    assert not list(cache.manifest_path.parent.glob("*.tmp"))
    cache.manifest_path.write_text("{broken")
    recovered = TraceCache(cache.directory, cache.manifest_path, clock=cache.clock)
    assert recovered.lookup(fingerprint) == path
    assert json.loads(cache.manifest_path.read_text())["entries"][SESSION_ID]


def test_structurally_valid_manifest_cannot_override_actual_html_fingerprint(trace_env):
    _, cache, fingerprint = trace_env
    path = _artifact(cache, fingerprint)
    manifest = json.loads(cache.manifest_path.read_text())
    manifest["entries"][SESSION_ID]["fingerprint"]["template_version"] += 99
    manifest["entries"][SESSION_ID]["size_bytes"] = 1
    cache.manifest_path.write_text(json.dumps(manifest))
    reloaded = TraceCache(cache.directory, cache.manifest_path, clock=cache.clock)
    assert reloaded.lookup(fingerprint) == path
    assert reloaded.stats().total_bytes == path.stat().st_size


def test_purge_order_is_version_then_age_then_lru(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    clock = Clock()
    cache = TraceCache(
        config.traces_dir(),
        config.trace_cache_manifest_path(),
        clock=clock,
        max_bytes=10,
        max_age_days=1,
    )
    base = TraceFingerprint(
        "versionbad1", 1, ingest.PARSER_VERSION, REDACTION_VERSION - 1,
        trace_html.TRACE_TEMPLATE_SCHEMA_VERSION,
    )
    for fingerprint, accessed in (
        (base, 190_000),
        (dataclasses.replace(base, session_id="oldsession1", redaction_version=REDACTION_VERSION), 0),
        (dataclasses.replace(base, session_id="lrusession1", redaction_version=REDACTION_VERSION), 150_000),
        (dataclasses.replace(base, session_id="newsession1", redaction_version=REDACTION_VERSION), 160_000),
    ):
        clock.value = accessed
        _artifact(cache, fingerprint, "0123456789")
    clock.value = 200_000
    deleted = []
    original = cache._delete

    def recording_delete(session_id):
        deleted.append(session_id)
        return original(session_id)

    cache._delete = recording_delete
    stats = cache.purge()
    assert deleted == ["versionbad1", "oldsession1", "lrusession1"]
    assert stats.count == 1 and stats.total_bytes == 10


def test_purge_never_deletes_a_generating_or_serving_file(trace_env):
    _, cache, fingerprint = trace_env
    path = _artifact(
        cache,
        dataclasses.replace(fingerprint, redaction_version=REDACTION_VERSION - 1),
    )
    with cache.protect(path):
        cache.purge()
        assert path.exists()
    cache.purge()
    assert not path.exists()


def test_cache_hit_is_immediately_ready_and_opens_without_generation(trace_env):
    database, cache, fingerprint = trace_env
    path = _artifact(cache, fingerprint)
    opened = []

    def should_not_generate(_session, _conn):
        raise AssertionError("cache hit generated a trace")

    manager = TraceJobManager(
        database, cache, generator=should_not_generate, opener=lambda *args: opened.append(args) or True
    )
    job = manager.submit(SESSION_ID)
    assert job.status == "ready" and job.cache_result == "hit"
    assert opened == [(path, "")]
    manager.shutdown()


def test_session_jobs_deduplicate_and_generated_trace_opens_in_browser(trace_env):
    database, cache, _ = trace_env
    entered = threading.Event()
    release = threading.Event()
    opened = []

    def generate(_session, conn):
        entered.set()
        assert release.wait(2)
        with pytest.raises(LedgerAccessDeniedError):
            conn.execute("INSERT INTO session(session_id) VALUES ('must-not-write')")
        path = cache.path_for(SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated")
        return path

    before = hashlib.sha256(database.read_bytes()).digest()
    manager = TraceJobManager(
        database, cache, generator=generate, opener=lambda *args: opened.append(args) or True
    )
    first = manager.submit(SESSION_ID, fragment="#prompt=private-prompt-id")
    assert entered.wait(1)
    second = manager.submit(SESSION_ID)
    assert second.job_id == first.job_id
    release.set()
    _wait(manager, first.job_id)
    assert opened == [(cache.path_for(SESSION_ID), "#prompt=private-prompt-id")]
    assert hashlib.sha256(database.read_bytes()).digest() == before
    manager.shutdown()


def test_default_job_generator_writes_only_cache_and_uses_injected_browser(trace_env):
    database, cache, _ = trace_env
    opened = []
    before = hashlib.sha256(database.read_bytes()).digest()
    manager = TraceJobManager(
        database,
        cache,
        opener=lambda *args: opened.append(args) or True,
    )
    job = manager.submit(SESSION_ID)
    _wait(manager, job.job_id)
    target = cache.path_for(SESSION_ID)
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert opened == [(target, "")]
    assert hashlib.sha256(database.read_bytes()).digest() == before
    manager.shutdown()


def test_job_limits_and_timeout_are_explicit_constants(trace_env):
    database, cache, _ = trace_env
    assert (MAX_CONCURRENT_JOBS, MAX_CONCURRENT_PER_SESSION, JOB_TIMEOUT_SECONDS) == (2, 1, 30.0)

    def slow(_session, _conn):
        time.sleep(0.15)
        path = cache.path_for(SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("late")
        return path

    manager = TraceJobManager(database, cache, generator=slow, timeout=0.02)
    job = manager.submit(SESSION_ID)
    _wait(manager, job.job_id, "failed")
    manager.shutdown(timeout=1)


def test_global_limit_is_two_and_shutdown_waits_for_active_generation(trace_env):
    database, cache, _ = trace_env
    writer = ledger.connect(database)
    for index in (2, 3):
        session_id = f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}"
        writer.execute(
            "INSERT INTO session(session_id,first_ts,last_ts) VALUES (?,?,?)",
            (session_id, 100.0, 100.0),
        )
        writer.execute(
            """INSERT INTO request
               (request_id,session_id,lineage_id,ts,model,is_synthetic,source)
               VALUES (?,?,?,?,?,0,'transcript')""",
            (f"request-private-000{index}", session_id, session_id, 100.0, "claude-sonnet-5"),
        )
    writer.commit()
    writer.close()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def generate(session_id, _conn):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        assert release.wait(2)
        path = cache.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trace")
        with lock:
            active -= 1
        return path

    manager = TraceJobManager(database, cache, generator=generate, opener=lambda *_: True)
    jobs = [
        manager.submit(session_id)
        for session_id in (
            SESSION_ID,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
        )
    ]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and active < 2:
        time.sleep(0.01)
    assert active == maximum == 2
    stopping = threading.Thread(target=manager.shutdown, kwargs={"timeout": 1})
    stopping.start()
    time.sleep(0.02)
    assert stopping.is_alive()
    release.set()
    stopping.join(timeout=2)
    assert not stopping.is_alive()
    assert all(manager.get(job.job_id).status == "ready" for job in jobs)


def test_doctor_reports_trace_cache_count_bytes_oldest_and_purge_failures(trace_env):
    _, cache, fingerprint = trace_env
    path = _artifact(cache, fingerprint)
    items = []
    doctor._trace_cache(items)
    values = {item["check_name"]: item["value"] for item in items}
    assert values["trace_cache_count"] == "1"
    assert values["trace_cache_bytes"] == str(path.stat().st_size)
    assert values["trace_cache_oldest_access"] != "none"
    assert values["trace_cache_purge_failures"] == "0"


def test_trace_cache_limits_use_central_config_allowlist(monkeypatch):
    assert {
        "METSUKE_TRACE_CACHE_MAX_MB",
        "METSUKE_TRACE_CACHE_MAX_AGE_DAYS",
    } <= set(config.CONFIG_KEYS)
    monkeypatch.setenv("METSUKE_TRACE_CACHE_MAX_MB", "12")
    monkeypatch.setenv("METSUKE_TRACE_CACHE_MAX_AGE_DAYS", "9")
    assert config.trace_cache_max_bytes() == 12 * 1024 * 1024
    assert config.trace_cache_max_age_days() == 9
