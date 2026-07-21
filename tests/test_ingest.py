"""Golden-fixture tests for the intake rules — the quality gate of Stage 1.

Covers: requestId dedupe (multi-record split), branches counted, subagent
linkage (agentId + meta.json toolUseId), interrupted output conditioned on
stop_reason, synthetic excluded from cost, cost math vs hand-computed values,
rebuild determinism.
"""

import hashlib
import json
import datetime as dt

import pytest

from metsuke import archiver, config, ingest, ledger

def _rec(i, **kw):
    base = {
        "uuid": f"u{i}",
        "parentUuid": f"u{i - 1}" if i else None,
        "sessionId": "sess-1",
        "timestamp": f"2026-07-17T05:00:{i:02d}.000Z",
        "version": "2.0.14",
        "gitBranch": "main",
        "type": "assistant",
    }
    base.update(kw)
    # mirror real data: only user records carry promptId (verified 2026-07-17);
    # assistant attribution flows through lineage state
    if base["type"] == "user":
        base.setdefault("promptId", "prompt-1")
    return base


def _usage(inp=2, out=100, cr=1000, w5m=0, w1h=500):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": w5m + w1h,
        "cache_creation": {
            "ephemeral_5m_input_tokens": w5m,
            "ephemeral_1h_input_tokens": w1h,
        },
        "service_tier": "standard",
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "metsuke-home"
    src = tmp_path / "projects"
    src.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(src))
    return src


