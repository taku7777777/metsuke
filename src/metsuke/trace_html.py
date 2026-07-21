"""Self-contained, read-only HTML trace generation (ADR 0006)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from importlib import resources
from pathlib import Path

from . import archiver, config, ingest, ledger
from .redaction import REDACTION_VERSION, redact

MAX_TEXT = 64 * 1024
HEAD_TEXT = 48 * 1024
TAIL_TEXT = 8 * 1024
DATA_MARKER = "/*__TRACE_DATA__*/"
# Increment this whenever trace_template.html or the serialized trace data contract
# changes in a way that makes an existing self-contained HTML file stale.
TRACE_TEMPLATE_SCHEMA_VERSION = 1
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; form-action 'none'; base-uri 'none'"
)
UNATTRIBUTED = "__unattributed__"
SESSION = "__session__"
STORY = "__story__"
LABEL_WIDTH = 180
PLOT_WIDTH = 1100
STORY_MIN_WIDTH = 60
STORY_GAP_WIDTH = 48


def safe_text(value: str) -> str:
    """Apply the indivisible read-boundary rule: redact fully, then truncate."""
    clean = redact(value)[0]
    if len(clean.encode("utf-8")) <= MAX_TEXT:
        return clean
    head = clean.encode("utf-8")[:HEAD_TEXT].decode("utf-8", "ignore")
    tail = clean.encode("utf-8")[-TAIL_TEXT:].decode("utf-8", "ignore")
    return f"{head}\n[… truncated …]\n{tail}"


def _sanitize(value):
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {safe_text(str(key)): _sanitize(item) for key, item in value.items()}
    return value


class TranscriptReader:
    """Read live transcripts first, using one manifest index for archive fallback."""

    def __init__(self) -> None:
        self._index: dict[str, list[dict]] = defaultdict(list)
        for entry in archiver.manifest_entries():
            self._index[entry["path"]].append(entry)

    def bytes(self, rel: str) -> bytes:
        try:
            return (config.source_dir() / rel).read_bytes()
        except OSError:
            pass
        entries = self._index.get(rel, [])
        if not entries:
            return b""
        try:
            return archiver.reconstruct(rel, entries=entries)
        except (OSError, ValueError):
            return b""

    def records(self, rel: str) -> list[dict]:
        rows = []
        for line in self.bytes(rel).splitlines():
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def extract(
        self, paths: list[str], request_ids: set[str], tool_ids: set[str]
    ) -> tuple[dict[str, str], set[str], dict[str, dict[str, str | None]]]:
        text_parts: dict[str, list[str]] = defaultdict(list)
        thinking_requests: set[str] = set()
        tool_io = {tool_id: {"input": None, "result": None} for tool_id in tool_ids}
        for path in paths:
            for record in self.records(path):
                message = record.get("message") or {}
                content = message.get("content")
                request_id = record.get("requestId") or message.get("id")
                if request_id in request_ids and isinstance(content, list):
                    texts = [
                        safe_text(block["text"])
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ]
                    if texts:
                        text_parts[request_id].extend(texts)
                    if any(
                        isinstance(block, dict) and block.get("type") == "thinking"
                        for block in content
                    ):
                        thinking_requests.add(request_id)
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    tool_id = block.get("id") if block.get("type") == "tool_use" else None
                    if tool_id in tool_io:
                        clean = _sanitize(block.get("input"))
                        tool_io[tool_id]["input"] = json.dumps(
                            clean, ensure_ascii=False, indent=2
                        )
                    result_id = (
                        block.get("tool_use_id") if block.get("type") == "tool_result" else None
                    )
                    if result_id in tool_io:
                        clean = _sanitize(block.get("content"))
                        if isinstance(clean, str):
                            tool_io[result_id]["result"] = clean
                        else:
                            tool_io[result_id]["result"] = json.dumps(
                                clean, ensure_ascii=False, indent=2
                            )
        req_text = {request_id: "\n".join(parts) for request_id, parts in text_parts.items()}
        return req_text, thinking_requests, tool_io


def _cost_parts(row) -> dict[str, float]:
    incoming = row["in_usd"] or 0
    return {
        "input": (row["input_tok"] or 0) * incoming / 1e6,
        "cache_read": (row["cache_read_tok"] or 0) * incoming * (row["cache_read_x"] or 0) / 1e6,
        "cache_w5m": (row["cache_w5m_tok"] or 0) * incoming * (row["cache_w5m_x"] or 0) / 1e6,
        "cache_w1h": (row["cache_w1h_tok"] or 0) * incoming * (row["cache_w1h_x"] or 0) / 1e6,
        "output": (row["output_tok"] or 0) * (row["out_usd"] or 0) / 1e6,
    }


def _model_color(model: str | None) -> str:
    for name, color in (
        ("fable", "#a78bfa"), ("opus", "#f59e0b"),
        ("sonnet", "#38bdf8"), ("haiku", "#34d399"),
    ):
        if name in (model or ""):
            return color
    return "#6b7280"


def _tool_color(name: str | None) -> str:
    value = name or ""
    if value in {"Task", "Workflow", "Agent"}:
        return "#c084fc"
    if value in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        return "#fb923c"
    if value in {"Bash", "BashOutput", "KillShell"}:
        return "#4ade80"
    if value in {"WebFetch", "WebSearch"}:
        return "#22d3ee"
    if value.startswith("mcp__"):
        return "#e879f9"
    if value in {"Read", "Grep", "Glob", "LS", "NotebookRead"}:
        return "#60a5fa"
    return "#9ca3af"


def _tick_step(duration: float) -> float:
    for step in (1, 2, 5, 10, 30, 60, 120, 300, 600, 1200, 3600):
        if duration / step <= 12:
            return step
    return 7200


def _span_geometry(
    requests: list[dict], tools: list[dict], agents: dict[str, dict],
    identity: dict[str, dict], hooks: list[dict], agent_cost_order: bool = False,
) -> dict:
    lanes: dict[str, dict] = {}
    for row in requests + tools:
        key = row.get("agent_id") or "__main__"
        lanes.setdefault(key, {"agent_id": row.get("agent_id"), "requests": [], "tools": []})
    for row in requests:
        lanes[row.get("agent_id") or "__main__"]["requests"].append(row)
    for row in tools:
        lanes[row.get("agent_id") or "__main__"]["tools"].append(row)
    for lane in lanes.values():
        values = [row.get("ts") for row in lane["requests"] + lane["tools"] if row.get("ts")]
        lane["first_ts"] = min(values) if values else math.inf
        lane["cost_usd"] = sum(row.get("cost_usd") or 0 for row in lane["requests"])
    if agent_cost_order:
        ordered = sorted(
            lanes.values(),
            key=lambda lane: (
                lane["agent_id"] is not None,
                -lane["cost_usd"] if lane["agent_id"] is not None else 0,
                lane["agent_id"] or "",
            ),
        )
    else:
        ordered = sorted(
            lanes.values(),
            key=lambda lane: (
                lane["agent_id"] is not None, lane["first_ts"], lane["agent_id"] or ""
            ),
        )
    measured_starts = []
    for row in requests:
        end = row.get("end_ts") or row.get("ts")
        if end is not None and row.get("api_duration_ms") is not None:
            measured_starts.append(end - row["api_duration_ms"] / 1000)
    all_ts = measured_starts
    all_ts += [value for row in requests for value in (row.get("ts"), row.get("end_ts")) if value]
    all_ts += [value for row in tools for value in (row.get("ts"), row.get("result_ts")) if value]
    t0, t1 = (min(all_ts), max(all_ts)) if all_ts else (0.0, 1.0)
    if t1 <= t0:
        t1 = t0 + 1
    duration = t1 - t0
    spans = []
    tool_spans = []
    lane_geometry = []
    clusters = []
    for lane_index, lane in enumerate(ordered):
        previous_end = None
        previous_request = None
        ordered_requests = sorted(
            lane["requests"], key=lambda item: (item.get("ts") or 0, item["request_id"])
        )
        for row in ordered_requests:
            end = row.get("end_ts") or row.get("ts") or t0
            measured = row.get("api_duration_ms") is not None
            if measured:
                start = end - row["api_duration_ms"] / 1000
            elif (
                previous_request
                and previous_request.get("stop_reason") == "tool_use"
                and not previous_request.get("is_synthetic")
            ):
                start = previous_end
            else:
                start = row.get("ts") or end
            start = min(start, end)
            x = LABEL_WIDTH + (start - t0) / duration * PLOT_WIDTH
            width = max(3, (end - start) / duration * PLOT_WIDTH)
            spans.append(
                {
                    "request_id": row["request_id"], "lane": lane_index,
                    "x": round(x, 3), "width": round(width, 3), "local_y": 4,
                    "height": 15, "color": _model_color(row.get("model")),
                    "measured": measured,
                    "cost_pcts": _cost_pcts(row.get("cost_parts") or {}),
                    "spark": identity.get(row["request_id"]),
                }
            )
            previous_end = end
            previous_request = row
        packed_ends: list[float] = []
        packed_tools = []
        for row in sorted(
            lane["tools"], key=lambda item: (item.get("ts") or 0, item["tool_use_id"])
        ):
            start = row.get("ts") or t0
            end = row.get("result_ts")
            x = LABEL_WIDTH + (start - t0) / duration * PLOT_WIDTH
            raw_width = (end - start) / duration * PLOT_WIDTH if end is not None else 0
            bar_w = max(11, raw_width)
            row_index = next(
                (index for index, last_end in enumerate(packed_ends) if x >= last_end + 2),
                len(packed_ends),
            )
            if row_index == len(packed_ends):
                packed_ends.append(x + bar_w)
            else:
                packed_ends[row_index] = x + bar_w
            item = {
                "tool_use_id": row["tool_use_id"], "lane": lane_index,
                "x": round(x, 3), "bar_w": round(bar_w, 3), "row": row_index,
                "local_y_collapsed": 22, "local_y_expanded": 22 + row_index * 14,
                "open": end is None, "error": bool(row.get("is_error")),
                "color": _tool_color(row.get("name")), "name": row.get("name"),
            }
            tool_spans.append(item)
            packed_tools.append(item)
        for item in packed_tools:
            if clusters and clusters[-1]["lane"] == lane_index and item["x"] - clusters[-1]["last_x"] < 11:
                clusters[-1]["count"] += 1
                clusters[-1]["last_x"] = item["x"]
            else:
                clusters.append({
                    "lane": lane_index, "x": item["x"], "count": 1,
                    "color": item["color"], "tool_use_id": item["tool_use_id"],
                    "last_x": item["x"],
                })
        agent = agents.get(lane["agent_id"] or "", {})
        label = "main" if lane["agent_id"] is None else f"↳ {agent.get('agent_type') or 'agent'}"
        rows = len(packed_ends)
        lane_geometry.append({
            "label": label, "sub": f"{len(lane['requests'])}req · ${lane['cost_usd']:.3f}",
            "base_h": 42, "expanded_h": 42 + rows * 14, "rows": rows,
        })
    for cluster in clusters:
        cluster.pop("last_x")
    height = 96 + 42 * len(ordered)
    step = _tick_step(duration)
    ticks = []
    tick = 0.0
    while tick <= duration + 0.001:
        ticks.append({
            "x": round(LABEL_WIDTH + tick / duration * PLOT_WIDTH, 3),
            "label": _elapsed(tick),
        })
        tick += step
    hook_glyphs = {
        "UserPromptSubmit": "▼", "Stop": "●", "Notification": "🔔", "PreCompact": "◆"
    }
    hook_marks = [
        {
            "x": round(LABEL_WIDTH + (hook["ts"] - t0) / duration * PLOT_WIDTH, 3),
            "glyph": hook_glyphs[hook["kind"]],
        }
        for hook in hooks
        if hook["kind"] in hook_glyphs and t0 <= hook["ts"] <= t1
    ]
    main = sorted(
        (row for row in requests if row.get("agent_id") is None),
        key=lambda row: row.get("ts") or 0,
    )
    peak = max((sum(row.get(key) or 0 for key in (
        "input_tok", "cache_read_tok", "cache_w5m_tok", "cache_w1h_tok"
    )) for row in main), default=1)
    context_points = []
    for row in main:
        end = row.get("end_ts") or row.get("ts") or t0
        x = LABEL_WIDTH + (end - t0) / duration * PLOT_WIDTH
        value = sum(row.get(key) or 0 for key in (
            "input_tok", "cache_read_tok", "cache_w5m_tok", "cache_w1h_tok"
        ))
        context_points.append((round(x, 3), round(44 - 40 * value / peak, 3)))
    return {
        "t0": t0, "t1": t1, "duration": duration, "width": LABEL_WIDTH + PLOT_WIDTH,
        "plot_width": PLOT_WIDTH,
        "height": height, "plot_x": LABEL_WIDTH, "lanes": lane_geometry,
        "spans": spans, "tools": tool_spans, "clusters": clusters,
        "ticks": ticks, "hook_marks": hook_marks,
        "context_peak": peak,
        "context_label": {"text": f"context main · peak {peak:,}"},
        "context_points": context_points,
    }


def _cost_pcts(parts: dict[str, float]) -> list[float]:
    keys = ("cache_read", "cache_w5m", "cache_w1h", "input", "output")
    total = sum(parts.get(key, 0) for key in keys)
    return [100 * parts.get(key, 0) / total if total else 0 for key in keys]


def _elapsed(value: float) -> str:
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"


def _svg(geometry: dict) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{LABEL_WIDTH} 0 '
        f'{PLOT_WIDTH} 48" width="{PLOT_WIDTH}" height="48" '
        'preserveAspectRatio="none">'
    ]
    points = [f"{x:.3f},{y:.3f}" for x, y in geometry["context_points"]]
    parts.append(
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#7aa2f7" '
        'stroke-width="2" vector-effect="non-scaling-stroke"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _data_url(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _prompt_key(prompt_id: str | None) -> str:
    return prompt_id if prompt_id is not None else UNATTRIBUTED


def _prompt_strip(prompts: list[dict], requests: list[dict], geometry: dict) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for request in requests:
        if request.get("prompt_id") is not None:
            grouped[request["prompt_id"]].append(request)
    active = [prompt for prompt in prompts if prompt["prompt_id"] in grouped]
    active.sort(key=lambda prompt: (prompt.get("ts") is None, prompt.get("ts") or 0,
                                    prompt["prompt_id"]))
    starts = []
    for prompt in active:
        rows = grouped[prompt["prompt_id"]]
        start = prompt.get("ts")
        if start is None:
            start = min((row.get("ts") for row in rows if row.get("ts") is not None),
                        default=geometry["t0"])
        starts.append(min(geometry["t1"], max(geometry["t0"], start)))
    strip = []
    for index, prompt in enumerate(active):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else geometry["t1"]
        rows = grouped[prompt["prompt_id"]]
        strip.append({
            "prompt_id": prompt["prompt_id"],
            "x": round(LABEL_WIDTH + (start - geometry["t0"]) /
                       geometry["duration"] * PLOT_WIDTH, 3),
            "width": round(max(0, end - start) / geometry["duration"] * PLOT_WIDTH, 3),
            "cost_usd": sum(row.get("cost_usd") or 0 for row in rows),
            "n_req": len(rows),
            "label": (prompt.get("text") or prompt["prompt_id"])[:60],
        })
    return strip


def _story_layout(
    prompts: list[dict], requests: list[dict], prompt_svgs: dict[str, dict]
) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for request in requests:
        if request.get("prompt_id") is not None:
            grouped[request["prompt_id"]].append(request)
    active = [
        prompt for prompt in prompts
        if prompt["prompt_id"] in grouped and prompt["prompt_id"] in prompt_svgs
    ]
    active.sort(key=lambda prompt: (prompt.get("ts") is None, prompt.get("ts") or 0,
                                    prompt["prompt_id"]))
    durations = [prompt_svgs[prompt["prompt_id"]]["geometry"]["duration"] for prompt in active]
    px_per_sec = PLOT_WIDTH / sum(durations) if sum(durations) else 1.0
    segments = []
    gaps = []
    offset = 0.0
    for index, prompt in enumerate(active):
        geometry = prompt_svgs[prompt["prompt_id"]]["geometry"]
        width = max(STORY_MIN_WIDTH, geometry["duration"] * px_per_sec)
        rows = grouped[prompt["prompt_id"]]
        timestamp = prompt.get("ts")
        segments.append({
            "prompt_id": prompt["prompt_id"],
            "x_offset": round(offset, 3),
            "width": round(width, 3),
            "duration": geometry["duration"],
            "cost_usd": sum(row.get("cost_usd") or 0 for row in rows),
            "n_req": len(rows),
            "time_label": (
                dt.datetime.fromtimestamp(timestamp).strftime("%H:%M")
                if timestamp is not None else "?"
            ),
            "label": (prompt.get("text") or prompt["prompt_id"])[:60],
        })
        offset += width
        if index + 1 < len(active):
            next_geometry = prompt_svgs[active[index + 1]["prompt_id"]]["geometry"]
            seconds = max(0, next_geometry["t0"] - geometry["t1"])
            gaps.append({
                "x": round(offset, 3), "seconds": seconds,
                "label": f"⋯ {seconds / 60:.1f}分",
            })
            offset += STORY_GAP_WIDTH
    return {
        "segments": segments, "gaps": gaps, "px_per_sec": px_per_sec,
        "min_segment_width": STORY_MIN_WIDTH, "gap_width": STORY_GAP_WIDTH,
        "total_width": round(offset, 3),
    }


def build_trace_data(conn, session_id: str, focus: str | None = None) -> dict | None:
    req_rows = conn.execute(
        "SELECT * FROM v_request_cost WHERE session_id=? ORDER BY ts,request_id", (session_id,)
    ).fetchall()
    if not req_rows:
        return None
    requests = []
    for row in req_rows:
        item = _sanitize(dict(row))
        item["cost_parts"] = _cost_parts(row)
        requests.append(item)
    tools = [_sanitize(dict(row)) for row in conn.execute(
        """SELECT tc.*,r.raw_path FROM tool_call tc
           LEFT JOIN request r ON r.request_id=tc.request_id
           WHERE tc.session_id=? ORDER BY tc.ts,tc.tool_use_id""", (session_id,)
    )]
    prompts = [_sanitize(dict(row)) for row in conn.execute(
        "SELECT * FROM prompt WHERE session_id=? ORDER BY ts,prompt_id", (session_id,)
    )]
    agent_rows = [_sanitize(dict(row)) for row in conn.execute(
        "SELECT * FROM agent WHERE session_id=? ORDER BY agent_id", (session_id,)
    )]
    agents = {row["agent_id"]: row for row in agent_rows}
    identity = {
        row["request_id"]: _sanitize(dict(row))
        for row in conn.execute(
            """SELECT ci.cause,ci.ts,ci.request_id,r.cache_write_usd
               FROM v_cache_identity ci JOIN v_request_cost r USING(request_id)
               WHERE ci.session_id=?""",
            (session_id,),
        )
    }
    hooks = [_sanitize(dict(row)) for row in conn.execute(
        "SELECT ts,kind FROM hook_event WHERE session_id=? ORDER BY ts", (session_id,)
    )]
    reader = TranscriptReader()
    raw_paths = sorted({row.get("raw_path") for row in requests if row.get("raw_path")})
    req_text, thinking_requests, tool_io = reader.extract(
        raw_paths, {row["request_id"] for row in requests}, {row["tool_use_id"] for row in tools}
    )
    prompt_svgs = {}
    keys = [_prompt_key(prompt["prompt_id"]) for prompt in prompts]
    keys.extend(_prompt_key(row.get("prompt_id")) for row in requests)
    if any(row.get("prompt_id") is None for row in requests):
        keys.append(UNATTRIBUTED)
    for key in dict.fromkeys(keys):
        prompt_id = None if key == UNATTRIBUTED else key
        group_requests = [row for row in requests if row.get("prompt_id") == prompt_id]
        group_tools = [row for row in tools if row.get("prompt_id") == prompt_id]
        if not group_requests and not group_tools:
            continue
        geometry = _span_geometry(group_requests, group_tools, agents, identity, hooks)
        svg = _svg(geometry)
        prompt_svgs[key] = {"data_url": _data_url(svg), "geometry": geometry}
    session_geometry = _span_geometry(
        requests, tools, agents, identity, hooks, agent_cost_order=True
    )
    session_geometry["prompt_strip"] = _prompt_strip(prompts, requests, session_geometry)
    prompt_svgs[SESSION] = {
        "data_url": _data_url(_svg(session_geometry)), "geometry": session_geometry
    }
    story_geometry = dict(session_geometry)
    story_geometry["story"] = _story_layout(prompts, requests, prompt_svgs)
    prompt_svgs[STORY] = {
        "data_url": prompt_svgs[SESSION]["data_url"], "geometry": story_geometry
    }
    redaction_row = conn.execute(
        "SELECT value FROM meta WHERE key='redaction_version'"
    ).fetchone()
    try:
        ledger_redaction_version = int(redaction_row[0]) if redaction_row else 0
    except (TypeError, ValueError):
        ledger_redaction_version = 0
    warning = None
    if ledger_redaction_version < REDACTION_VERSION:
        warning = "台帳は旧リダクション版 — metsuke rebuild を推奨"
    return {
        "session_id": safe_text(session_id), "requests": requests, "tools": tools,
        "prompts": prompts, "agents": agent_rows, "req_text": req_text,
        "req_thinking": {request_id: True for request_id in thinking_requests},
        "tool_io": tool_io,
        "prompt_svgs": prompt_svgs, "unattributed_key": UNATTRIBUTED,
        "total_usd": sum(row.get("cost_usd") or 0 for row in requests),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "redaction_version": REDACTION_VERSION, "parser_version": ingest.PARSER_VERSION,
        "trace_template_schema_version": TRACE_TEMPLATE_SCHEMA_VERSION,
        "session_last_request_at": max(
            (
                max(row.get("ts") or 0, row.get("end_ts") or row.get("ts") or 0)
                for row in requests
            ),
            default=0,
        ),
        "ledger_redaction_version": ledger_redaction_version, "warning": warning,
        "focus_request_id": safe_text(focus) if focus else None,
        "thresholds": {
            "prompt_warn_usd": config.float_value("METSUKE_PROMPT_WARN_USD", 3.0),
            "prompt_crit_usd": config.float_value("METSUKE_PROMPT_CRIT_USD", 7.5),
            "context_warn_tokens": config.int_value(
                "METSUKE_CONTEXT_WARN_TOKENS", 200_000
            ),
            "context_crit_tokens": config.int_value(
                "METSUKE_CONTEXT_CRIT_TOKENS", 500_000
            ),
        },
    }


def _json_blob(data: dict) -> str:
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return value.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _purge_old(directory: Path | None = None) -> None:
    directory = directory or config.traces_dir()
    if not directory.exists():
        return
    for path in directory.glob("*.html"):
        try:
            match = re.search(r'redaction_version(?:=|":)(\d+)', path.read_text(errors="ignore"))
            if match is None or int(match.group(1)) < REDACTION_VERSION:
                path.unlink()
        except OSError:
            path.unlink(missing_ok=True)


def _record_generation(session_id: str) -> None:
    spool = config.hooks_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    os.chmod(spool, config.DIR_MODE)
    path = spool / f"trace-{time.time_ns()}-{os.getpid()}.ndjson"
    payload = {
        "metsuke_event": "trace_html_generated", "metsuke_ts": time.time(),
        "payload": {"session_id": session_id},
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def target_path(session_id: str) -> Path | None:
    if (
        ".." in session_id
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", session_id)
    ):
        return None
    return config.traces_dir() / f"{session_id}.html"


def generate(
    session_id: str,
    focus: str | None = None,
    *,
    conn=None,
    record: bool = True,
    purge: bool = True,
    directory: Path | None = None,
) -> Path | None:
    target = target_path(session_id)
    if target is None:
        return None
    if directory is not None:
        target = directory / target.name
    owns_connection = conn is None
    trace_conn = ledger.connect_readonly() if owns_connection else conn
    try:
        data = build_trace_data(trace_conn, session_id, focus=focus)
    finally:
        if owns_connection:
            trace_conn.close()
    if data is None:
        return None
    output_directory = target.parent
    output_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(output_directory, config.DIR_MODE)
    if purge:
        _purge_old(output_directory)
    template = resources.files("metsuke").joinpath("trace_template.html").read_text()
    if template.count(DATA_MARKER) != 1:
        raise ValueError("trace template must contain exactly one data marker")
    html = template.replace(DATA_MARKER, _json_blob(data))
    tmp = output_directory / f".{session_id}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(html)
    os.replace(tmp, target)
    os.chmod(target, config.FILE_MODE)
    if record:
        _record_generation(session_id)
    return target


def _open_in_cmux(path: Path, fragment: str, cmux: str) -> bool:
    """Open a generated viewer in a dedicated cmux workspace."""
    workspace: str | None = None
    try:
        created = subprocess.run(
            [
                cmux,
                "workspace",
                "create",
                "--name",
                "metsuke viewer",
                "--cwd",
                str(path.parent),
                "--focus",
                "true",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        match = re.search(r"\bworkspace:[^\s]+", created.stdout + created.stderr)
        if match is None:
            return False
        workspace = match.group(0)
        subprocess.run(
            [
                cmux,
                "new-pane",
                "--type",
                "browser",
                "--workspace",
                workspace,
                "--url",
                path.as_uri() + fragment,
                "--focus",
                "true",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        if workspace is not None:
            try:
                subprocess.run(
                    [cmux, "workspace", "close", workspace],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError:
                pass
        return False
    return True


def open_browser(path: Path, fragment: str = "") -> bool:
    cmux = shutil.which("cmux") if os.environ.get("CMUX_WORKSPACE_ID") else None
    if cmux is not None:
        return _open_in_cmux(path, fragment, cmux)
    try:
        subprocess.run(["open", path.as_uri() + fragment], check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True
