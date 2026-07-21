import json
import os
import re
import sqlite3
import base64
import subprocess
import time
from importlib import resources

import pytest

from metsuke import archiver, cli, ingest, ledger, report, trace_html
from metsuke.redaction import REDACTION_VERSION
from metsuke.viewgen import render


def test_open_browser_uses_a_new_cmux_workspace(tmp_path, monkeypatch):
    html = tmp_path / "trace.html"
    html.touch()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:3] == ["workspace", "create"]:
            return subprocess.CompletedProcess(command, 0, "OK workspace:52\n", "")
        return subprocess.CompletedProcess(command, 0, "OK\n", "")

    monkeypatch.setenv("CMUX_WORKSPACE_ID", "current-workspace")
    monkeypatch.setattr(trace_html.shutil, "which", lambda command: "/bin/cmux")
    monkeypatch.setattr(trace_html.subprocess, "run", run)

    assert trace_html.open_browser(html, "#prompt=p1") is True
    assert calls[0][0] == [
        "/bin/cmux",
        "workspace",
        "create",
        "--name",
        "metsuke viewer",
        "--cwd",
        str(tmp_path),
        "--focus",
        "true",
    ]
    assert calls[1][0] == [
        "/bin/cmux",
        "new-pane",
        "--type",
        "browser",
        "--workspace",
        "workspace:52",
        "--url",
        html.as_uri() + "#prompt=p1",
        "--focus",
        "true",
    ]


def test_open_browser_closes_cmux_workspace_when_open_fails(tmp_path, monkeypatch):
    html = tmp_path / "trace.html"
    html.touch()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["workspace", "create"]:
            return subprocess.CompletedProcess(command, 0, "OK workspace:52\n", "")
        if command[1] == "new-pane":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "OK\n", "")

    monkeypatch.setenv("CMUX_WORKSPACE_ID", "current-workspace")
    monkeypatch.setattr(trace_html.shutil, "which", lambda command: "/bin/cmux")
    monkeypatch.setattr(trace_html.subprocess, "run", run)

    assert trace_html.open_browser(html) is False
    assert calls[-1] == ["/bin/cmux", "workspace", "close", "workspace:52"]


def test_open_browser_uses_macos_open_outside_cmux(tmp_path, monkeypatch):
    html = tmp_path / "trace.html"
    html.touch()
    calls = []

    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(
        trace_html.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert trace_html.open_browser(html) is True
    assert calls == [(["open", html.as_uri()], {"check": True})]


@pytest.mark.parametrize(
    "argv",
    [
        ["today"],
        ["today", "--json"],
        ["week"],
        ["explain"],
        ["explain", "--json"],
        ["trace", "last"],
        ["trace", "last", "--json"],
    ],
)
def test_report_commands_fail_cleanly_without_ledger(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "missing-home"))

    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ledger unavailable" in captured.err
    assert "metsuke sync を先に実行してください" in captured.err
    assert "Traceback" not in captured.err