def _build_fixture(src):
    """One session: user prompt -> assistant (2 records, same requestId) with a
    tool_use -> tool_result -> Task spawn of a subagent -> interrupted assistant
    -> synthetic error record."""
    sess = src / "-Users-t-proj" / "sess-1.jsonl"
    lines = [
        # human prompt
        _rec(0, type="user", message={"role": "user", "content": "リポジトリを説明して"}),
        # result can arrive before its tool_use record (stub must later be filled)
        _rec(1, type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_early", "content": "first"}
        ]}),
        # one API response split into 2 records: SAME requestId, duplicated usage
        _rec(
            1,
            requestId="req_A",
            message={
                "id": "msg_A",
                "model": "claude-fable-5",
                "usage": _usage(out=50),
                "stop_reason": None,
                "content": [{"type": "text", "text": "見てみます"}],
            },
        ),
        _rec(
            2,
            requestId="req_A",
            message={
                "id": "msg_A",
                "model": "claude-fable-5",
                "usage": _usage(out=50),
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "toolu_02", "name": "Task", "input": {}},
                    {"type": "tool_use", "id": "toolu_early", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "toolu_wf", "name": "Workflow", "input": {}},
                ],
            },
        ),
        # tool result comes back in a user record
        _rec(
            3,
            type="user",
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01", "content": "file body"},
                    {"type": "tool_result", "tool_use_id": "toolu_early", "content": "second",
                     "is_error": True},
                ],
            },
        ),
        _rec(
            4,
            type="user",
            toolUseResult={"runId": "wf_w1", "status": "async_launched"},
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_wf", "content": "launched"}
            ]},
        ),
        # agent spawn result on parent side
        _rec(
            4,
            type="user",
            toolUseResult={
                "agentId": "abc123def456789ab",
                "agentType": "Explore",
                "resolvedModel": "claude-sonnet-5",
            },
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_02", "content": "agent done"}
            ]},
        ),
        # second API request, interrupted after a completed tool_use response
        _rec(
            5,
            requestId="req_B",
            message={
                "id": "msg_B",
                "model": "claude-fable-5",
                "usage": _usage(cr=1500, w1h=200, out=4),
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "続けます"}],
            },
        ),
        _rec(
            6,
            type="user",
            interruptedMessageId="msg_B",
            message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
        ),
        # API error -> synthetic record with zero usage
        _rec(
            7,
            requestId="req_ERR",
            isApiErrorMessage=True,
            message={
                "id": "msg_E",
                "model": "<synthetic>",
                "usage": _usage(inp=0, out=0, cr=0, w1h=0),
                "content": [{"type": "text", "text": "429"}],
            },
        ),
        _rec(8, type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_null", "content": "done"}
        ]}),
        _rec(17, type="user", promptId="prompt-notify", message={
            "role": "user", "content": (
                "<task-notification>\n"
                "<tool-use-id>toolu_null</tool-use-id>\n"
                "<task-id>abc123def456789ab</task-id>\n"
                "<task-id>3333333333333333a</task-id>\n"
                "</task-notification>"
            ),
        }),
        _rec(18, requestId="req_NOTIFY", message={
            "id": "msg_NOTIFY", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0), "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_wave", "name": "Task", "input": {}}],
        }),
        _rec(19, type="user", promptId="prompt-unknown", message={
            "role": "user", "content": (
                "[SYSTEM NOTIFICATION completed]\n\n<task-notification>\n"
                "<task-id>deadbeefdeadbeef</task-id>\n</task-notification>"
            ),
        }),
        _rec(20, requestId="req_UNKNOWN_NOTIFY", message={
            "id": "msg_UNKNOWN_NOTIFY", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0),
            "stop_reason": "end_turn", "content": [],
        }),
        _rec(21, type="user", promptId="prompt-quoted", message={
            "role": "user", "content": (
                "Explain this quoted payload:\n<task-notification>\n"
                "<task-id>abc123def456789ab</task-id>\n</task-notification>"
            ),
        }),
        _rec(22, requestId="req_QUOTED", message={
            "id": "msg_QUOTED", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0),
            "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "toolu_other", "name": "Task", "input": {}},
            ],
        }),
        _rec(25, type="user", promptId="prompt-mcp-notify", message={
            "role": "user", "content": (
                "<task-notification>\n"
                "<task-id>kil26dh7j</task-id>\n"
                "<tool-use-id>toolu_01</tool-use-id>\n"
                "</task-notification>"
            ),
        }),
        _rec(26, requestId="req_MCP_NOTIFY", message={
            "id": "msg_MCP_NOTIFY", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0),
            "stop_reason": "end_turn", "content": [],
        }),
        _rec(27, type="user", promptId="prompt-closed-notify", message={
            "role": "user", "content": (
                "<task-notification>completed without ids</task-notification>\n"
                "Quoted later: <tool-use-id>toolu_01</tool-use-id>"
            ),
        }),
        _rec(28, requestId="req_CLOSED_NOTIFY", message={
            "id": "msg_CLOSED_NOTIFY", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0),
            "stop_reason": "end_turn", "content": [],
        }),
    ]
    sess.parent.mkdir(parents=True)
    sess.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    unattributed = src / "-Users-t-proj" / "0-unattributed.jsonl"
    unattributed.write_text(json.dumps(_rec(
        8,
        requestId="req_NULL",
        isApiErrorMessage=True,
        message={
            "id": "msg_NULL", "model": "<synthetic>",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0), "content": [],
        },
    )) + "\n")

    # subagent: separate file + meta.json (individual identity + parent link)
    adir = src / "-Users-t-proj" / "sess-1" / "subagents"
    adir.mkdir(parents=True)
    (adir / "agent-abc123def456789ab.meta.json").write_text(
        json.dumps({"agentType": "Explore", "toolUseId": "toolu_02", "spawnDepth": 1})
    )
    agent_lines = [
        _rec(9, type="user", agentId="abc123def456789ab", isSidechain=True,
             promptId="prompt-1", message={"role": "user", "content": "start"}),
        _rec(
            10,
            requestId="req_AG1",
            agentId="abc123def456789ab",
            isSidechain=True,
            message={
                "id": "msg_AG1",
                "model": "claude-sonnet-5",
                "usage": _usage(cr=0, w1h=8000, out=300),
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "explored"}],
            },
        ),
        _rec(11, type="user", agentId="abc123def456789ab", isSidechain=True,
             promptId="prompt-later", message={"role": "user", "content": "notification"}),
        _rec(12, requestId="req_AG2", agentId="abc123def456789ab", isSidechain=True,
             message={"id": "msg_AG2", "model": "claude-sonnet-5",
                      "usage": _usage(inp=0, out=0, cr=0, w1h=0),
                      "stop_reason": "tool_use", "content": [
                          {"type": "tool_use", "id": "toolu_nested", "name": "Task", "input": {}},
                      ]}),
    ]
    (adir / "agent-abc123def456789ab.jsonl").write_text(
        "\n".join(json.dumps(x) for x in agent_lines) + "\n"
    )
    nested_id = "fedcba9876543210a"
    (adir / f"agent-{nested_id}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "toolUseId": "toolu_nested", "spawnDepth": 2})
    )
    nested_lines = [
        _rec(13, type="user", agentId=nested_id, isSidechain=True, promptId="prompt-later",
             message={"role": "user", "content": "nested"}),
        _rec(14, requestId="req_GRAND", agentId=nested_id, isSidechain=True,
             message={"id": "msg_GRAND", "model": "claude-sonnet-5",
                      "usage": _usage(inp=0, out=0, cr=0, w1h=0),
                      "stop_reason": "end_turn", "content": []}),
    ]
    (adir / f"agent-{nested_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in nested_lines) + "\n"
    )
    unresolved_id = "1111111111111111a"
    (adir / f"agent-{unresolved_id}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "toolUseId": "toolu_missing", "spawnDepth": 1})
    )
    unresolved_lines = [
        _rec(15, type="user", agentId=unresolved_id, isSidechain=True, promptId="prompt-later",
             message={"role": "user", "content": "orphan"}),
        _rec(16, requestId="req_ORPHAN", agentId=unresolved_id, isSidechain=True,
             message={"id": "msg_ORPHAN", "model": "claude-sonnet-5",
                      "usage": _usage(inp=0, out=0, cr=0, w1h=0),
                      "stop_reason": "end_turn", "content": []}),
    ]
    (adir / f"agent-{unresolved_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in unresolved_lines) + "\n"
    )
    wave_id = "2222222222222222a"
    (adir / f"agent-{wave_id}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "toolUseId": "toolu_wave", "spawnDepth": 1})
    )
    wave_lines = [
        _rec(23, type="user", agentId=wave_id, isSidechain=True, promptId="prompt-notify",
             message={"role": "user", "content": "follow-up wave"}),
        _rec(24, requestId="req_WAVE", agentId=wave_id, isSidechain=True,
             message={"id": "msg_WAVE", "model": "claude-sonnet-5",
                      "usage": _usage(inp=0, out=0, cr=0, w1h=0),
                      "stop_reason": "end_turn", "content": []}),
    ]
    (adir / f"agent-{wave_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in wave_lines) + "\n"
    )
    alternate_id = "3333333333333333a"
    (adir / f"agent-{alternate_id}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "toolUseId": "toolu_other", "spawnDepth": 1})
    )
    workflow_dir = adir / "workflows" / "wf_w1"
    workflow_dir.mkdir(parents=True)
    workflow_id = "abcdef0123456789a"
    (workflow_dir / f"agent-{workflow_id}.meta.json").write_text(
        json.dumps({"agentType": "workflow-subagent", "spawnDepth": 1})
    )
    workflow_record = _rec(
        11,
        requestId="req_WF1",
        agentId=workflow_id,
        isSidechain=True,
        message={
            "id": "msg_WF1", "model": "claude-sonnet-5",
            "usage": _usage(inp=0, out=0, cr=0, w1h=0),
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "workflow"}],
        },
    )
    (workflow_dir / f"agent-{workflow_id}.jsonl").write_text(json.dumps(workflow_record) + "\n")


