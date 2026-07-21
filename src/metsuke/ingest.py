"""Ingester: replays the archive manifest into ledger.db (single writer).

The archive is the ONLY input — live ingest and `metsuke rebuild` are the same code
path, so "not archived" implies "not analyzed" and the ledger is always
reconstructible. Intake rules (docs/02 §2) are enforced here and frozen by
golden-fixture tests:
  1. dedupe by requestId (one API response spans multiple assistant records)
  2. branches kept (billing truth), narrative flag deferred
  3. subagents linked via agentId + meta.json toolUseId
  4. unknown/unparsable lines quarantined, never dropped
  5. lineage = session × agent
  6. interrupted requests: output_tokens is trusted iff stop_reason is set;
     NULL only for a genuine mid-stream cut, while is_interrupted is always recorded
  7. (OTel enrichment — Stage 5)
  8. agent requests: causal prompt_id via parent_tool_use_id overrides the
     time-sliced observed value; unresolved chains keep the observed value
  9. task-notification pseudo-prompts fold into their spawn-origin prompt via
     tool-use-id, then task-id fallback; head-anchored only, unresolved kept
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import archiver, config, ledger
from .redaction import REDACTION_VERSION, redact

PARSER_VERSION = 10
_MODEL_DATE_SUFFIX = re.compile(r"-\d{8}$")
_TASK_NOTIFICATION_HEAD = re.compile(
    r"\A\s*(?:\[SYSTEM NOTIFICATION[^\]]*\]\s*)?<task-notification>"
)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_TASK_ID = re.compile(r"<task-id>([0-9a-f]+)</task-id>")
_IGNORED_TRANSCRIPT_TYPES = {
    "ai-title",
    "attachment",
    "file-history-delta",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
    "progress",
    "queue-operation",
    "summary",
    "system",
}
_OTEL_SAFE_ATTRIBUTES = {
    "event.name", "event.sequence", "session.id", "prompt.id", "request_id",
    "model", "effort", "query_source", "speed", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cost_usd", "duration_ms",
    "error", "status_code", "attempt", "agent.name", "skill.name", "plugin.name",
    "marketplace.name", "mcp_server.name", "mcp_tool.name", "workflow.run_id",
}
TASK_LABELS = {"feature", "incident", "design", "refactor", "chore"}
TASK_OUTCOMES = {"completed", "partial", "abandoned"}


def _purge_now_ignored_quarantine(conn) -> None:
    """Drop only legacy unknown-type quarantines now classified as metadata."""
    reasons = [
        repr(ValueError(f"unknown transcript record type: {record_type!r}"))
        for record_type in sorted(_IGNORED_TRANSCRIPT_TYPES)
    ]
    placeholders = ",".join("?" for _ in reasons)
    conn.execute(
        f"""DELETE FROM quarantine
             WHERE reason IN ({placeholders})""",
        reasons,
    )


def _quarantine_text(value: bytes | str) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return redact(text)[0][:4000]


@dataclass
class IngestStats:
    segments: int = 0
    records: int = 0
    quarantined: int = 0
    new_models: list[str] = field(default_factory=list)


def _norm_model(model: str | None) -> str | None:
    if not model:
        return None
    return _MODEL_DATE_SUFFIX.sub("", model)


def _ts(record: dict) -> float | None:
    t = record.get("timestamp")
    if not t:
        return None
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _path_context(rel: str) -> dict:
    """Derive project/session/agent identity from the archive-relative path."""
    parts = Path(rel).parts
    ctx = {"project": parts[0] if parts else None, "agent_id": None, "session_id": None}
    if "subagents" in parts:
        i = parts.index("subagents")
        ctx["session_id"] = parts[i - 1] if i >= 1 else None
        stem = Path(parts[-1]).name
        m = re.match(r"agent-([0-9a-f]+)", stem)
        if m:
            ctx["agent_id"] = m.group(1)
    elif len(parts) == 2 and parts[1].endswith(".jsonl"):
        ctx["session_id"] = Path(parts[1]).stem
    return ctx


def run(conn=None, from_start: bool = False) -> IngestStats:
    own = conn is None
    if own:
        conn = ledger.connect()
    stats = IngestStats()
    _archive_spool()
    known = ledger.known_models(conn)
    _purge_now_ignored_quarantine(conn)
    # prompt attribution state (assistant records carry no promptId on real data)
    state: dict[str, str] = {
        r[0]: r[1] for r in conn.execute("SELECT lineage_id, prompt_id FROM lineage_state")
    }

    pos = 0
    byte_offset = 0
    if not from_start:
        row = conn.execute("SELECT value FROM meta WHERE key='manifest_pos'").fetchone()
        pos = int(row[0]) if row else 0
        row = conn.execute(
            "SELECT value FROM meta WHERE key='manifest_byte_offset'"
        ).fetchone()
        byte_offset = int(row[0]) if row else 0

    mp = config.manifest_path()
    if not mp.exists():
        _derive_workflow_links(conn)
        _derive_prompt_attribution(conn)
        _derive_nudges(conn)
        _derive_judgments(conn)
        _derive_commits(conn)
        _derive_otel(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('redaction_version', ?)",
            (str(REDACTION_VERSION),),
        )
        conn.execute(
            "INSERT INTO ingest_log VALUES (?,?,?,?,?,?)",
            (time.time(), 0, 0, 0, 0, PARSER_VERSION),
        )
        conn.commit()
        return stats
    with open(mp, "rb") as f:
        if byte_offset == 0 and pos > 0:
            # One-time migration from the legacy line cursor.
            for _ in range(pos):
                if not f.readline():
                    raise ValueError("manifest line cursor exceeds file length")
            byte_offset = f.tell()
        if byte_offset > mp.stat().st_size:
            raise ValueError("manifest byte cursor exceeds file length")
        f.seek(byte_offset)
        lines = f.readlines()
        new_byte_offset = f.tell()

    for entry_line in lines:
        if not entry_line.strip():
            continue
        entry = json.loads(entry_line)
        stats.segments += 1
        if stats.segments % 200 == 0:
            conn.commit()  # bound WAL size on large replays; upserts keep this idempotent
        if entry["kind"] == "hooks":
            _ingest_hooks(conn, entry, stats)
            continue
        if entry["kind"] == "otel":
            _ingest_otel(conn, entry, stats)
            continue
        if entry["kind"] == "snapshot" and entry["path"].endswith(".meta.json"):
            _ingest_meta_json(conn, entry, stats)
            continue
        if entry["kind"] != "jsonl":
            continue
        ctx = _path_context(entry["path"])
        seg = config.segments_dir() / entry["seg"]
        try:
            raw = archiver._decompress(seg.read_bytes(), entry.get("codec", "zstd"))
        except OSError as e:
            conn.execute(
                "INSERT INTO quarantine VALUES (?,?,?,?)",
                (time.time(), entry["seg"], f"segment read failed: {e}", ""),
            )
            stats.quarantined += 1
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            stats.records += 1
            try:
                rec = json.loads(line)
                _ingest_record(conn, rec, ctx, entry["path"], known, stats, state)
            except Exception as e:  # quarantine, never drop
                conn.execute(
                    "INSERT INTO quarantine VALUES (?,?,?,?)",
                    (
                        time.time(), entry["path"], repr(e)[:200],
                        _quarantine_text(line),
                    ),
                )
                stats.quarantined += 1

    _derive_workflow_links(conn)
    _derive_prompt_attribution(conn)
    for lineage_id, pid in state.items():
        conn.execute(
            "INSERT OR REPLACE INTO lineage_state VALUES (?,?)", (lineage_id, pid)
        )
    new_pos = pos + len(lines)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('manifest_pos', ?)", (str(new_pos),)
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('manifest_byte_offset', ?)",
        (str(new_byte_offset),),
    )
    conn.execute(
        "INSERT INTO ingest_log VALUES (?,?,?,?,?,?)",
        (time.time(), new_pos, stats.segments, stats.records, stats.quarantined, PARSER_VERSION),
    )
    _derive_nudges(conn)
    _derive_judgments(conn)
    _derive_commits(conn)
    _derive_otel(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('redaction_version', ?)",
        (str(REDACTION_VERSION),),
    )
    conn.commit()
    if own:
        conn.close()
    return stats


def _archive_spool() -> None:
    """Batch complete hook files into one immutable segment, archive first."""
    config.ensure_dirs()
    spool = config.hooks_spool_dir()
    if not spool.exists():
        return
    files = sorted(spool.glob("*.ndjson"))
    if not files:
        return
    readable = []
    chunks = []
    for path in files:
        try:
            chunks.append(path.read_bytes())
            readable.append(path)
        except OSError:
            continue
    if not chunks:
        return
    raw = b"".join(chunk if chunk.endswith(b"\n") else chunk + b"\n" for chunk in chunks)
    name = f"batch-{time.time_ns()}-{os.getpid()}.ndjson"
    try:
        with open(config.manifest_path(), "a") as manifest:
            os.chmod(config.manifest_path(), config.FILE_MODE)
            archiver.archive_bytes(f"__hooks__/{name}", raw, "hooks", manifest, 1)
            manifest.flush()
            os.fsync(manifest.fileno())
        for path in readable:
            path.unlink(missing_ok=True)
    except OSError:
        return


def _ingest_hooks(conn, entry, stats) -> None:
    seg = config.segments_dir() / entry["seg"]
    try:
        raw = archiver._decompress(seg.read_bytes(), entry.get("codec", "zstd"))
    except Exception as exc:
        conn.execute("INSERT INTO quarantine VALUES (?,?,?,?)", (time.time(), entry["path"], repr(exc)[:200], ""))
        stats.quarantined += 1
        return
    for line in raw.splitlines():
        if not line.strip():
            continue
        stats.records += 1
        try:
            rec = json.loads(line)
            payload = rec.get("payload") or {}
            if not isinstance(payload, dict) or not rec.get("metsuke_event"):
                raise ValueError("invalid hook envelope")
            ts = float(rec["metsuke_ts"])
            clean, _ = redact(line.decode("utf-8", "replace"))
            conn.execute(
                "INSERT OR IGNORE INTO hook_event VALUES (?,?,?,?,?)",
                (ts, rec["metsuke_event"], payload.get("session_id"), payload.get("prompt_id"), clean),
            )
        except Exception as exc:
            conn.execute(
                "INSERT INTO quarantine VALUES (?,?,?,?)",
                (time.time(), entry["path"], repr(exc)[:200], _quarantine_text(line)),
            )
            stats.quarantined += 1


def _otel_value(value: dict):
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _otel_attributes(record: dict) -> dict:
    result = {}
    for item in record.get("attributes") or []:
        if isinstance(item, dict) and item.get("key"):
            result[item["key"]] = _otel_value(item.get("value"))
    return result


def _otel_resource_attributes(resource: dict) -> dict:
    value = resource.get("resource") if isinstance(resource, dict) else None
    return _otel_attributes(value) if isinstance(value, dict) else {}


def _int_value(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_value(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ingest_otel(conn, entry, stats) -> None:
    seg = config.segments_dir() / entry["seg"]
    try:
        raw = archiver._decompress(seg.read_bytes(), entry.get("codec", "zstd"))
    except Exception as exc:
        conn.execute(
            "INSERT INTO quarantine VALUES (?,?,?,?)",
            (time.time(), entry["path"], repr(exc)[:200], ""),
        )
        stats.quarantined += 1
        return
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            records = [
                (record, _otel_resource_attributes(resource))
                for resource in envelope["resourceLogs"]
                for scope in resource["scopeLogs"]
                for record in scope["logRecords"]
            ]
            if not isinstance(records, list):
                raise ValueError("OTLP logRecords must be a list")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            conn.execute(
                "INSERT INTO quarantine VALUES (?,?,?,?)",
                (
                    time.time(), entry["path"], repr(exc)[:200],
                    _quarantine_text(line),
                ),
            )
            stats.quarantined += 1
            continue
        for record, resource_attrs in records:
            stats.records += 1
            try:
                attrs = resource_attrs | _otel_attributes(record)
                kind = attrs.get("event.name")
                if kind not in {"api_request", "api_error"}:
                    continue
                request_id = attrs.get("request_id")
                session_id = attrs.get("session.id") or attrs.get("session_id")
                sequence = attrs.get("event.sequence")
                if sequence is None:
                    sequence = attrs.get("event_sequence")
                if request_id is None and (not session_id or sequence is None):
                    raise ValueError("OTel event missing request_id or session/sequence identity")
                dedup = request_id or f"{session_id or ''}:{sequence if sequence is not None else ''}:{kind}"
                ts = int(record["timeUnixNano"]) / 1e9
                safe_attrs = {
                    key: value for key, value in attrs.items()
                    if key in _OTEL_SAFE_ATTRIBUTES and value is not None
                }
                raw_record = json.dumps(
                    {"timeUnixNano": record["timeUnixNano"], "attributes": safe_attrs},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO otel_event
                       (ts,kind,session_id,request_id,prompt_id,model,effort,query_source,speed,
                        input_tok,output_tok,cache_read_tok,cache_creation_tok,cost_usd_sdk,
                        duration_ms,error,status_code,dedup_key,raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ts, kind, session_id, request_id,
                        attrs.get("prompt.id") or attrs.get("prompt_id"),
                        attrs.get("model"), attrs.get("effort"), attrs.get("query_source"),
                        attrs.get("speed"), _int_value(attrs.get("input_tokens")),
                        _int_value(attrs.get("output_tokens")),
                        _int_value(attrs.get("cache_read_tokens")),
                        _int_value(attrs.get("cache_creation_tokens")),
                        _float_value(attrs.get("cost_usd")),
                        _float_value(attrs.get("duration_ms")), attrs.get("error"),
                        str(attrs["status_code"]) if attrs.get("status_code") is not None else None,
                        str(dedup), raw_record,
                    ),
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                conn.execute(
                    "INSERT INTO quarantine VALUES (?,?,?,?)",
                    (
                        time.time(), entry["path"], repr(exc)[:200],
                        _quarantine_text(json.dumps(record)),
                    ),
                )
                stats.quarantined += 1


def _ingest_meta_json(conn, entry, stats) -> None:
    seg = config.segments_dir() / entry["seg"]
    try:
        meta = json.loads(archiver._decompress(seg.read_bytes(), entry.get("codec", "zstd")))
    except Exception as e:
        conn.execute(
            "INSERT INTO quarantine VALUES (?,?,?,?)",
            (time.time(), entry["path"], repr(e)[:200], ""),
        )
        stats.quarantined += 1
        return
    ctx = _path_context(entry["path"])
    m = re.match(r"agent-([0-9a-f]+)", Path(entry["path"]).name)
    agent_id = m.group(1) if m else None
    workflow = re.search(r"(?:^|/)subagents/workflows/(wf_[^/]+)/", entry["path"])
    workflow_run_id = workflow.group(1) if workflow else None
    if agent_id:
        conn.execute(
            """INSERT INTO agent
               (agent_id, session_id, agent_type, parent_tool_use_id, spawn_depth, workflow_run_id)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 agent_type=COALESCE(excluded.agent_type, agent.agent_type),
                 parent_tool_use_id=COALESCE(excluded.parent_tool_use_id, agent.parent_tool_use_id),
                 spawn_depth=COALESCE(excluded.spawn_depth, agent.spawn_depth),
                 workflow_run_id=COALESCE(excluded.workflow_run_id, agent.workflow_run_id)""",
            (
                agent_id,
                ctx["session_id"],
                meta.get("agentType"),
                meta.get("toolUseId"),
                meta.get("spawnDepth"),
                workflow_run_id,
            ),
        )


def _ingest_record(
    conn, rec: dict, ctx: dict, rel: str, known: set, stats: IngestStats, state: dict
) -> None:
    rtype = rec.get("type")
    session_id = rec.get("sessionId") or ctx["session_id"]
    agent_id = rec.get("agentId") or ctx["agent_id"]
    lineage = f"{session_id}/{agent_id}" if agent_id else session_id
    ts = _ts(rec)

    if session_id:
        conn.execute(
            """INSERT INTO session (session_id, project, slug, git_branch, cc_version, first_ts, last_ts)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 slug=COALESCE(excluded.slug, session.slug),
                 git_branch=COALESCE(excluded.git_branch, session.git_branch),
                 cc_version=COALESCE(excluded.cc_version, session.cc_version),
                 first_ts=MIN(COALESCE(session.first_ts, excluded.first_ts), COALESCE(excluded.first_ts, session.first_ts)),
                 last_ts=MAX(COALESCE(session.last_ts, excluded.last_ts), COALESCE(excluded.last_ts, session.last_ts))""",
            (session_id, ctx["project"], rec.get("slug"), rec.get("gitBranch"), rec.get("version"), ts, ts),
        )
    if rec.get("version"):
        conn.execute(
            "INSERT OR IGNORE INTO regime_event VALUES (?,?,?)",
            (ts or time.time(), "cc_version", rec["version"]),
        )

    if rtype == "assistant":
        _ingest_assistant(conn, rec, session_id, agent_id, lineage, ts, rel, known, stats, state)
    elif rtype == "user":
        if rec.get("promptId"):
            state[lineage] = rec["promptId"]
        _ingest_user(conn, rec, session_id, agent_id, ts)
    elif rtype not in _IGNORED_TRANSCRIPT_TYPES:
        raise ValueError(f"unknown transcript record type: {rtype!r}")


def _ingest_assistant(
    conn, rec, session_id, agent_id, lineage, ts, rel, known, stats, state
) -> None:
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    request_id = rec.get("requestId") or msg.get("id")
    if not request_id or usage is None:
        raise ValueError("assistant record missing request id or usage")
    prompt_id = rec.get("promptId") or state.get(lineage)
    model = _norm_model(msg.get("model"))
    synthetic = 1 if (msg.get("model") == "<synthetic>" or rec.get("isApiErrorMessage")) else 0
    if model and not synthetic and model not in known:
        conn.execute(
            "INSERT OR IGNORE INTO regime_event VALUES (?,?,?)",
            (ts or time.time(), "model_new", model),
        )
        known.add(model)
        stats.new_models.append(model)

    cc_detail = usage.get("cache_creation") or {}
    w5m = cc_detail.get("ephemeral_5m_input_tokens")
    w1h = cc_detail.get("ephemeral_1h_input_tokens")
    if w5m is None and w1h is None:
        # no TTL breakdown observed: fall back to the calibrated 1h (2.0x) assumption
        w5m, w1h = 0, usage.get("cache_creation_input_tokens", 0)

    conn.execute(
        """INSERT INTO request
           (request_id, message_id, session_id, agent_id, lineage_id, prompt_id, ts, model,
            input_tok, output_tok, cache_read_tok, cache_w5m_tok, cache_w1h_tok,
            server_tool_use, service_tier, speed, geo, stop_reason,
            is_synthetic, is_interrupted, on_main_path, source, parser_version, raw_path,
            end_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,'transcript',?,?,?)
           ON CONFLICT(request_id) DO UPDATE SET
             message_id=CASE WHEN request.source='otel' THEN excluded.message_id ELSE request.message_id END,
             session_id=CASE WHEN request.source='otel' THEN excluded.session_id ELSE request.session_id END,
             agent_id=CASE WHEN request.source='otel' THEN excluded.agent_id ELSE request.agent_id END,
             lineage_id=CASE WHEN request.source='otel' THEN excluded.lineage_id ELSE request.lineage_id END,
             prompt_id=CASE WHEN request.source='otel' THEN excluded.prompt_id ELSE request.prompt_id END,
             ts=CASE WHEN request.source='otel' THEN excluded.ts ELSE request.ts END,
             model=CASE WHEN request.source='otel' THEN excluded.model ELSE request.model END,
             input_tok=CASE WHEN request.source='otel' THEN excluded.input_tok ELSE request.input_tok END,
             output_tok=CASE WHEN request.source='otel' THEN excluded.output_tok
                             WHEN request.is_interrupted=1
                                  AND COALESCE(excluded.stop_reason,
                                               request.stop_reason) IS NULL THEN NULL
                             ELSE MAX(COALESCE(request.output_tok,0),COALESCE(excluded.output_tok,0)) END,
             cache_read_tok=CASE WHEN request.source='otel' THEN excluded.cache_read_tok ELSE request.cache_read_tok END,
             cache_w5m_tok=CASE WHEN request.source='otel' THEN excluded.cache_w5m_tok ELSE request.cache_w5m_tok END,
             cache_w1h_tok=CASE WHEN request.source='otel' THEN excluded.cache_w1h_tok ELSE request.cache_w1h_tok END,
             server_tool_use=CASE WHEN request.source='otel' THEN excluded.server_tool_use ELSE request.server_tool_use END,
             service_tier=CASE WHEN request.source='otel' THEN excluded.service_tier ELSE request.service_tier END,
             speed=CASE WHEN request.source='otel' THEN excluded.speed ELSE request.speed END,
             geo=CASE WHEN request.source='otel' THEN excluded.geo ELSE request.geo END,
             stop_reason=CASE WHEN request.source='otel' THEN excluded.stop_reason
                              ELSE COALESCE(excluded.stop_reason,request.stop_reason) END,
             is_synthetic=CASE WHEN request.source='otel' THEN excluded.is_synthetic ELSE request.is_synthetic END,
             on_main_path=CASE WHEN request.source='otel' THEN excluded.on_main_path ELSE request.on_main_path END,
             source=CASE WHEN request.source='otel' THEN excluded.source ELSE request.source END,
             parser_version=CASE WHEN request.source='otel' THEN excluded.parser_version ELSE request.parser_version END,
             raw_path=CASE WHEN request.source='otel' THEN excluded.raw_path ELSE request.raw_path END,
             end_ts=CASE
               WHEN request.source='otel' THEN excluded.end_ts
               WHEN excluded.raw_path != request.raw_path THEN request.end_ts
               WHEN request.end_ts IS NULL THEN excluded.end_ts
               WHEN excluded.end_ts IS NULL THEN request.end_ts
               ELSE MAX(request.end_ts, excluded.end_ts) END,
             api_duration_ms=COALESCE(request.api_duration_ms, excluded.api_duration_ms)""",
        (
            request_id,
            msg.get("id"),
            session_id,
            agent_id,
            lineage,
            prompt_id,
            ts,
            model,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            w5m or 0,
            w1h or 0,
            json.dumps(usage.get("server_tool_use")) if usage.get("server_tool_use") else None,
            usage.get("service_tier"),
            usage.get("speed"),
            usage.get("inference_geo"),
            msg.get("stop_reason"),
            synthetic,
            PARSER_VERSION,
            rel,
            ts,
        ),
    )
    # tool_use blocks: register calls (name lives here, results arrive in user records)
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                file_path, lines_changed = _tool_change(block.get("name"), block.get("input"))
                conn.execute(
                    """INSERT INTO tool_call
                       (tool_use_id,request_id,session_id,agent_id,prompt_id,name,ts,
                        file_path,lines_changed)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tool_use_id) DO UPDATE SET
                         request_id=COALESCE(tool_call.request_id, excluded.request_id),
                         session_id=COALESCE(tool_call.session_id, excluded.session_id),
                         agent_id=COALESCE(tool_call.agent_id, excluded.agent_id),
                         prompt_id=COALESCE(tool_call.prompt_id, excluded.prompt_id),
                         name=COALESCE(tool_call.name, excluded.name),
                         ts=COALESCE(tool_call.ts, excluded.ts),
                         file_path=COALESCE(tool_call.file_path, excluded.file_path),
                         lines_changed=COALESCE(tool_call.lines_changed, excluded.lines_changed)""",
                    (
                        block["id"], request_id, session_id, agent_id, prompt_id,
                        block.get("name"), ts, file_path, lines_changed,
                    ),
                )


def _line_count(value) -> int:
    return str(value or "").count("\n") + 1


def _tool_change(name, tool_input) -> tuple[str | None, int | None]:
    if name not in {"Edit", "Write", "MultiEdit", "NotebookEdit"} or not isinstance(tool_input, dict):
        return None, None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if name == "Write":
        return path, _line_count(tool_input.get("content"))
    if name in {"Edit", "NotebookEdit"}:
        old = tool_input.get("old_string") or tool_input.get("old_source")
        new = tool_input.get("new_string") or tool_input.get("new_source")
        return path, max(_line_count(old), _line_count(new))
    changed = 0
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict):
            changed += max(_line_count(edit.get("old_string")), _line_count(edit.get("new_string")))
    return path, changed


def _ingest_user(conn, rec, session_id, agent_id, ts) -> None:
    # interruption marker: only a genuine mid-stream cut has placeholder output (rule 6)
    imid = rec.get("interruptedMessageId")
    if imid:
        conn.execute(
            "UPDATE request SET is_interrupted=1, "
            "output_tok=CASE WHEN stop_reason IS NULL THEN NULL ELSE output_tok END "
            "WHERE message_id=?",
            (imid,),
        )
        if rec.get("promptId"):
            conn.execute(
                "UPDATE prompt SET interrupted_message_id=? WHERE prompt_id=?",
                (imid, rec["promptId"]),
            )

    msg = rec.get("message") or {}
    content = msg.get("content")

    # tool results → enrich tool_call
    if isinstance(content, list):
        result_blocks = [
            block for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id")
        ]
        for block in result_blocks:
            if ts is not None:
                body = block.get("content")
                size = len(json.dumps(body)) if body is not None else 0
                workflow_run_id = None
                tur = rec.get("toolUseResult")
                if len(result_blocks) == 1 and isinstance(tur, dict) and tur.get("runId"):
                    workflow_run_id = tur["runId"]
                conn.execute(
                    """INSERT INTO tool_call
                       (tool_use_id,session_id,agent_id,is_error,result_bytes,result_ts,
                        workflow_run_id)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(tool_use_id) DO UPDATE SET
                         is_error=CASE WHEN tool_call.result_ts IS NULL
                                       THEN excluded.is_error ELSE tool_call.is_error END,
                         result_bytes=CASE WHEN tool_call.result_ts IS NULL
                                           THEN excluded.result_bytes ELSE tool_call.result_bytes END,
                         result_ts=COALESCE(tool_call.result_ts, excluded.result_ts),
                         workflow_run_id=COALESCE(tool_call.workflow_run_id,
                                                  excluded.workflow_run_id)""",
                    (
                        block["tool_use_id"], session_id, agent_id,
                        1 if block.get("is_error") else 0, size, ts, workflow_run_id,
                    ),
                )

    # agent spawn results (parent side): resolved model etc.
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict) and tur.get("agentId"):
        conn.execute(
            """INSERT INTO agent (agent_id, session_id, agent_type, resolved_model)
               VALUES (?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 resolved_model=COALESCE(excluded.resolved_model, agent.resolved_model),
                 agent_type=COALESCE(excluded.agent_type, agent.agent_type)""",
            (tur["agentId"], session_id, tur.get("agentType"), tur.get("resolvedModel")),
        )

    # the human prompt itself (first textual user record per promptId, main thread only)
    pid = rec.get("promptId")
    if pid and agent_id is None and not rec.get("isSidechain"):
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [b.get("text") for b in content
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            text = "\n".join(texts) if texts else None
        if text and not text.startswith("[Request interrupted"):
            from .redaction import redact

            clean, _ = redact(text)
            clean = clean[:20000]
            conn.execute(
                """INSERT INTO prompt (prompt_id, session_id, ts, text)
                   VALUES (?,?,?,?)
                   ON CONFLICT(prompt_id) DO NOTHING""",
                (pid, session_id, ts, clean),
            )


def _hook_prompt(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json).get("payload") or {}
        return str(payload.get("prompt") or "")
    except (TypeError, ValueError):
        return ""


def _derive_nudges(conn) -> None:
    """Materialize nudges and classify only behavior that is actually observed."""
    for row in conn.execute("SELECT ts,payload_json FROM hook_event WHERE kind='nudge_fired'"):
        try:
            payload = json.loads(row["payload_json"])["payload"]
            conn.execute(
                """INSERT OR IGNORE INTO nudge
                   (rule,fired_ts,session_id,detail_json,experiment_group)
                   VALUES (?,?,?,?,?)""",
                (
                    payload["rule"],
                    row["ts"],
                    payload.get("session_id") or "",
                    json.dumps(payload.get("detail") or {}, separators=(",", ":")),
                    payload.get("experiment_group") or "treatment",
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue

    cutoff = time.time() - 600
    rows = conn.execute(
        "SELECT * FROM nudge WHERE outcome IS NULL AND fired_ts<=?",
        (cutoff,),
    ).fetchall()
    measured = {
        "coldcache_warn",
        "ctx_warn",
        "budget_warn_50",
        "budget_warn_80",
        "budget_warn_100",
        "runaway_guard",
    }
    for nudge in rows:
        rule, fired, sid = nudge["rule"], nudge["fired_ts"], nudge["session_id"]
        decided = fired + 600
        outcome = "unknown"
        reason = "no_observable_action"
        observed = {}
        if rule not in measured:
            conn.execute(
                """UPDATE nudge SET followed=NULL,decided_ts=?,outcome='unknown',
                     outcome_reason='rule_not_measured',observed_json='{}'
                   WHERE rule=? AND fired_ts=? AND session_id=?""",
                (decided, rule, fired, sid),
            )
            continue
        prompts = conn.execute(
            """SELECT payload_json FROM hook_event
               WHERE kind='UserPromptSubmit' AND session_id=? AND ts>? AND ts<=? ORDER BY ts""",
            (sid, fired, decided),
        ).fetchall()
        handed_off = any(_hook_prompt(row[0]).lstrip().startswith("/handoff") for row in prompts)
        observed["next_prompt_count"] = len(prompts)
        observed["handoff"] = handed_off
        if handed_off:
            outcome, reason = "followed", "explicit_handoff"
        elif rule in {"budget_warn_50", "budget_warn_80", "budget_warn_100"}:
            before = conn.execute(
                """SELECT in_usd,effort FROM v_request_cost
                   WHERE session_id=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                (sid, fired),
            ).fetchone()
            if before:
                cheaper = conn.execute(
                    """SELECT 1 FROM v_request_cost
                       WHERE session_id=? AND ts>? AND ts<=? AND in_usd<? LIMIT 1""",
                    (sid, fired, decided, before[0]),
                ).fetchone()
                effort_rank = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
                lower_effort = False
                if before["effort"] in effort_rank:
                    later = conn.execute(
                        """SELECT effort FROM request
                           WHERE session_id=? AND ts>? AND ts<=? AND effort IS NOT NULL""",
                        (sid, fired, decided),
                    ).fetchall()
                    lower_effort = any(
                        row["effort"] in effort_rank
                        and effort_rank[row["effort"]] < effort_rank[before["effort"]]
                        for row in later
                    )
                observed["cheaper_model"] = bool(cheaper)
                observed["lower_effort"] = lower_effort
                if cheaper or lower_effort:
                    outcome, reason = "followed", (
                        "cheaper_model" if cheaper else "lower_effort"
                    )
            if outcome == "unknown" and prompts:
                outcome, reason = "not_followed", "continued_without_observed_reduction"
        elif rule == "runaway_guard":
            interrupted = conn.execute(
                """SELECT 1 FROM request WHERE session_id=?
                   AND COALESCE(end_ts,ts)>? AND COALESCE(end_ts,ts)<=?
                   AND is_interrupted=1 LIMIT 1""",
                (sid, fired, decided),
            ).fetchone()
            fanout = conn.execute(
                """SELECT 1 FROM request r WHERE r.session_id=? AND r.agent_id IS NOT NULL
                   AND r.ts>? AND r.ts<=? AND NOT EXISTS (
                     SELECT 1 FROM request old WHERE old.agent_id=r.agent_id AND old.ts<=?) LIMIT 1""",
                (sid, fired, decided, fired),
            ).fetchone()
            samples = conn.execute(
                """SELECT json_extract(payload_json,'$.payload.cost.total_cost_usd') cost
                   FROM hook_event WHERE session_id=? AND kind='statusline_sample'
                     AND ts>? AND ts<=? ORDER BY ts""",
                (sid, fired, decided),
            ).fetchall()
            costs = [float(row["cost"]) for row in samples if row["cost"] is not None]
            continued_cost = len(costs) >= 2 and costs[-1] - costs[0] >= 0.25
            observed.update(
                interrupted=bool(interrupted),
                new_fanout=bool(fanout),
                continued_cost=continued_cost,
            )
            if interrupted:
                outcome, reason = "followed", "request_interrupted"
            elif fanout:
                outcome, reason = "not_followed", "new_agent_fanout"
            elif continued_cost:
                outcome, reason = "not_followed", "cost_continued"
        elif prompts:
            outcome, reason = "not_followed", "continued_without_handoff"
        followed = 1 if outcome == "followed" else (0 if outcome == "not_followed" else None)
        conn.execute(
            """UPDATE nudge SET followed=?,decided_ts=?,outcome=?,outcome_reason=?,observed_json=?
               WHERE rule=? AND fired_ts=? AND session_id=?""",
            (
                followed, decided, outcome, reason,
                json.dumps(observed, separators=(",", ":")), rule, fired, sid,
            ),
        )