def test_report_command_catches_unreadable_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    ledger.db_path().write_text("not a sqlite database")

    assert cli.main(["today", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ledger unavailable")
    assert "metsuke sync を先に実行してください" in captured.err
    assert "file is not a database" in captured.err


def test_report_command_does_not_hide_programming_error(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    ledger.db_path().touch()

    def raise_programming_error():
        raise sqlite3.ProgrammingError("broken report query")

    monkeypatch.setattr(ledger, "connect_readonly", raise_programming_error)

    with pytest.raises(sqlite3.ProgrammingError, match="broken report query"):
        report.today()


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    conn = ledger.connect()
    conn.execute("DELETE FROM price WHERE model='claude-sonnet-5'")
    conn.execute(
        """INSERT INTO price
           (model,valid_from,in_usd,out_usd,cache_read_x,cache_w5m_x,cache_w1h_x,
            batch_x,fast_x,geo_us_x)
           VALUES ('claude-sonnet-5','1970-01-01',3,15,0.1,1.25,2,0.5,2,1.1)"""
    )
    conn.execute("INSERT INTO prompt(prompt_id,session_id,ts,text) VALUES ('p1','s1',10,'work')")
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source,raw_path,end_ts,
            api_duration_ms)
           VALUES ('r1','s1','s1','p1',11,'claude-sonnet-5',10,20,30,40,50,0,
                   'transcript','project/s1.jsonl',13,1000)"""
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source,raw_path,end_ts)
           VALUES ('r2','s1','s1','p1',14,'claude-sonnet-5',1,2,3,4,5,0,
                   'transcript','project/s1.jsonl',15)"""
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source,raw_path,end_ts)
           VALUES ('ru','s1','s1',NULL,20,'claude-sonnet-5',2,3,4,5,6,0,
                   'transcript','project/s1.jsonl',21)"""
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source,raw_path,end_ts,
            api_duration_ms)
           VALUES ('rotel','s1','s1','p1',12,'claude-sonnet-5',2,3,4,5,6,0,
                   'otel','project/s1.jsonl',12,100000)"""
    )
    conn.execute(
        "INSERT INTO agent(agent_id,session_id,agent_type) VALUES ('agent-x','s1','<b>\"x\"</b>')"
    )
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,agent_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source,raw_path,end_ts)
           VALUES ('ragent','s1','s1/agent-x','agent-x','p1',16,'claude-sonnet-5',1,1,
                   1,1,1,0,'transcript','project/s1.jsonl',17)"""
    )
    conn.execute(
        """INSERT INTO tool_call
           (tool_use_id,request_id,session_id,prompt_id,name,ts,result_ts,result_bytes,is_error)
           VALUES ('t1','r1','s1','p1','Read',12,16,20,1)"""
    )
    conn.executemany(
        """INSERT INTO tool_call
           (tool_use_id,request_id,session_id,prompt_id,agent_id,name,ts,result_ts,is_error)
           VALUES (?,?,?,?,?,?,?,?,0)""",
        [
            ("t2", "r1", "s1", "p1", None, "Bash", 12, 15),
            ("t3", "ragent", "s1", "p1", "agent-x", "Edit", 16, 16.5),
        ],
    )
    conn.execute(
        "INSERT INTO hook_event(session_id,ts,kind,payload_json) "
        "VALUES ('s1',12,'Notification','{}')"
    )
    conn.commit()
    conn.close()
    path = source / "project" / "s1.jsonl"
    path.parent.mkdir()
    secret = "sk-proj-" + "x" * 32
    breakout = "</script><script>alert(1)</script></ScRiPt foo><!--<script>"
    tool_breakout = "</script><script>tool(1)</script><!--<script>" + secret
    records = [
        {"type": "assistant", "requestId": "r1", "message": {"content": [
            {"type": "text", "text": breakout + secret},
            {"type": "thinking", "thinking": "must not be embedded"},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/tmp/demo"}},
        ]}},
        {"type": "assistant", "requestId": "r1", "message": {"content": [
            {"type": "text", "text": "second record text"}
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": tool_breakout}
        ]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return home


def test_span_geometry_and_cost_golden(trace_env):
    conn = ledger.connect_readonly()
    data = trace_html.build_trace_data(conn, "s1")
    conn.close()
    assert data is not None
    geometry = data["prompt_svgs"]["p1"]["geometry"]
    assert len(geometry["lanes"]) == 2
    spans = {span["request_id"]: span for span in geometry["spans"]}
    assert spans["r1"]["measured"] is True
    assert spans["rotel"]["x"] == geometry["plot_x"]
    assert all(span["x"] >= geometry["plot_x"] for span in geometry["spans"])
    assert data["requests"][0]["cost_parts"] == pytest.approx(
        {"input": 0.00003, "cache_read": 0.000009, "cache_w5m": 0.00015,
         "cache_w1h": 0.0003, "output": 0.0003}
    )
    assert geometry["ticks"][0] == {"x": 180.0, "label": "0:00"}
    assert geometry["hook_marks"] == [{"x": 1227.619, "glyph": "🔔"}]
    assert geometry["lanes"] == [
        {"label": "main", "sub": "3req · $0.001", "base_h": 42,
         "expanded_h": 70, "rows": 2},
        {"label": '↳ <b>"x"</b>', "sub": "1req · $0.000", "base_h": 42,
         "expanded_h": 56, "rows": 1},
    ]
    assert all("request_id" not in lane for lane in geometry["lanes"])
    assert geometry["context_label"] == {"text": "context main · peak 130"}
    assert all(4 <= point[1] <= 44 for point in geometry["context_points"])
    assert spans["r1"]["color"] == "#38bdf8"
    assert spans["r1"]["local_y"] == 4
    assert spans["r1"]["cost_pcts"] == pytest.approx(
        [1.140684, 19.011407, 38.022814, 3.802281, 38.022814]
    )
    assert sum(spans["r1"]["cost_pcts"]) == pytest.approx(100)
    sparks = [span["spark"] for span in spans.values() if span["spark"]]
    assert {spark["cause"] for spark in sparks} == {"unknown"}
    assert all({"cause", "ts", "request_id", "cache_write_usd"} <= spark.keys()
               for spark in sparks)
    assert geometry["tools"][0]["color"] == "#60a5fa"
    assert geometry["tools"][0]["error"] is True
    assert geometry["tools"][0]["name"] == "Read"
    tools = {tool["tool_use_id"]: tool for tool in geometry["tools"]}
    assert (tools["t1"]["row"], tools["t2"]["row"], tools["t3"]["row"]) == (0, 1, 0)
    assert tools["t1"]["bar_w"] == pytest.approx(41.905, abs=.001)
    assert tools["t3"]["bar_w"] == 11
    assert tools["t2"]["local_y_expanded"] == 36
    assert geometry["clusters"] == [
        {"lane": 0, "x": 1227.619, "count": 2, "color": "#60a5fa", "tool_use_id": "t1"},
        {"lane": 1, "x": 1269.524, "count": 1, "color": "#fb923c", "tool_use_id": "t3"},
    ]
    unattributed = data["prompt_svgs"][data["unattributed_key"]]["geometry"]
    assert [span["request_id"] for span in unattributed["spans"]] == ["ru"]
    session = data["prompt_svgs"]["__session__"]["geometry"]
    assert len(session["spans"]) == len(data["requests"]) == 5
    assert [lane["label"] for lane in session["lanes"]] == ["main", '↳ <b>"x"</b>']
    assert (session["t0"], session["t1"]) == (-88, 21)
    assert session["prompt_strip"] == [{
        "prompt_id": "p1", "x": pytest.approx(1168.991),
        "width": pytest.approx(111.009), "cost_usd": pytest.approx(
            sum(request["cost_usd"] or 0 for request in data["requests"]
                if request["prompt_id"] == "p1")
        ),
        "n_req": 4, "label": data["prompts"][0]["text"][:60],
    }]
    story = data["prompt_svgs"]["__story__"]["geometry"]["story"]
    assert [segment["prompt_id"] for segment in story["segments"]] == ["p1"]
    assert story["segments"][0]["width"] == pytest.approx(1100)
    assert story["segments"][0]["label"] == data["prompts"][0]["text"][:60]
    assert story["gaps"] == []
    assert len(data["requests"]) == 5
    assert data["total_usd"] == pytest.approx(
        sum(request["cost_usd"] or 0 for request in data["requests"])
    )
    assert data["thresholds"] == {
        "prompt_warn_usd": 3.0,
        "prompt_crit_usd": 7.5,
        "context_warn_tokens": 200_000,
        "context_crit_tokens": 500_000,
    }
    assert geometry["context_peak"] == 130
    assert data["req_text"]["r1"].endswith("second record text")
    assert data["req_thinking"]["r1"] is True
    assert data["tool_io"]["t1"]["input"] == '{\n  "file_path": "/tmp/demo"\n}'
    assert data["tool_io"]["t1"]["result"].startswith("</script>")
    assert "bodies" not in data and "must not be embedded" not in json.dumps(data)
    svg = base64.b64decode(data["prompt_svgs"]["p1"]["data_url"].split(",", 1)[1]).decode()
    assert "<text" not in svg
    assert 'preserveAspectRatio="none"' in svg
    assert 'viewBox="180 0 1100 48"' in svg
    assert 'vector-effect="non-scaling-stroke"' in svg
    assert "<polyline" in svg and "<rect" not in svg and "<line" not in svg


def test_trace_focus_prefix_labels_and_html_anchor(trace_env, capsys):
    assert cli.main(["trace", "s1", "--focus", "ragen"]) == 0
    terminal = capsys.readouterr()
    assert "▶ agent" in terminal.out
    assert re.search(r"⚡unknown \$0\.00 \d{2}:\d{2}", terminal.out)

    assert cli.main(["trace", "s1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    breaks = payload["prompts"][0]["main"]["identity_breaks"]
    assert {"cause", "ts", "request_id", "cache_write_usd"} <= breaks[0].keys()

    assert cli.main(["trace", "s1", "--focus", "ragen", "--html"]) == 0
    output = capsys.readouterr().out.strip()
    assert output.endswith("#request=ragent")
    html = (trace_env / "traces" / "s1.html").read_text()
    assert '"focus_request_id":"ragent"' in html
    assert 'band.id="request-"+span.request_id' in html
    assert "breakLabel(span.spark)" in html
    assert "minimumFractionDigits:2,maximumFractionDigits:2" in html


@pytest.mark.parametrize("amount", [8.759, 9.27, 1234.5])
def test_trace_identity_money_matches_v3_money(amount):
    label = report._identity_label(
        {"cause": "ttl_expiry", "ts": 0, "cache_write_usd": amount}
    )
    assert label.split()[1] == render.money(amount)


def test_trace_focus_ambiguous_and_missing_are_safe(trace_env, capsys):
    conn = ledger.connect()
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES ('ragent-two','s1','s1','p1',30,'claude-sonnet-5',1,1,1,1,1,0,
                   'transcript')"""
    )
    conn.executemany(
        """INSERT INTO request
           (request_id,session_id,lineage_id,prompt_id,ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,source)
           VALUES (?,'s1','s1','p1',?,'claude-sonnet-5',1,1,1,1,1,0,'transcript')""",
        [(f"ragent-{index:02d}", 31 + index) for index in range(11)],
    )
    conn.commit()
    conn.close()
    assert cli.main(["trace", "s1", "--focus", "ragen"]) == 1
    error = capsys.readouterr().err
    assert "ambiguous" in error and "先頭16文字" in error
    assert "…他 3 件" in error
    assert error.count("ragent-") == 9  # ragent 自体を含め、候補表示は合計10件まで
    assert "ragent-two" not in error
    assert cli.main(["trace", "s1", "--focus", "missing"]) == 0
    captured = capsys.readouterr()
    assert "warning: focus request not found" in captured.err
    assert "session s1" in captured.out