def _table_hash(conn) -> str:
    h = hashlib.sha256()
    for table, order in (
        ("request", "request_id"),
        ("tool_call", "tool_use_id"),
        ("agent", "agent_id"),
        ("prompt", "prompt_id"),
        ("nudge", "rule,fired_ts,session_id"),
    ):
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _add_cross_file_resume(src):
    resume = src / "-Users-t-proj" / "z-resume.jsonl"
    resume.write_text(json.dumps(_rec(
        20,
        requestId="req_A",
        message={
            "id": "msg_A", "model": "claude-fable-5", "usage": _usage(out=50),
            "stop_reason": "end_turn", "content": [],
        },
    )) + "\n")


def test_intake_rules_and_cost(env):
    _build_fixture(env)
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)

    # rule 1: requestId dedupe — 2 records, 1 request row; usage counted once
    reqs = {r["request_id"]: r for r in conn.execute("SELECT * FROM request")}
    assert set(reqs) == {
        "req_A", "req_B", "req_ERR", "req_AG1", "req_AG2", "req_GRAND",
        "req_NOTIFY", "req_MCP_NOTIFY", "req_UNKNOWN_NOTIFY", "req_QUOTED", "req_WAVE",
        "req_CLOSED_NOTIFY", "req_ORPHAN", "req_WF1", "req_NULL",
    }
    assert reqs["req_A"]["output_tok"] == 50  # not 100 (duplicated usage not summed)
    assert reqs["req_A"]["stop_reason"] == "tool_use"  # later record filled it
    assert reqs["req_A"]["end_ts"] == dt.datetime.fromisoformat(
        "2026-07-17T05:00:02+00:00"
    ).timestamp()
    _add_cross_file_resume(env)
    archiver.run(env)
    ingest.run(conn)
    assert conn.execute(
        "SELECT end_ts FROM request WHERE request_id='req_A'"
    ).fetchone()[0] == dt.datetime.fromisoformat("2026-07-17T05:00:02+00:00").timestamp()
    assert reqs["req_NULL"]["prompt_id"] is None
    assert reqs["req_NULL"]["end_ts"] == dt.datetime.fromisoformat(
        "2026-07-17T05:00:08+00:00"
    ).timestamp()

    # rule 6: completed interrupted output is trusted
    assert reqs["req_B"]["is_interrupted"] == 1
    assert reqs["req_B"]["output_tok"] == 4

    # synthetic flagged and excluded from cost views
    assert reqs["req_ERR"]["is_synthetic"] == 1
    n_cost = conn.execute("SELECT COUNT(*) FROM v_request_cost").fetchone()[0]
    assert n_cost == 13

    # rule 3: subagent identity + parent link via meta.json toolUseId
    ag = conn.execute("SELECT * FROM agent WHERE agent_id='abc123def456789ab'").fetchone()
    assert ag["agent_type"] == "Explore"
    assert ag["parent_tool_use_id"] == "toolu_02"
    assert ag["resolved_model"] == "claude-sonnet-5"
    assert reqs["req_AG1"]["lineage_id"] == "sess-1/abc123def456789ab"
    assert reqs["req_AG1"]["prompt_id"] == "prompt-1"
    assert reqs["req_AG2"]["prompt_id"] == "prompt-1"
    assert reqs["req_GRAND"]["prompt_id"] == "prompt-1"
    assert reqs["req_ORPHAN"]["prompt_id"] == "prompt-later"
    assert reqs["req_NOTIFY"]["prompt_id"] == "prompt-1"
    assert reqs["req_MCP_NOTIFY"]["prompt_id"] == "prompt-1"
    assert reqs["req_WAVE"]["prompt_id"] == "prompt-1"
    assert reqs["req_UNKNOWN_NOTIFY"]["prompt_id"] == "prompt-unknown"
    assert reqs["req_QUOTED"]["prompt_id"] == "prompt-quoted"
    assert reqs["req_CLOSED_NOTIFY"]["prompt_id"] == "prompt-closed-notify"
    assert conn.execute(
        "SELECT prompt_id FROM tool_call WHERE tool_use_id='toolu_null'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT prompt_id FROM tool_call WHERE tool_use_id='toolu_nested'"
    ).fetchone()[0] == "prompt-1"
    workflow = conn.execute(
        "SELECT * FROM agent WHERE agent_id='abcdef0123456789a'"
    ).fetchone()
    assert workflow["parent_tool_use_id"] == "toolu_wf"
    assert workflow["workflow_run_id"] == "wf_w1"
    assert reqs["req_WF1"]["prompt_id"] == "prompt-1"

    # tool linkage: name from assistant block, result size from user record
    tc = conn.execute("SELECT * FROM tool_call WHERE tool_use_id='toolu_01'").fetchone()
    assert tc["name"] == "Read" and tc["request_id"] == "req_A" and tc["result_bytes"] > 0
    assert tc["result_ts"] == dt.datetime.fromisoformat("2026-07-17T05:00:03+00:00").timestamp()
    early = conn.execute("SELECT * FROM tool_call WHERE tool_use_id='toolu_early'").fetchone()
    assert all(early[key] is not None for key in ("name", "ts", "request_id", "result_ts"))
    assert early["is_error"] == 0 and early["result_bytes"] == len(json.dumps("first"))
    workflow_tool = conn.execute(
        "SELECT * FROM tool_call WHERE tool_use_id='toolu_wf'"
    ).fetchone()
    assert workflow_tool["workflow_run_id"] == "wf_w1"
    assert workflow["workflow_run_id"] == workflow_tool["workflow_run_id"]
    conn.execute(
        "UPDATE agent SET parent_tool_use_id='wrong' WHERE agent_id='abcdef0123456789a'"
    )
    ingest._derive_workflow_links(conn)
    assert conn.execute(
        "SELECT parent_tool_use_id FROM agent WHERE agent_id='abcdef0123456789a'"
    ).fetchone()[0] == "toolu_wf"

    # prompt captured, interruption noted
    p = conn.execute("SELECT * FROM prompt WHERE prompt_id='prompt-1'").fetchone()
    assert p["text"] == "リポジトリを説明して"
    assert p["interrupted_message_id"] == "msg_B"
    assert conn.execute(
        "SELECT COUNT(*) FROM request WHERE prompt_id='prompt-notify'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT text FROM prompt WHERE prompt_id='prompt-notify'"
    ).fetchone()[0].startswith("<task-notification>")

    # cost math (hand-computed):
    # req_A (fable in=10/out=50 $/M): in 2*10 + cr 1000*10*0.1 + w1h 500*10*2.0 + out 50*50
    expect_a = (2 * 10 + 1000 * 10 * 0.1 + 500 * 10 * 2.0 + 50 * 50) / 1e6
    got_a = conn.execute(
        "SELECT cost_usd FROM v_request_cost WHERE request_id='req_A'"
    ).fetchone()[0]
    assert got_a == pytest.approx(expect_a)
    # req_B interrupted after tool_use: completed output remains billable
    expect_b = (2 * 10 + 1500 * 10 * 0.1 + 200 * 10 * 2.0 + 4 * 50) / 1e6
    got_b = conn.execute(
        "SELECT cost_usd FROM v_request_cost WHERE request_id='req_B'"
    ).fetchone()[0]
    assert got_b == pytest.approx(expect_b)
    # subagent on Sonnet 5 introductory pricing (2/10 through 2026-08-31)
    expect_ag = (2 * 2 + 8000 * 2 * 2.0 + 300 * 10) / 1e6
    got_ag = conn.execute(
        "SELECT cost_usd FROM v_request_cost WHERE request_id='req_AG1'"
    ).fetchone()[0]
    assert got_ag == pytest.approx(expect_ag)

    # prompt rollup includes the subagent (same prompt_id)
    v = conn.execute("SELECT * FROM v_prompt_cost WHERE prompt_id='prompt-1'").fetchone()
    assert v["n_requests"] == 9 and v["n_agents"] == 4
    assert v["cost_usd"] == pytest.approx(expect_a + expect_b + expect_ag)

    # regime events: cc_version + no unknown models (all in price table)
    kinds = {r["kind"]: r["detail"] for r in conn.execute("SELECT * FROM regime_event")}
    assert kinds.get("cc_version") == "2.0.14"
    assert conn.execute(
        "SELECT value FROM meta WHERE key='redaction_version'"
    ).fetchone()[0] == "2"
    conn.close()


