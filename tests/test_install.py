import json
import os
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _run(script: str, home: Path, *args: str, env_overrides=None):
    settings = home / ".claude" / "settings.json"
    config = home / ".metsuke" / "config.env"
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CLAUDE_SETTINGS": str(settings),
        "METSUKE_CONFIG": str(config),
        "METSUKE_OTEL_PORT": "54329",
        "METSUKE_SKIP_PREREQ_CHECK": "1",
    }
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / script), *args],
        env=env,
        text=True,
        capture_output=True,
    )


def test_unified_install_and_dry_run_uninstall(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    installed = _run("install.sh", home, "--skip-git", "--skip-launchd")
    assert installed.returncode == 0, installed.stderr
    settings = home / ".claude" / "settings.json"
    config = home / ".metsuke" / "config.env"
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == str(ROOT / "scripts/statusline.sh")
    assert data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert not any(key.startswith("METSUKE_") for key in data["env"])
    assert config.is_file() and config.stat().st_mode & 0o777 == 0o600

    before = settings.read_bytes()
    planned = _run("uninstall.sh", home)
    assert planned.returncode == 0 and "dry-run" in planned.stdout
    assert settings.read_bytes() == before

    removed = _run(
        "uninstall.sh",
        home,
        "--apply",
        "--git-root",
        str(home / "no-repositories"),
        env_overrides={"METSUKE_LAUNCHCTL": "/bin/true"},
    )
    assert removed.returncode == 0, removed.stderr
    data = json.loads(settings.read_text())
    assert "statusLine" not in data and "hooks" not in data
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in data.get("env", {})
    assert config.exists(), "configuration and data are retained unless --purge-data is explicit"


def test_git_hook_install_refuses_existing_hook(tmp_path):
    home = tmp_path / "user"
    hook = home / "repos/example/.git/hooks/post-commit"
    hook.parent.mkdir(parents=True)
    original = "#!/usr/bin/env python3\nprint('kept')\n"
    hook.write_text(original)
    result = _run("install-git-hooks.sh", home, str(home / "repos"))
    assert result.returncode == 0
    assert "refused: existing hook left unchanged" in result.stderr
    assert hook.read_text() == original


def test_installer_can_skip_claude_hooks_and_statusline_independently(tmp_path):
    hooks_home = tmp_path / "hooks-only"
    hooks_home.mkdir()
    hooks_only = _run(
        "install.sh",
        hooks_home,
        "--skip-statusline",
        "--skip-otel",
        "--skip-launchd",
    )
    assert hooks_only.returncode == 0, hooks_only.stderr
    hooks_settings = json.loads((hooks_home / ".claude/settings.json").read_text())
    assert "hooks" in hooks_settings and "statusLine" not in hooks_settings

    status_home = tmp_path / "status-only"
    status_home.mkdir()
    status_only = _run(
        "install.sh",
        status_home,
        "--skip-claude-hooks",
        "--skip-otel",
        "--skip-launchd",
    )
    assert status_only.returncode == 0, status_only.stderr
    status_settings = json.loads((status_home / ".claude/settings.json").read_text())
    assert "statusLine" in status_settings and "hooks" not in status_settings


def test_otel_port_is_configurable_and_conflicts_are_rejected(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    installed = _run(
        "install-otel-env.sh",
        home,
        env_overrides={"METSUKE_OTEL_PORT": str(port)},
    )
    assert installed.returncode == 0, installed.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://localhost:{port}"

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    conflict_home = tmp_path / "conflict-user"
    conflict_home.mkdir()
    conflict = _run(
        "install-otel-env.sh",
        conflict_home,
        env_overrides={"METSUKE_OTEL_PORT": str(occupied.getsockname()[1])},
    )
    occupied.close()
    assert conflict.returncode != 0
    assert "OTel port" in conflict.stderr and "unavailable" in conflict.stderr


def test_launchd_install_waits_for_keepalive_removal_and_skips_unchanged(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    fake_state = tmp_path / "launchctl-state"
    fake_state.mkdir()
    fake_log = tmp_path / "launchctl.log"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        """#!/bin/bash
set -eu
state=$FAKE_LAUNCHCTL_STATE
log=$FAKE_LAUNCHCTL_LOG
cmd=$1
target=${2:-}
label=${target##*/}
printf '%s %s\\n' "$cmd" "$target" >>"$log"
case "$cmd" in
  print)
    if [ -f "$state/$label.removing" ]; then
      count=$(cat "$state/$label.removing")
      if [ "$count" -gt 1 ]; then
        printf '%s\\n' "$((count - 1))" >"$state/$label.removing"
        printf 'state = running\\nprogram = fake\\n'
        exit 0
      fi
      rm -f "$state/$label.removing" "$state/$label.loaded"
      exit 1
    fi
    [ -f "$state/$label.loaded" ] || exit 1
    printf 'state = running\\nprogram = fake\\n'
    ;;
  bootout)
    printf '2\\n' >"$state/$label.removing"
    ;;
  bootstrap)
    plist=$3
    label=$(basename "$plist" .plist)
    [ ! -f "$state/$label.removing" ] || exit 5
    : >"$state/$label.loaded"
    ;;
esac
"""
    )
    fake_launchctl.chmod(0o700)
    labels = (
        "com.metsuke.archiver",
        "com.metsuke.tick",
        "com.metsuke.analyst",
        "com.metsuke.deadman",
        "com.metsuke.backup",
        "com.metsuke.otelcol",
    )
    for label in labels:
        (fake_state / f"{label}.loaded").touch()
    env = {
        "METSUKE_LAUNCHCTL": str(fake_launchctl),
        "METSUKE_OTELCOL_BIN": "/bin/true",
        "METSUKE_OTEL_PORT": "54321",
        "METSUKE_LAUNCHD_POLL_SECONDS": "0",
        "FAKE_LAUNCHCTL_STATE": str(fake_state),
        "FAKE_LAUNCHCTL_LOG": str(fake_log),
    }

    first = _run("install-launchd.sh", home, env_overrides=env)
    assert first.returncode == 0, first.stderr
    first_log = fake_log.read_text()
    assert first_log.count("bootout ") == len(labels)
    assert first_log.count("bootstrap ") == len(labels) - 1
    assert not (fake_state / "com.metsuke.backup.loaded").exists()
    assert not (home / "Library/LaunchAgents/com.metsuke.backup.plist").exists()
    assert "disabled: com.metsuke.backup" in first.stdout
    assert "127.0.0.1:54321" in (home / ".metsuke/otel/otelcol.yaml").read_text()
    assert not list(fake_state.glob("*.removing"))

    fake_log.write_text("")
    second = _run("install-launchd.sh", home, env_overrides=env)
    assert second.returncode == 0, second.stderr
    second_log = fake_log.read_text()
    assert "bootout " not in second_log and "bootstrap " not in second_log
    assert second.stdout.count("unchanged:") >= len(labels) - 1