def test_unmeasured_span_start_respects_previous_stop_reason():
    requests = [
        {"request_id": "end", "ts": 10, "end_ts": 11, "stop_reason": "end_turn", "input_tok": 1},
        {"request_id": "after-end", "ts": 20, "end_ts": 22, "stop_reason": "end_turn", "input_tok": 1},
        {"request_id": "tool", "ts": 30, "end_ts": 31, "stop_reason": "tool_use", "input_tok": 1},
        {"request_id": "after-tool", "ts": 40, "end_ts": 42, "input_tok": 1},
        {"request_id": "lane-first", "agent_id": "agent", "ts": 50, "end_ts": 52,
         "input_tok": 1},
    ]
    geometry = trace_html._span_geometry(requests, [], {}, {}, [])
    spans = {span["request_id"]: span for span in geometry["spans"]}

    def x(ts):
        return round(
            geometry["plot_x"] + (ts - geometry["t0"]) / geometry["duration"]
            * geometry["plot_width"],
            3,
        )

    assert spans["after-end"]["x"] == x(20)
    assert spans["after-tool"]["x"] == x(31)
    assert spans["lane-first"]["x"] == x(50)


def test_session_lane_order_is_main_then_agent_cost_descending():
    requests = [
        {"request_id": "main", "ts": 1, "end_ts": 2, "input_tok": 1,
         "cost_usd": 1},
        {"request_id": "low", "agent_id": "low", "ts": 2, "end_ts": 3,
         "input_tok": 1, "cost_usd": 2},
        {"request_id": "high", "agent_id": "high", "ts": 3, "end_ts": 4,
         "input_tok": 1, "cost_usd": 9},
    ]
    agents = {"low": {"agent_type": "low"}, "high": {"agent_type": "high"}}
    geometry = trace_html._span_geometry(
        requests, [], agents, {}, [], agent_cost_order=True
    )
    assert [lane["label"] for lane in geometry["lanes"]] == ["main", "↳ high", "↳ low"]