def test_prompt_redaction_precedes_20000_character_truncation(env):
    secret = "sk-proj-" + "q" * 32
    text = "a" * 19_989 + secret
    path = env / "project" / "boundary.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps(_rec(0, type="user", message={"content": text})) + "\n")
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)
    prompt = conn.execute("SELECT text FROM prompt WHERE prompt_id='prompt-1'").fetchone()[0]
    assert secret not in prompt and "[REDACTED" in prompt
    conn.close()


def test_incremental_ingest_and_rebuild_determinism(env):
    _build_fixture(env)
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)
    _add_cross_file_resume(env)
    archiver.run(env)
    ingest.run(conn)
    h1 = _table_hash(conn)

    # incremental: second ingest with no new segments is a no-op
    s2 = ingest.run(conn)
    assert s2.records == 0
    assert _table_hash(conn) == h1
    conn.close()

    # rebuild from archive reproduces the exact same facts
    ingest.rebuild()
    conn2 = ledger.connect_readonly()
    assert _table_hash(conn2) == h1
    conn2.close()


def test_notification_spawn_wave_requires_fixed_point(env):
    _build_fixture(env)
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)

    conn.execute("UPDATE request SET prompt_id='prompt-notify' WHERE request_id='req_NOTIFY'")
    conn.execute("UPDATE tool_call SET prompt_id='prompt-notify' WHERE tool_use_id='toolu_wave'")
    conn.execute("UPDATE request SET prompt_id='prompt-notify' WHERE request_id='req_WAVE'")

    ingest._derive_prompt_attribution(conn, max_iterations=1)
    assert conn.execute(
        "SELECT prompt_id FROM request WHERE request_id='req_NOTIFY'"
    ).fetchone()[0] == "prompt-1"
    assert conn.execute(
        "SELECT prompt_id FROM tool_call WHERE tool_use_id='toolu_wave'"
    ).fetchone()[0] == "prompt-1"
    assert conn.execute(
        "SELECT prompt_id FROM request WHERE request_id='req_WAVE'"
    ).fetchone()[0] == "prompt-notify"

    ingest._derive_prompt_attribution(conn)
    assert conn.execute(
        "SELECT prompt_id FROM request WHERE request_id='req_WAVE'"
    ).fetchone()[0] == "prompt-1"
    conn.close()


