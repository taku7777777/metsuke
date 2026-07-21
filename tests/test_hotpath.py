import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_hook_sensor(tmp_path):
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "SessionStart"],
        input='{"session_id":"s1"}', text=True, env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )
    assert proc.returncode == 0
    files = list((tmp_path / "spool/hooks").glob("*.ndjson"))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    assert row["metsuke_event"] == "SessionStart" and row["payload"]["session_id"] == "s1"
    assert isinstance(row["metsuke_ts"], float)


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_display_and_throttle(tmp_path):
    script = ["bash", str(ROOT / "scripts/statusline.sh")]
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    sample = json.dumps({"session_id": "s1", "version": "2.1.3", "cost": {"total_cost_usd": 4.2}, "context_window": {"total_input_tokens": 187236, "used_percentage": 61}})
    first = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert first.returncode == 0 and "no data yet" in first.stdout
    state = {"generated_at": 9999999999, "stale": False, "today": {"cost_usd": 37.2, "budget_usd": 150, "burn_rate_usd_h": 52, "pace_ratio": 1.3, "landing_usd": 92}, "sessions": {"s1": {"recent_prompts": [{"cost_usd": 4.1, "interrupted": False}]}}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    second = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "$37.2" in second.stdout and "$52/h" in second.stdout and "1.3x" not in second.stdout
    assert " | \x1b[33m$4.10\x1b[0m |" in second.stdout and "sess $4.2" in second.stdout
    assert "ctx 187K" in second.stdout
    assert len(list((tmp_path / "spool/hooks").glob("*.ndjson"))) == 1
    statusline_sample = next((tmp_path / "spool/hooks").glob("*.ndjson"))
    assert json.loads(statusline_sample.read_text())["payload"]["version"] == "2.1.3"

    state["today"]["budget_usd"] = None
    (tmp_path / "state.json").write_text(json.dumps(state))
    unconfigured = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "⛽$37.2" in unconfigured.stdout and "⛽$37.2/" not in unconfigured.stdout
    state["today"]["budget_usd"] = 150

    state["sessions"]["s1"]["recent_prompts"][0]["interrupted"] = True
    (tmp_path / "state.json").write_text(json.dumps(state))
    interrupted = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "⚡$4.10" in interrupted.stdout

    state["sessions"]["s1"] = {}
    (tmp_path / "state.json").write_text(json.dumps(state))
    missing = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "⏵" not in missing.stdout and "⚡$" not in missing.stdout

    small_sample = json.dumps({"session_id": "s1", "context_window": {"total_input_tokens": 9234, "used_percentage": 19}})
    small = subprocess.run(script, input=small_sample, text=True, env=env, capture_output=True)
    assert "ctx 9.2K" in small.stdout

    yellow_sample = json.dumps({"session_id": "s1", "context_window": {"total_input_tokens": 200000, "used_percentage": 20}})
    yellow = subprocess.run(script, input=yellow_sample, text=True, env=env, capture_output=True)
    assert "ctx \x1b[33m200K\x1b[0m" in yellow.stdout

    critical_sample = json.dumps({"session_id": "s1", "context_window": {"total_input_tokens": 500000, "used_percentage": 50}})
    critical = subprocess.run(script, input=critical_sample, text=True, env=env, capture_output=True)
    assert "ctx \x1b[31m500K\x1b[0m" in critical.stdout

    fallback_sample = json.dumps({"session_id": "s1", "context_window": {"used_percentage": 44}})
    fallback = subprocess.run(script, input=fallback_sample, text=True, env=env, capture_output=True)
    assert "ctx 44%" in fallback.stdout

    fallback_red_sample = json.dumps({"session_id": "s1", "context_window": {"used_percentage": 80}})
    fallback_red = subprocess.run(script, input=fallback_red_sample, text=True, env=env, capture_output=True)
    assert "ctx \x1b[31m80%\x1b[0m" in fallback_red.stdout

    absolute_wins_sample = json.dumps({"session_id": "s1", "context_window": {"total_input_tokens": 812, "used_percentage": 80}})
    absolute_wins = subprocess.run(script, input=absolute_wins_sample, text=True, env=env, capture_output=True)
    assert "ctx 812" in absolute_wins.stdout and "\x1b[31m812" not in absolute_wins.stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
@pytest.mark.parametrize(
    ("state_contents", "expected"),
    [
        pytest.param("", "⚠stale", id="empty"),
        pytest.param(" \n\t", "⚠stale", id="whitespace"),
        pytest.param("null", "⚠stale", id="null"),
        pytest.param("[]", "no data yet", id="array"),
    ],
)
def test_statusline_degenerate_state_is_not_silent(tmp_path, state_contents, expected):
    (tmp_path / "state.json").write_text(state_contents)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/statusline.sh")], input="{}", text=True,
        env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True,
    )
    assert result.returncode == 0
    assert expected in result.stdout
    assert result.stdout.strip()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_prompt_cost_group(tmp_path):
    script = ["bash", str(ROOT / "scripts/statusline.sh")]
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    sample = json.dumps({"session_id": "s1"})
    state = {"generated_at": 9999999999, "stale": False, "today": {}, "sessions": {"s1": {}}}

    def render(session):
        state["sessions"]["s1"] = session
        (tmp_path / "state.json").write_text(json.dumps(state))
        return subprocess.run(script, input=sample, text=True, env=env, capture_output=True).stdout

    recent = [
        {"cost_usd": 4.68, "interrupted": False},
        {"cost_usd": 0.75, "interrupted": False},
        {"cost_usd": 2.24, "interrupted": False},
    ]
    assert " | ⏵$1.22 \x1b[33m$4.68\x1b[0m $0.75 $2.24 |" in render({"inflight_usd": 1.22, "recent_prompts": recent})
    without_inflight = render({"inflight_usd": None, "recent_prompts": recent})
    assert " | \x1b[33m$4.68\x1b[0m $0.75 $2.24 |" in without_inflight and "⏵" not in without_inflight
    assert " | \x1b[33m$4.68\x1b[0m |" in render({"recent_prompts": recent[:1]})
    assert "\x1b[33m⚡$4.68\x1b[0m" in render({"recent_prompts": [{"cost_usd": 4.68, "interrupted": True}]})
    empty = render({"inflight_usd": None, "recent_prompts": []})
    assert "⏵" not in empty and "$0.00" not in empty and " |  |" not in empty

    thresholds = render(
        {
            "inflight_usd": 7.5,
            "recent_prompts": [
                {"cost_usd": 7.49},
                {"cost_usd": 3.0, "detail_url": "file:///tmp/detail.html#prompt=p"},
                {"cost_usd": 2.99},
            ],
        }
    )
    assert "\x1b[31m⏵$7.50\x1b[0m" in thresholds
    assert "\x1b[33m$7.49\x1b[0m" in thresholds
    assert "\x1b]8;;file:///tmp/detail.html#prompt=p\x1b\\\x1b[33m$3.00\x1b[0m\x1b]8;;\x1b\\" in thresholds
    assert "\x1b[33m$2.99" not in thresholds and "\x1b[31m$2.99" not in thresholds


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
@pytest.mark.parametrize(("burn_rate", "color"), [(20, "32"), (45, "33"), (150, "31")])
def test_statusline_burn_rate_color(tmp_path, burn_rate, color):
    state = {"generated_at": 9999999999, "stale": False, "today": {"cost_usd": 1, "budget_usd": 150, "burn_rate_usd_h": burn_rate}, "sessions": {}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/statusline.sh")], input="{}", text=True,
        env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True,
    )
    assert f"\x1b[{color}m${burn_rate}/h\x1b[0m" in result.stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