def test_prompt_strip_is_active_prompts_in_time_order():
    prompts = [
        {"prompt_id": "later", "ts": 20, "text": "later prompt"},
        {"prompt_id": "empty", "ts": 15, "text": "no requests"},
        {"prompt_id": "first", "ts": 10, "text": "redacted first prompt"},
    ]
    requests = [
        {"prompt_id": "later", "ts": 21, "cost_usd": 2},
        {"prompt_id": "first", "ts": 11, "cost_usd": 1},
    ]
    geometry = {"t0": 0, "t1": 30, "duration": 30}
    strip = trace_html._prompt_strip(prompts, requests, geometry)
    assert [item["prompt_id"] for item in strip] == ["first", "later"]
    assert [item["x"] for item in strip] == sorted(item["x"] for item in strip)
    assert [item["label"] for item in strip] == ["redacted first prompt", "later prompt"]
    assert [item["n_req"] for item in strip] == [1, 1]


def test_story_layout_widths_gaps_and_overlap():
    prompts = [
        {"prompt_id": "c", "ts": 30, "text": "third"},
        {"prompt_id": "a", "ts": 10, "text": "first"},
        {"prompt_id": "empty", "ts": 15, "text": "empty"},
        {"prompt_id": "b", "ts": 20, "text": "second"},
    ]
    requests = [
        {"prompt_id": "a", "cost_usd": 1},
        {"prompt_id": "b", "cost_usd": 2},
        {"prompt_id": "c", "cost_usd": 3},
    ]
    prompt_svgs = {
        "a": {"geometry": {"duration": 10, "t0": 0, "t1": 10}},
        "b": {"geometry": {"duration": 20, "t0": 70, "t1": 90}},
        "c": {"geometry": {"duration": 1, "t0": 85, "t1": 86}},
    }
    story = trace_html._story_layout(prompts, requests, prompt_svgs)
    segments = story["segments"]
    assert [segment["prompt_id"] for segment in segments] == ["a", "b", "c"]
    assert segments[0]["time_label"] == time.strftime("%H:%M", time.localtime(10))
    assert segments[0]["width"] / segments[1]["width"] == pytest.approx(0.5, abs=1e-5)
    assert segments[2]["width"] == trace_html.STORY_MIN_WIDTH
    assert [gap["seconds"] for gap in story["gaps"]] == [60, 0]
    assert [segment["x_offset"] for segment in segments] == sorted(
        segment["x_offset"] for segment in segments
    )
    assert story["px_per_sec"] == pytest.approx(1100 / 31)