def test_interrupted_output_depends_on_merged_stop_reason(env):
    path = env / "project" / "interruptions.jsonl"
    path.parent.mkdir()
    records = [
        _rec(30, type="user", message={"role": "user", "content": "interruptions"}),
        # Marker after duplicated content blocks: completed output must survive the marker UPDATE.
        _rec(31, requestId="req_AFTER", message={
            "id": "msg_AFTER", "model": "claude-fable-5", "usage": _usage(out=4),
            "stop_reason": None, "content": [{"type": "text", "text": "partial"}],
        }),
        _rec(32, requestId="req_AFTER", message={
            "id": "msg_AFTER", "model": "claude-fable-5", "usage": _usage(out=200),
            "stop_reason": "tool_use", "content": [],
        }),
        _rec(33, type="user", interruptedMessageId="msg_AFTER",
             message={"role": "user", "content": "interrupted after completion"}),
        # Marker before the completed duplicate: conflict merge must restore trusted output.
        _rec(34, requestId="req_BEFORE", message={
            "id": "msg_BEFORE", "model": "claude-fable-5", "usage": _usage(out=4),
            "stop_reason": None, "content": [{"type": "text", "text": "partial"}],
        }),
        _rec(35, type="user", interruptedMessageId="msg_BEFORE",
             message={"role": "user", "content": "interrupted before completion"}),
        _rec(36, requestId="req_BEFORE", message={
            "id": "msg_BEFORE", "model": "claude-fable-5", "usage": _usage(out=200),
            "stop_reason": "tool_use", "content": [],
        }),
        # Genuine mid-stream interruption retains the established NULL behavior.
        _rec(37, requestId="req_MID", message={
            "id": "msg_MID", "model": "claude-fable-5", "usage": _usage(out=4),
            "stop_reason": None, "content": [{"type": "text", "text": "partial"}],
        }),
        _rec(38, type="user", interruptedMessageId="msg_MID",
             message={"role": "user", "content": "mid-stream interruption"}),
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)
    rows = {
        row["request_id"]: row
        for row in conn.execute(
            "SELECT request_id,output_tok,stop_reason,is_interrupted,parser_version "
            "FROM request ORDER BY request_id"
        )
    }
    assert rows["req_AFTER"]["output_tok"] == 200
    assert rows["req_BEFORE"]["output_tok"] == 200
    assert rows["req_MID"]["output_tok"] is None
    assert all(row["is_interrupted"] == 1 for row in rows.values())
    assert all(row["parser_version"] == 10 for row in rows.values())
    conn.close()