@pytest.mark.parametrize("burn_value", [pytest.param(None, id="null"), pytest.param("missing", id="missing")])
def test_statusline_burn_rate_missing(tmp_path, burn_value):
    today = {"cost_usd": 1, "budget_usd": 150}
    if burn_value != "missing":
        today["burn_rate_usd_h"] = burn_value
    state = {"generated_at": 9999999999, "stale": False, "today": today, "sessions": {}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/statusline.sh")], input="{}", text=True,
        env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True,
    )
    assert "/h" not in result.stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_ttl_expiry_time(tmp_path):
    script = ["bash", str(ROOT / "scripts/statusline.sh")]
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    sample = json.dumps({"session_id": "s1"})
    now = time.time()
    state = {"generated_at": now, "stale": False, "today": {}, "sessions": {"s1": {"last_ts": now - 1000}}}

    (tmp_path / "state.json").write_text(json.dumps(state))
    normal = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    expected = time.strftime("%H:%M", time.localtime(now - 1000 + 3600))
    assert f"🔥{expected}" in normal.stdout
    assert f"\x1b[31m🔥{expected}" not in normal.stdout

    state["sessions"]["s1"]["last_ts"] = now - 3000
    (tmp_path / "state.json").write_text(json.dumps(state))
    near = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    near_expected = time.strftime("%H:%M", time.localtime(now - 3000 + 3600))
    assert f"\x1b[31m🔥{near_expected}\x1b[0m" in near.stdout

    state["sessions"]["s1"]["last_ts"] = now - 4000
    (tmp_path / "state.json").write_text(json.dumps(state))
    expired = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "❄" in expired.stdout and "🔥" not in expired.stdout

    state["sessions"] = {}
    (tmp_path / "state.json").write_text(json.dumps(state))
    missing = subprocess.run(script, input=sample, text=True, env=env, capture_output=True)
    assert "🔥" not in missing.stdout and "❄" not in missing.stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_context_warn_marker_and_cooldown(tmp_path):
    script = ["bash", str(ROOT / "scripts/statusline.sh")]
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    def run(pct):
        return subprocess.run(
            script,
            input=json.dumps(
                {"session_id": "s1", "context_window": {"used_percentage": pct}}
            ),
            text=True,
            env=env,
            capture_output=True,
        )
    run(61)
    marker = tmp_path / "state/ctxwarn-s1"
    assert marker.read_text().strip() == "61"
    marker.unlink()
    (tmp_path / "state/ctxwarned-s1").touch()
    run(61)
    assert not marker.exists()
    (tmp_path / "state/ctxwarned-s1").unlink()
    run(59)
    assert not marker.exists()