def test_html_security_csp_template_contract_and_modes(trace_env, monkeypatch, capsys):
    old = trace_env / "traces" / "old.html"
    old.parent.mkdir(mode=0o700, exist_ok=True)
    old.write_text('"redaction_version":1')
    future = trace_env / "traces" / "future.html"
    future.write_text('footer redaction_version=999')
    path = trace_html.generate("s1")
    assert path is not None and not old.exists() and future.exists()
    html = path.read_text()
    assert "</script><script>alert(1)</script>" not in html
    assert "</ScRiPt foo>" not in html
    assert "<!--" not in html
    assert "</script><script>tool(1)</script>" not in html
    assert "sk-proj-" not in html and "[REDACTED:openai_key:" in html
    assert f'content="{trace_html.CSP}"' in html
    assert "台帳は旧リダクション版 — metsuke rebuild を推奨" in html
    template = resources.files("metsuke").joinpath("trace_template.html").read_text()
    assert template.count(trace_html.DATA_MARKER) == 1
    assert "KeyB" in template and "no-nav" in template and "no-aside" in template
    assert "preventDefault" in template
    assert '<div id="labels">' in template and '<div id="plot">' in template
    assert '<div id="ruler">' in template and '<div id="lanes">' in template
    assert '<button id="expand">レーン展開</button>' in template
    assert 'id="summary"' in template and 'function renderOverview' in template
    assert '"main context peak"' in template
    assert 'id="helpPanel" hidden' in template and 'aria-controls="helpPanel"' in template
    assert "context_warn_tokens" in template and "context_crit_tokens" in template
    assert '"contextrow "+levelClass' in template
    assert "grid-template-rows:minmax(0,1fr) minmax(150px,230px)" in template
    assert 'add(group,"button",null,"band"' in template
    assert 'data.prompt_svgs.__session__' in template
    assert 'data.prompt_svgs.__story__' in template
    assert '"promptseg"' in template and 'g.prompt_strip||[]' in template
    assert '"ストーリー"' in template and '"実時間"' in template
    assert 'data.prompt_svgs.__story__?"__story__"' in template
    assert 'function rebuildStory()' in template and 'storychapter' in template
    assert 'aria-label="プロンプト一覧"' in template
    assert 'aria-label="選択項目の詳細"' in template
    assert 'aria-live="polite"' in template
    assert "function keyboardClick" in template and "function decorateTable" in template
    assert "focus-visible" in template and "prefers-reduced-motion" in template
    assert "fixedHotStyle" not in template and 'add(document.head,"style")' not in template
    assert len(list(resources.files("metsuke").iterdir())) > 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    records = list((trace_env / "spool" / "hooks").glob("*.ndjson"))
    envelope = json.loads(records[0].read_text())
    assert envelope["metsuke_event"] == "trace_html_generated" and "metsuke_ts" in envelope
    assert trace_html.generate("s1", record=False) == path
    assert len(list((trace_env / "spool" / "hooks").glob("*.ndjson"))) == len(records)
    source = trace_env.parent / "source"
    archiver.run(source)
    conn = ledger.connect()
    ingest.run(conn)
    generated = conn.execute(
        "SELECT * FROM hook_event WHERE kind='trace_html_generated'"
    ).fetchone()
    assert generated is not None and generated["session_id"] == "s1"
    conn.close()
    assert cli.main(["trace", "missing", "--html"]) == 1
    monkeypatch.setattr(trace_html, "open_browser", lambda *_: False)
    assert cli.main(["trace", "s1", "--html", "--open"]) == 0
    assert "warning:" in capsys.readouterr().err
    assert f'"redaction_version":{REDACTION_VERSION}' in path.read_text()