def test_unparsable_line_quarantined_not_dropped(env):
    sess = env / "-Users-t-proj" / "sess-2.jsonl"
    sess.parent.mkdir(parents=True)
    secret = "sk-proj-" + "s" * 32
    sess.write_text(json.dumps({
        "type": "assistant", "requestId": "x",
        "message": {"usage": "NOT A DICT", "content": secret},
    }) + "\n")
    archiver.run(env)
    conn = ledger.connect()
    stats = ingest.run(conn)
    assert stats.quarantined == 1
    q = conn.execute("SELECT * FROM quarantine").fetchone()
    assert "NOT A DICT" in q["raw"]
    assert secret not in q["raw"] and "[REDACTED" in q["raw"]
    conn.close()


def test_unknown_transcript_type_is_quarantined_and_visible(env):
    sess = env / "project" / "unknown.jsonl"
    sess.parent.mkdir(parents=True)
    sess.write_text(
        json.dumps(
            {
                "type": "future-record-shape",
                "sessionId": "unknown-session",
                "timestamp": "2026-07-20T00:00:00Z",
                "payload": {"new": True},
            }
        )
        + "\n"
    )
    archiver.run(env)
    conn = ledger.connect()
    stats = ingest.run(conn)
    assert stats.quarantined == 1
    row = conn.execute("SELECT reason,raw FROM quarantine").fetchone()
    assert "unknown transcript record type" in row["reason"]
    assert "future-record-shape" in row["raw"]
    conn.close()