def test_hotpath_discipline():
    hook = (ROOT / "scripts/hook-sensor.sh").read_text().lower()
    status = (ROOT / "scripts/statusline.sh").read_text().lower()
    for source in (hook, status):
        assert "sqlite" not in source and "ledger.db" not in source
    assert "metsuke sync" not in status


def _run_statusline_with_jq_counter(tmp_path, throttle_sensor):
    real_jq = shutil.which("jq")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    counter = tmp_path / "jq-calls"
    wrapper = shim_dir / "jq"
    wrapper.write_text(
        '#!/bin/bash\n'
        'printf "call\\n" >>"$JQ_COUNTER"\n'
        'exec "$REAL_JQ" "$@"\n'
    )
    wrapper.chmod(0o755)
    state = {
        "generated_at": time.time(), "stale": False,
        "today": {"cost_usd": 37.2, "budget_usd": 150, "burn_rate_usd_h": 52},
        "sessions": {"s1": {"recent_prompts": [{"cost_usd": 4.1}]}},
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    if throttle_sensor:
        (tmp_path / "state").mkdir()
        (tmp_path / "state/sl-s1.last").write_text(f"{int(time.time())}\n")
    env = {
        "METSUKE_HOME": str(tmp_path),
        "PATH": f"{shim_dir}:/usr/bin:/bin",
        "JQ_COUNTER": str(counter),
        "REAL_JQ": real_jq,
    }
    sample = json.dumps({"session_id": "s1", "cost": {"total_cost_usd": 4.2}})
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/statusline.sh")], input=sample, text=True,
        env=env, capture_output=True,
    )
    return result, len(counter.read_text().splitlines())


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_jq_call_budget_normal_render(tmp_path):
    result, calls = _run_statusline_with_jq_counter(tmp_path, throttle_sensor=True)
    assert result.returncode == 0 and result.stdout.strip()
    # R1 (50ms) proxy: jq ~=3ms/call and bash startup ~=2.6ms; avoid flaky wall-clock timing.
    assert calls <= 3  # implementation uses 2; one call of regression headroom


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_statusline_jq_call_budget_with_sensor_write(tmp_path):
    result, calls = _run_statusline_with_jq_counter(tmp_path, throttle_sensor=False)
    assert result.returncode == 0 and result.stdout.strip()
    assert len(list((tmp_path / "spool/hooks").glob("*.ndjson"))) == 1
    # R1 (50ms) proxy: jq ~=3ms/call and bash startup ~=2.6ms; avoid flaky wall-clock timing.
    assert calls <= 4  # implementation uses 3; one call of regression headroom