def test_safe_text_redacts_before_truncating():
    secret = "sk-proj-" + "z" * 32
    value = "a" * (trace_html.HEAD_TEXT - 5) + secret + "b" * 30_000
    clean = trace_html.safe_text(value)
    assert secret not in clean and "[REDACTED:openai_key:" in clean
    assert len(clean.encode()) < trace_html.MAX_TEXT


def test_generated_html_has_no_dynamic_html_sinks():
    template = resources.files("metsuke").joinpath("trace_template.html").read_text()
    assert "innerHTML" not in template
    assert "insertAdjacentHTML" not in template
    for sink in ("outerHTML", "document.write", "eval(", "Function(", "srcdoc", "onclick="):
        assert sink not in template
    assert template.count('<script type="application/json" id="trace-data">') == 1
    assert template.index("Content-Security-Policy") < template.index("<script")
    assert os.path.basename(str(resources.files("metsuke").joinpath("trace_template.html"))) == (
        "trace_template.html"
    )
    assert sorted(path.name for path in resources.files("metsuke").iterdir() if path.name.endswith(".html")) == [
        "trace_template.html", "view_template.html"
    ]


def test_archive_fallback_preserves_selected_bodies(trace_env):
    source = trace_env.parent / "source"
    archiver.run(source)
    conn = ledger.connect_readonly()
    live = trace_html.build_trace_data(conn, "s1")
    (source / "project" / "s1.jsonl").unlink()
    archived = trace_html.build_trace_data(conn, "s1")
    conn.close()
    assert archived["req_text"] == live["req_text"]
    assert archived["req_thinking"] == live["req_thinking"]
    assert archived["tool_io"] == live["tool_io"]


@pytest.mark.parametrize("session_id", ["/tmp/evil", "../x", ".hidden", "a..b"])
def test_generate_rejects_unsafe_session_id(trace_env, session_id):
    before = set((trace_env / "traces").glob("*")) if (trace_env / "traces").exists() else set()
    assert trace_html.generate(session_id) is None
    after = set((trace_env / "traces").glob("*")) if (trace_env / "traces").exists() else set()
    assert after == before