def test_known_nonbilling_transcript_metadata_is_ignored(env):
    sess = env / "project" / "metadata.jsonl"
    sess.parent.mkdir(parents=True)
    record_types = (
        "ai-title",
        "attachment",
        "file-history-delta",
        "last-prompt",
        "mode",
        "permission-mode",
    )
    sess.write_text(
        "\n".join(
            json.dumps({"type": record_type, "sessionId": "metadata-session"})
            for record_type in record_types
        )
        + "\n"
    )
    archiver.run(env)
    conn = ledger.connect()
    stats = ingest.run(conn)
    assert stats.quarantined == 0
    assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
    conn.close()


def test_newly_known_metadata_removes_only_matching_legacy_quarantine(env):
    conn = ledger.connect()
    conn.execute(
        "INSERT INTO quarantine VALUES (1,'old',?,?)",
        (
            "ValueError(\"unknown transcript record type: 'permission-mode'\")",
            '{"type":"permission-mode","truncated":',
        ),
    )
    conn.execute(
        "INSERT INTO quarantine VALUES (2,'future',?,?)",
        (
            "ValueError(\"unknown transcript record type: 'future-record-shape'\")",
            json.dumps({"type": "future-record-shape"}),
        ),
    )
    ingest.run(conn)
    rows = conn.execute("SELECT src FROM quarantine ORDER BY ts").fetchall()
    assert [row[0] for row in rows] == ["future"]
    conn.close()