def _run_hook(tmp_path, state, sid="s1", prompt="hello", env_overrides=None):
    (tmp_path / "state.json").write_text(json.dumps(state))
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "UserPromptSubmit"],
        input=json.dumps({"session_id": sid, "prompt": prompt}),
        text=True,
        env=env,
        capture_output=True,
    )


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
@pytest.mark.parametrize(
    ("cost", "needle", "rule"),
    [(80, "50%", "budget_warn_50"), (125, "80%", "budget_warn_80")],
)
def test_budget_warn_once_and_tier(tmp_path, cost, needle, rule):
    state = {"generated_at": 9999999999, "today": {"cost_usd": cost, "budget_usd": 150, "burn_rate_usd_h": 20, "landing_usd": 170}, "sessions": {}}
    enabled = {"METSUKE_BUDGET_WARN_ENABLED": "1"}
    first = _run_hook(tmp_path, state, env_overrides=enabled)
    second = _run_hook(tmp_path, state, env_overrides=enabled)
    assert needle in json.loads(first.stdout)["systemMessage"]
    assert second.stdout == ""
    nudges = list((tmp_path / "spool/hooks").glob(f"*-nudge-{rule}.ndjson"))
    assert len(nudges) == 1
    if cost == 125:
        assert "50%" not in first.stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_budget_100_warns_without_block_and_stale_is_silent(tmp_path):
    state = {"generated_at": 9999999999, "today": {"cost_usd": 151, "budget_usd": 150}, "sessions": {}}
    enabled = {"METSUKE_BUDGET_WARN_ENABLED": "1"}
    warned = _run_hook(tmp_path, state, env_overrides=enabled)
    output = json.loads(warned.stdout)
    assert "100%" in output["systemMessage"] and "decision" not in output
    assert len(list((tmp_path / "spool/hooks").glob("*-nudge-budget_warn_100.ndjson"))) == 1
    (tmp_path / "state" / "unlock-until").write_text("9999999999\n")
    assert _run_hook(tmp_path, state, env_overrides=enabled).stdout == ""
    stale = dict(state, generated_at=1)
    assert _run_hook(tmp_path, stale, env_overrides=enabled).stdout == ""


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_budget_warning_can_be_disabled_without_disabling_usage_state(tmp_path):
    state = {
        "generated_at": 9999999999,
        "today": {"cost_usd": 200, "budget_usd": 150},
        "sessions": {},
    }
    result = _run_hook(
        tmp_path, state, env_overrides={"METSUKE_BUDGET_WARN_ENABLED": "0"}
    )
    assert result.stdout == ""
    assert not list((tmp_path / "spool/hooks").glob("*-nudge-budget_warn_*.ndjson"))


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_coldcache_marker_cap_and_nudge_spool(tmp_path):
    now = time.time()
    sessions = {
        f"s{i}": {"last_ts": now - 4000 - i, "rebuild_cost_usd": 1.25}
        for i in range(4)
    }
    state = {"generated_at": now, "today": {"cost_usd": 0, "budget_usd": 150}, "sessions": sessions}
    env = {"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    (tmp_path / "state.json").write_text(json.dumps(state))
    outputs = []
    for i in range(4):
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts/hook-sensor.sh"), "UserPromptSubmit"],
            input=json.dumps({"session_id": f"s{i}", "prompt": "resume"}), text=True,
            env=env, capture_output=True,
        )
        outputs.append(proc.stdout)
    assert sum(bool(x) for x in outputs) == 3
    assert "$1.25" in outputs[0] and "/handoff" in outputs[0]
    assert len(list((tmp_path / "spool/hooks").glob("*-nudge-coldcache_warn.ndjson"))) == 3
    assert subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "UserPromptSubmit"],
        input=json.dumps({"session_id": "s0"}), text=True, env=env, capture_output=True,
    ).stdout == ""


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_coldcache_respects_state_threshold_with_legacy_fallback(tmp_path):
    now = time.time()
    state = {
        "generated_at": now,
        "today": {"cost_usd": 0, "budget_usd": 150},
        "sessions": {
            sid: {"last_ts": now - 4000, "rebuild_cost_usd": 1.25}
            for sid in ("s1", "s2")
        },
    }
    assert "$1.25" in _run_hook(tmp_path, state).stdout

    state["thresholds"] = {"coldcache_min_usd": 2.0}
    assert _run_hook(tmp_path, state, sid="s2").stdout == ""


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_context_warn_hook_consumes_marker(tmp_path):
    marker = tmp_path / "state/ctxwarn-s1"
    marker.parent.mkdir()
    marker.write_text("61\n")
    state = {"generated_at": time.time(), "today": {"cost_usd": 0, "budget_usd": 150}, "sessions": {}}
    result = _run_hook(tmp_path, state)
    assert "/handoff" in json.loads(result.stdout)["systemMessage"]
    assert not marker.exists() and (tmp_path / "state/ctxwarned-s1").exists()
    fired = list((tmp_path / "spool/hooks").glob("*-nudge-ctx_warn.ndjson"))
    assert len(fired) == 1 and json.loads(fired[0].read_text())["payload"]["rule"] == "ctx_warn"


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_postcompact_marker_rearms_context_warning(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "ctxwarn-s1").write_text("70\n")
    (state_dir / "ctxwarned-s1").touch()
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "PostCompact"],
        input=json.dumps({"session_id": "s1"}), text=True,
        env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True,
    )
    assert result.returncode == 0 and (state_dir / "compacted-s1").exists()
    assert not (state_dir / "ctxwarn-s1").exists() and not (state_dir / "ctxwarned-s1").exists()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