def test_explain_html_resolves_request_only_prompt(trace_env, capsys):
    conn = ledger.connect()
    conn.execute(
        "UPDATE request SET prompt_id='request-only' WHERE request_id='ru'"
    )
    conn.commit()
    conn.close()
    assert cli.main(["explain", "request-o", "--html"]) == 0
    assert "#prompt=request-only" in capsys.readouterr().out
    assert cli.main(["explain", "does-not-exist", "--html"]) == 1
    assert "no such prompt" in capsys.readouterr().out


def test_stage6_migration_column_order_matches_new_database(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    old_path = tmp_path / "old.db"
    old = sqlite3.connect(old_path)
    schema = resources.files("metsuke").joinpath("schema.sql").read_text()
    old.executescript(schema)
    for table, columns in (
        ("request", ("end_ts", "api_duration_ms")),
        ("tool_call", ("result_ts", "workflow_run_id")),
        ("agent", ("workflow_run_id",)),
    ):
        for column in columns:
            old.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    old.commit()
    old.close()
    migrated = ledger.connect(old_path)
    fresh = ledger.connect(tmp_path / "fresh.db")
    for table in ("request", "tool_call", "agent"):
        migrated_names = [row[1] for row in migrated.execute(f"PRAGMA table_info({table})")]
        fresh_names = [row[1] for row in fresh.execute(f"PRAGMA table_info({table})")]
        assert migrated_names == fresh_names
    migrated.close()
    fresh.close()


def test_health_timeline_coverage_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path))
    conn = ledger.connect()
    now = time.time()
    conn.executemany(
        """INSERT INTO request(request_id,ts,end_ts,is_synthetic,source)
           VALUES (?,?,?,0,'transcript')""",
        [(f"r{i}", now, now if i < 98 else None) for i in range(100)],
    )
    conn.executemany(
        "INSERT INTO tool_call(tool_use_id,ts,result_ts) VALUES (?,?,?)",
        [(f"t{i}", now, now if i < 98 else None) for i in range(100)],
    )
    statuses = dict(conn.execute(
        "SELECT check_name,status FROM v_health WHERE check_name LIKE '%coverage_7d'"
    ))
    assert statuses == {"request_end_coverage_7d": "ok", "tool_result_coverage_7d": "ok"}
    conn.execute("UPDATE request SET end_ts=NULL WHERE request_id IN ('r96','r97')")
    conn.execute("UPDATE tool_call SET result_ts=NULL WHERE tool_use_id IN ('t96','t97')")
    statuses = dict(conn.execute(
        "SELECT check_name,status FROM v_health WHERE check_name LIKE '%coverage_7d'"
    ))
    assert set(statuses.values()) == {"warn"}
    conn.execute("UPDATE request SET end_ts=NULL WHERE request_id<'r85'")
    conn.execute("UPDATE tool_call SET result_ts=NULL WHERE tool_use_id<'t85'")
    statuses = dict(conn.execute(
        "SELECT check_name,status FROM v_health WHERE check_name LIKE '%coverage_7d'"
    ))
    assert set(statuses.values()) == {"fail"}
    conn.close()


def test_cache_identity_hook_causes_respect_both_time_window_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    conn = ledger.connect()
    cases = (
        ("compact-inside", "PreCompact", 1050, "compaction"),
        ("compact-outside", "PostCompact", 5000, "unknown"),
        ("config-inside", "SessionStart", 1050, "config_change"),
        ("config-outside", "SessionStart", 5000, "unknown"),
    )
    for session_id, hook_kind, hook_ts, _ in cases:
        conn.executemany(
            """INSERT INTO request
               (request_id,session_id,lineage_id,ts,model,input_tok,output_tok,
                cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,
                is_interrupted,source)
               VALUES (?,?,?,?,?,10,1,?,?,0,0,0,'transcript')""",
            (
                (f"{session_id}-previous", session_id, session_id, 1000, "model", 100, 20),
                (f"{session_id}-current", session_id, session_id, 1100, "model", 1000, 0),
            ),
        )
        conn.execute(
            """INSERT INTO hook_event(ts,kind,session_id,payload_json)
               VALUES (?,?,?,?)""",
            (hook_ts, hook_kind, session_id, json.dumps({"case": session_id})),
        )

    causes = dict(
        conn.execute("SELECT session_id,cause FROM v_cache_identity ORDER BY session_id")
    )
    assert causes == {session_id: expected for session_id, _, _, expected in cases}
    conn.close()