def test_hook_spool_archive_ingest_and_rebuild(env):
    home = env.parent / "metsuke-home"
    spool = home / "spool/hooks"
    spool.mkdir(parents=True)
    envelope = {"metsuke_event": "PreCompact", "metsuke_ts": 123.5, "payload": {"session_id": "sess-hook", "prompt_id": "p-hook"}}
    (spool / "1.ndjson").write_text(json.dumps(envelope) + "\n")
    conn = ledger.connect()
    ingest.run(conn)
    assert not list(spool.glob("*.ndjson"))
    row = conn.execute("SELECT * FROM hook_event").fetchone()
    assert row["kind"] == "PreCompact" and row["session_id"] == "sess-hook"
    conn.close()
    ingest.rebuild()
    conn = ledger.connect_readonly()
    assert conn.execute("SELECT COUNT(*) FROM hook_event").fetchone()[0] == 1
    conn.close()


def test_hook_spool_is_batched_and_manifest_cursor_is_incremental(env):
    home = env.parent / "metsuke-home"
    spool = home / "spool/hooks"
    spool.mkdir(parents=True)
    for index in range(5):
        envelope = {
            "metsuke_event": "Notification",
            "metsuke_ts": 200 + index,
            "payload": {"session_id": "batch", "index": index},
        }
        (spool / f"{index}.ndjson").write_text(json.dumps(envelope) + "\n")
    conn = ledger.connect()
    stats = ingest.run(conn)
    hook_entries = [
        row for row in archiver.manifest_entries() if row["kind"] == "hooks"
    ]
    assert len(hook_entries) == 1
    assert stats.records == 5
    assert conn.execute("SELECT COUNT(*) FROM hook_event").fetchone()[0] == 5
    byte_offset = int(
        conn.execute(
            "SELECT value FROM meta WHERE key='manifest_byte_offset'"
        ).fetchone()[0]
    )
    assert byte_offset == config.manifest_path().stat().st_size
    again = ingest.run(conn)
    assert again.segments == 0 and again.records == 0
    conn.close()
    ingest.rebuild()
    conn = ledger.connect_readonly()
    assert conn.execute("SELECT COUNT(*) FROM hook_event").fetchone()[0] == 5
    conn.close()


def test_missing_tool_result_timestamp_and_multi_result_run_id_are_not_mixed(env):
    path = env / "project" / "tools.jsonl"
    path.parent.mkdir()
    assistant = _rec(1, requestId="req-tools", message={
        "id": "msg-tools", "model": "claude-sonnet-5", "usage": _usage(),
        "content": [
            {"type": "tool_use", "id": "missing-ts", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "multi-a", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "multi-b", "name": "Read", "input": {}},
        ],
    })
    missing = _rec(2, type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "missing-ts", "content": "ignored"}
    ]})
    missing.pop("timestamp")
    multiple = _rec(
        3, type="user", toolUseResult={"runId": "wf_wrong"}, message={"content": [
            {"type": "tool_result", "tool_use_id": "multi-a", "content": "a"},
            {"type": "tool_result", "tool_use_id": "multi-b", "content": "b"},
        ]},
    )
    path.write_text("\n".join(json.dumps(row) for row in (assistant, missing, multiple)) + "\n")
    archiver.run(env)
    conn = ledger.connect()
    ingest.run(conn)
    row = conn.execute("SELECT * FROM tool_call WHERE tool_use_id='missing-ts'").fetchone()
    assert row["result_ts"] is None and row["result_bytes"] is None
    workflows = conn.execute(
        "SELECT workflow_run_id FROM tool_call WHERE tool_use_id IN ('multi-a','multi-b')"
    ).fetchall()
    assert all(item[0] is None for item in workflows)
    conn.close()