def _quarantine_judgment(conn, row, reason: str) -> None:
    raw = row["payload_json"]
    exists = conn.execute(
        "SELECT 1 FROM quarantine WHERE src='judgment' AND reason=? AND raw=? LIMIT 1",
        (reason, raw),
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO quarantine VALUES (?,?,?,?)",
            (row["ts"], "judgment", reason, raw),
        )


def _required(payload: dict, keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if key not in payload or payload[key] is None]
    if missing:
        raise ValueError(f"missing keys: {','.join(missing)}")


def _derive_judgments(conn) -> None:
    """Replay judgment events in logical order into their fact tables."""
    rows = conn.execute(
        "SELECT ts,payload_json FROM hook_event WHERE kind='judgment' ORDER BY ts,payload_json"
    ).fetchall()
    verdicts = {"win", "loss", "inconclusive"}
    outcomes = {"completed", "reverted", "abandoned", "partial"}
    for row in rows:
        try:
            envelope = json.loads(row["payload_json"])
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            kind = payload.get("kind")
            if kind == "marker_start":
                _required(payload, ("marker_id", "ts_start", "category", "hypothesis"))
                conn.execute(
                    """INSERT OR IGNORE INTO marker
                       (marker_id,ts_start,category,hypothesis,expected_effect)
                       VALUES (?,?,?,?,?)""",
                    (
                        payload["marker_id"],
                        payload["ts_start"],
                        payload["category"],
                        payload["hypothesis"],
                        payload.get("expected_effect"),
                    ),
                )
            elif kind == "marker_end":
                _required(payload, ("marker_id", "ts_end"))
                conn.execute(
                    "UPDATE marker SET ts_end=? WHERE marker_id=?",
                    (payload["ts_end"], payload["marker_id"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise ValueError("marker not found")
            elif kind == "marker_verdict":
                _required(payload, ("marker_id", "verdict", "decided_by", "verdict_ts"))
                if payload["verdict"] not in verdicts:
                    raise ValueError("invalid verdict")
                if payload["decided_by"] not in {"human", "ai+human"}:
                    raise ValueError("invalid decided_by")
                conn.execute(
                    """UPDATE marker SET verdict=?,decided_by=?,verdict_ts=?,saving_usd=?,
                         saving_low_usd=?,saving_high_usd=?,saving_basis=?,verdict_note=?
                       WHERE marker_id=?""",
                    (
                        payload["verdict"],
                        payload["decided_by"],
                        payload["verdict_ts"],
                        payload.get("saving_usd"),
                        payload.get("saving_low_usd"),
                        payload.get("saving_high_usd"),
                        payload.get("saving_basis"),
                        payload.get("note"),
                        payload["marker_id"],
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise ValueError("marker not found")
            elif kind == "outcome":
                _required(payload, ("prompt_id", "ts", "label", "source"))
                if payload["label"] not in outcomes or payload["source"] not in {"auto", "manual"}:
                    raise ValueError("invalid outcome")
                conn.execute(
                    """INSERT OR IGNORE INTO outcome
                       (prompt_id,ts,label,lines_added,lines_removed,commits,source)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        payload["prompt_id"],
                        payload["ts"],
                        payload["label"],
                        payload.get("lines_added"),
                        payload.get("lines_removed"),
                        payload.get("commits"),
                        payload["source"],
                    ),
                )
            elif kind == "task_label":
                _required(payload, ("prompt_id", "label"))
                if payload["label"] not in TASK_LABELS:
                    raise ValueError("invalid task label")
                conn.execute(
                    "UPDATE prompt SET task_label=? WHERE prompt_id=?",
                    (payload["label"], payload["prompt_id"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise ValueError("prompt not found")
            elif kind == "task_start":
                _required(payload, ("task_id", "title", "category", "ts_start"))
                if payload["category"] not in TASK_LABELS:
                    raise ValueError("invalid task category")
                conn.execute(
                    """INSERT OR IGNORE INTO work_task
                       (task_id,title,goal,category,project,ts_start,created_by)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        payload["task_id"], payload["title"], payload.get("goal"),
                        payload["category"], payload.get("project"), payload["ts_start"],
                        payload.get("created_by") or "human",
                    ),
                )
            elif kind == "task_attach":
                _required(payload, ("task_id", "prompt_id", "attached_ts"))
                if not conn.execute(
                    "SELECT 1 FROM work_task WHERE task_id=?", (payload["task_id"],)
                ).fetchone():
                    raise ValueError("task not found")
                if not conn.execute(
                    "SELECT 1 FROM prompt WHERE prompt_id=?", (payload["prompt_id"],)
                ).fetchone():
                    raise ValueError("prompt not found")
                conn.execute(
                    """INSERT INTO task_prompt
                       (task_id,prompt_id,attached_ts,source,confidence)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(prompt_id) DO UPDATE SET
                         task_id=excluded.task_id,attached_ts=excluded.attached_ts,
                         source=excluded.source,confidence=excluded.confidence""",
                    (
                        payload["task_id"], payload["prompt_id"], payload["attached_ts"],
                        payload.get("source") or "manual", payload.get("confidence", 1.0),
                    ),
                )
            elif kind == "task_finish":
                _required(payload, ("task_id", "ts_end", "outcome"))
                if payload["outcome"] not in TASK_OUTCOMES:
                    raise ValueError("invalid task outcome")
                quality = payload.get("quality_score")
                if quality is not None and (not isinstance(quality, int) or not 1 <= quality <= 5):
                    raise ValueError("quality_score must be 1..5")
                rework = payload.get("rework_minutes")
                if rework is not None and (
                    not isinstance(rework, (int, float)) or rework < 0
                ):
                    raise ValueError("rework_minutes must be non-negative")
                conn.execute(
                    """UPDATE work_task SET ts_end=?,status='finished',outcome=?,
                         quality_score=?,rework_minutes=?,note=? WHERE task_id=?""",
                    (
                        payload["ts_end"], payload["outcome"], quality,
                        rework, payload.get("note"),
                        payload["task_id"],
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise ValueError("task not found")
            elif kind == "roi_cost":
                _required(payload, ("cost_id", "ts", "cost_kind"))
                if payload.get("minutes") is None and payload.get("usd") is None:
                    raise ValueError("roi cost requires minutes or usd")
                conn.execute(
                    """INSERT OR IGNORE INTO roi_cost
                       (cost_id,ts,kind,minutes,usd,note,source) VALUES (?,?,?,?,?,?,?)""",
                    (
                        payload["cost_id"], payload["ts"], payload["cost_kind"],
                        payload.get("minutes"), payload.get("usd"), payload.get("note"),
                        payload.get("source") or "human",
                    ),
                )
            elif kind == "regime":
                _required(payload, ("ts", "regime_kind", "detail"))
                conn.execute(
                    "INSERT OR IGNORE INTO regime_event VALUES (?,?,?)",
                    (payload["ts"], payload["regime_kind"], payload["detail"]),
                )
            elif kind == "invoice":
                _required(payload, ("month", "billed_usd", "ts"))
                conn.execute(
                    "INSERT OR REPLACE INTO invoice(month,billed_usd,note,ts) VALUES (?,?,?,?)",
                    (payload["month"], payload["billed_usd"], payload.get("note"), payload["ts"]),
                )
            else:
                raise ValueError(f"unknown judgment kind: {kind}")
        except (KeyError, TypeError, ValueError) as exc:
            _quarantine_judgment(conn, row, str(exc))
    _derive_task_links(conn)


def _derive_task_links(conn) -> None:
    """Attach prompts exposed while a task was active, with bounded time fallback."""
    rows = conn.execute(
        "SELECT ts,session_id,prompt_id,payload_json FROM hook_event "
        "WHERE kind='UserPromptSubmit' ORDER BY ts"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"]).get("payload") or {}
            task_id = payload.get("metsuke_task_id")
        except (TypeError, ValueError):
            continue
        if not task_id or not conn.execute(
            "SELECT 1 FROM work_task WHERE task_id=?", (task_id,)
        ).fetchone():
            continue
        prompt_id = payload.get("prompt_id") or row["prompt_id"]
        confidence = 1.0
        if not prompt_id:
            prompt = conn.execute(
                """SELECT prompt_id,ABS(ts-?) distance FROM prompt
                   WHERE session_id=? AND ABS(ts-?)<=120 ORDER BY distance LIMIT 1""",
                (row["ts"], row["session_id"], row["ts"]),
            ).fetchone()
            if prompt:
                prompt_id = prompt["prompt_id"]
                confidence = max(0.5, 1.0 - float(prompt["distance"]) / 240.0)
        if prompt_id and conn.execute(
            "SELECT 1 FROM prompt WHERE prompt_id=?", (prompt_id,)
        ).fetchone():
            conn.execute(
                """INSERT INTO task_prompt
                   (task_id,prompt_id,attached_ts,source,confidence) VALUES (?,?,?,?,?)
                   ON CONFLICT(prompt_id) DO NOTHING""",
                (task_id, prompt_id, row["ts"], "active_task_hook", confidence),
            )


def _commit_payload(payload_json: str) -> dict | None:
    try:
        payload = json.loads(payload_json).get("payload")
        return payload if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None


def _derive_commits(conn) -> None:
    """Materialize git commits, attribute nearby prompts, and derive outcomes."""
    hooks = conn.execute(
        "SELECT ts,payload_json FROM hook_event WHERE kind='git_commit' ORDER BY ts,payload_json"
    ).fetchall()
    for row in hooks:
        payload = _commit_payload(row["payload_json"])
        if not payload or not payload.get("sha"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO commit_event
               (sha,ts,repo,repo_path,branch,subject,insertions,deletions,files_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                payload["sha"], row["ts"], payload.get("repo"), payload.get("repo_path"),
                payload.get("branch"), payload.get("subject"), payload.get("insertions"),
                payload.get("deletions"),
                json.dumps(payload.get("files") or [], ensure_ascii=False, separators=(",", ":")),
            ),
        )
    pending = conn.execute(
        "SELECT sha,ts,repo_path FROM commit_event WHERE prompt_id IS NULL"
    ).fetchall()
    for commit in pending:
        slug = (commit["repo_path"] or "").replace("/", "-")
        prompt = conn.execute(
            """SELECT p.prompt_id FROM prompt p JOIN session s ON s.session_id=p.session_id
               WHERE s.project=? AND p.ts<=? AND ?-p.ts<=21600
               ORDER BY p.ts DESC LIMIT 1""",
            (slug, commit["ts"], commit["ts"]),
        ).fetchone()
        if prompt:
            conn.execute(
                "UPDATE commit_event SET prompt_id=? WHERE sha=?",
                (prompt["prompt_id"], commit["sha"]),
            )
    conn.execute(
        """INSERT OR IGNORE INTO outcome
           (prompt_id,ts,label,lines_added,lines_removed,commits,source)
           SELECT prompt_id,ts,'completed',insertions,deletions,1,'auto'
           FROM commit_event WHERE prompt_id IS NOT NULL"""
    )
    revert_pattern = re.compile(r"This reverts commit ([0-9a-fA-F]+)")
    for row in hooks:
        payload = _commit_payload(row["payload_json"])
        if not payload:
            continue
        match = revert_pattern.search(str(payload.get("body") or ""))
        if not match:
            continue
        original = conn.execute(
            """SELECT prompt_id FROM commit_event
               WHERE sha LIKE ? AND prompt_id IS NOT NULL ORDER BY length(sha) LIMIT 1""",
            (match.group(1) + "%",),
        ).fetchone()
        if original:
            conn.execute(
                """INSERT INTO outcome
                   (prompt_id,ts,label,commits,source) VALUES (?,?,'reverted',1,'auto')
                   ON CONFLICT(prompt_id,ts,source) DO UPDATE SET
                     label='reverted',lines_added=NULL,lines_removed=NULL,commits=1""",
                (original["prompt_id"], row["ts"]),
            )


def _derive_otel(conn) -> None:
    """Converge native OTel requests with transcript billing truth (rule 7)."""
    events = conn.execute(
        "SELECT * FROM otel_event WHERE kind='api_request' AND request_id IS NOT NULL ORDER BY ts"
    ).fetchall()
    for event in events:
        existing = conn.execute(
            "SELECT 1 FROM request WHERE request_id=?", (event["request_id"],)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE request SET
                     query_source=COALESCE(query_source,?),
                     effort=COALESCE(effort,?),
                     cost_usd_sdk=COALESCE(cost_usd_sdk,?),
                     api_duration_ms=COALESCE(api_duration_ms,?)
                   WHERE request_id=?""",
                (
                    event["query_source"], event["effort"], event["cost_usd_sdk"],
                    event["duration_ms"],
                    event["request_id"],
                ),
            )
            continue
        if event["session_id"]:
            conn.execute(
                """INSERT OR IGNORE INTO session
                   (session_id,project,slug,git_branch,cc_version,first_ts,last_ts)
                   VALUES (?,NULL,NULL,NULL,NULL,?,?)""",
                (event["session_id"], event["ts"], event["ts"]),
            )
        conn.execute(
            """INSERT OR IGNORE INTO request
               (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
                cache_read_tok,cache_w5m_tok,cache_w1h_tok,speed,is_synthetic,is_interrupted,
                on_main_path,source,parser_version,query_source,effort,cost_usd_sdk,
                end_ts,api_duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,0,0,1,'otel',?,?,?,?,?,?)""",
            (
                event["request_id"], event["session_id"], event["session_id"],
                event["prompt_id"], event["ts"], _norm_model(event["model"]),
                event["input_tok"] or 0, event["output_tok"] or 0,
                event["cache_read_tok"] or 0, 0, event["cache_creation_tok"] or 0,
                event["speed"], PARSER_VERSION, event["query_source"], event["effort"],
                event["cost_usd_sdk"],
                event["ts"], event["duration_ms"],
            ),
        )


def _derive_workflow_links(conn) -> None:
    conn.execute(
        """UPDATE agent SET parent_tool_use_id = (
               SELECT tool_use_id FROM tool_call
               WHERE tool_call.workflow_run_id = agent.workflow_run_id
               ORDER BY ts, tool_use_id LIMIT 1)
           WHERE workflow_run_id IS NOT NULL"""
    )


def _derive_prompt_attribution(conn, max_iterations: int = 16) -> None:
    """Fold notifications and propagate spawn-time prompts to a fixed point."""
    notifications = []
    for prompt_id, prompt_text in conn.execute(
        "SELECT prompt_id, text FROM prompt WHERE text IS NOT NULL ORDER BY prompt_id"
    ):
        head = _TASK_NOTIFICATION_HEAD.match(prompt_text)
        if not head:
            continue
        close = prompt_text.find("</task-notification>", head.end())
        body = prompt_text[head.end():close if close >= 0 else None]
        tool = _TOOL_USE_ID.search(body)
        task = _TASK_ID.search(body)
        if tool or task:
            notifications.append(
                (prompt_id, tool.group(1) if tool else None, task.group(1) if task else None)
            )

    for _ in range(max_iterations):
        notification_changes = 0
        if notifications:
            values = ",".join("(?,?,?)" for _ in notifications)
            params = [value for pair in notifications for value in pair]
            conn.execute(
                f"""WITH notification(prompt_id, tool_use_id, agent_id) AS (VALUES {values}),
                    origin AS (
                      SELECT n.prompt_id AS notification_prompt_id,
                        COALESCE(
                          (SELECT tc.prompt_id FROM tool_call tc
                           WHERE tc.tool_use_id = n.tool_use_id),
                          (SELECT tc.prompt_id FROM agent a
                           JOIN tool_call tc ON tc.tool_use_id = a.parent_tool_use_id
                           WHERE a.agent_id = n.agent_id)
                        ) AS origin_prompt_id
                      FROM notification n)
                    UPDATE request AS r SET prompt_id = (
                      SELECT o.origin_prompt_id FROM origin o
                      WHERE o.notification_prompt_id = r.prompt_id)
                    WHERE EXISTS (
                      SELECT 1 FROM origin o
                      WHERE o.notification_prompt_id = r.prompt_id
                        AND o.origin_prompt_id IS NOT NULL
                        AND o.origin_prompt_id != o.notification_prompt_id)""",
                params,
            )
            notification_changes = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(
            """UPDATE request SET prompt_id = (
                   SELECT tc.prompt_id FROM agent a
                   JOIN tool_call tc ON tc.tool_use_id = a.parent_tool_use_id
                   WHERE a.agent_id = request.agent_id)
               WHERE agent_id IS NOT NULL
                 AND EXISTS (
                   SELECT 1 FROM agent a
                   JOIN tool_call tc ON tc.tool_use_id = a.parent_tool_use_id
                   WHERE a.agent_id = request.agent_id
                     AND tc.prompt_id IS NOT NULL)
                 AND prompt_id IS NOT (
                   SELECT tc.prompt_id FROM agent a
                   JOIN tool_call tc ON tc.tool_use_id = a.parent_tool_use_id
                   WHERE a.agent_id = request.agent_id)"""
        )
        request_changes = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(
            """UPDATE tool_call SET prompt_id = (
                   SELECT prompt_id FROM request
                   WHERE request.request_id = tool_call.request_id)
               WHERE EXISTS (
                   SELECT 1 FROM request
                   WHERE request.request_id = tool_call.request_id)
                 AND prompt_id IS NOT (
                   SELECT prompt_id FROM request
                   WHERE request.request_id = tool_call.request_id)"""
        )
        tool_changes = conn.execute("SELECT changes()").fetchone()[0]
        if notification_changes == 0 and request_changes == 0 and tool_changes == 0:
            break


def rebuild() -> IngestStats:
    """Drop ledger, replay entire manifest. Judgment tables survive via spool replay
    (none exist yet in Stage 1)."""
    p = ledger.db_path()
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    conn = ledger.connect()
    stats = run(conn, from_start=True)
    conn.close()
    return stats