@pytest.fixture()
def cache_identity_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "home"))
    conn = ledger.connect()
    yield conn
    conn.close()


def _insert_cache_request(
    conn,
    request_id,
    ts,
    *,
    lineage_id="lineage",
    end_ts=None,
    cache_read=100,
    cache_w5m=0,
    cache_w1h=0,
    interrupted=0,
):
    conn.execute(
        """INSERT INTO request
           (request_id,session_id,lineage_id,ts,end_ts,model,input_tok,output_tok,
            cache_read_tok,cache_w5m_tok,cache_w1h_tok,is_synthetic,
            is_interrupted,source)
           VALUES (?,?,?,?,?,'model',10,1,?,?,?,0,?,'transcript')""",
        (
            request_id,
            lineage_id,
            lineage_id,
            ts,
            end_ts,
            cache_read,
            cache_w5m,
            cache_w1h,
            interrupted,
        ),
    )


def test_cache_identity_5m_ttl_expiry(cache_identity_conn):
    _insert_cache_request(
        cache_identity_conn, "previous", 1000, end_ts=1010, cache_w5m=100
    )
    _insert_cache_request(cache_identity_conn, "current", 1400, cache_read=0)
    cause = cache_identity_conn.execute(
        "SELECT cause FROM v_cache_identity WHERE request_id='current'"
    ).fetchone()[0]
    assert cause == "ttl_expiry"


@pytest.mark.parametrize(
    ("case", "previous", "current", "earlier"),
    [
        ("wait-from-end-at-most-300s",
         {"ts": 1000, "end_ts": 1590, "cache_w5m": 100},
         {"ts": 1600, "cache_read": 0}, None),
        ("partial-loss", {"ts": 1000, "end_ts": 1010, "cache_w5m": 100},
         {"ts": 1400, "cache_read": 100}, None),
        ("live-1h-prefix", {"ts": 1100, "end_ts": 1110, "cache_w5m": 100},
         {"ts": 1500, "cache_read": 0},
         {"ts": 1000, "end_ts": 1010, "cache_w1h": 100}),
        ("mixed-previous-write-at-1h-boundary",
         {"ts": 1000, "end_ts": 1010, "cache_w5m": 100, "cache_w1h": 1},
         {"ts": 4600, "cache_read": 0}, None),
    ],
)
def test_cache_identity_5m_ttl_expiry_negative_cases(
    cache_identity_conn, case, previous, current, earlier
):
    if earlier:
        _insert_cache_request(cache_identity_conn, f"{case}-earlier", **earlier)
    _insert_cache_request(cache_identity_conn, f"{case}-previous", **previous)
    _insert_cache_request(cache_identity_conn, f"{case}-current", **current)
    cause = cache_identity_conn.execute(
        "SELECT cause FROM v_cache_identity WHERE request_id=?", (f"{case}-current",)
    ).fetchone()[0]
    assert cause != "ttl_expiry"


def test_cache_identity_existing_1h_ttl_expiry_rule(cache_identity_conn):
    _insert_cache_request(cache_identity_conn, "previous", 1000, cache_w5m=0)
    _insert_cache_request(cache_identity_conn, "current", 4601, cache_read=0)
    cause = cache_identity_conn.execute(
        "SELECT cause FROM v_cache_identity WHERE request_id='current'"
    ).fetchone()[0]
    assert cause == "ttl_expiry"


def test_cache_identity_interruption_precedes_5m_ttl_expiry(cache_identity_conn):
    _insert_cache_request(
        cache_identity_conn, "previous", 1000, end_ts=1010,
        cache_w5m=100, interrupted=1
    )
    _insert_cache_request(cache_identity_conn, "current", 1400, cache_read=0)
    cause = cache_identity_conn.execute(
        "SELECT cause FROM v_cache_identity WHERE request_id='current'"
    ).fetchone()[0]
    assert cause == "interruption"