@pytest.mark.parametrize("state", [None, {"generated_at": 1, "today": {}}])
def test_compact_recovery_without_fresh_state(tmp_path, state):
    marker = tmp_path / "state/compacted-s1"
    marker.parent.mkdir()
    marker.write_text("1\n")
    if state is None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/hook-sensor.sh"), "UserPromptSubmit"],
            input=json.dumps({"session_id": "s1", "prompt": "continue"}), text=True,
            env={"METSUKE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True,
        )
    else:
        result = _run_hook(tmp_path, state)
    output = json.loads(result.stdout)
    assert "圧縮" in output["hookSpecificOutput"]["additionalContext"]
    assert not marker.exists()
    fired = list((tmp_path / "spool/hooks").glob("*-nudge-compact_recovery.ndjson"))
    assert len(fired) == 1 and json.loads(fired[0].read_text())["payload"]["rule"] == "compact_recovery"


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_compact_recovery_is_delivered_with_budget_100_warning(tmp_path):
    marker = tmp_path / "state/compacted-s1"
    marker.parent.mkdir()
    marker.write_text("1\n")
    state = {"generated_at": time.time(), "today": {"cost_usd": 150, "budget_usd": 150}, "sessions": {}}
    output = json.loads(
        _run_hook(
            tmp_path,
            state,
            env_overrides={"METSUKE_BUDGET_WARN_ENABLED": "1"},
        ).stdout
    )
    assert "100%" in output["systemMessage"]
    assert "COMPACT RECOVERY" in output["hookSpecificOutput"]["additionalContext"]
    assert not marker.exists()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_posttooluse_trigger_only_and_throttle(tmp_path):
    spool = tmp_path / "spool/hooks"
    spool.mkdir(parents=True)
    source = tmp_path / "empty-projects"
    source.mkdir()
    env = {
        "METSUKE_HOME": str(tmp_path),
        "METSUKE_SOURCE": str(source),
        "PATH": "/usr/bin:/bin",
    }
    command = ["bash", str(ROOT / "scripts/hook-sensor.sh"), "PostToolUse"]
    subprocess.run(command, input="{}", text=True, env=env, capture_output=True)
    marker = tmp_path / "state/sync-trigger.last"
    first = marker.read_text()
    subprocess.run(command, input="{}", text=True, env=env, capture_output=True)
    assert marker.read_text() == first
    assert not list(spool.glob("*.ndjson"))
