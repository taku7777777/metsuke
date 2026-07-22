import json
import os
import plistlib
import shutil
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
        # Never let a test register a throwaway bundle in the real LaunchServices
        # database; every installer run in this file is a no-op for Spotlight.
        "METSUKE_LSREGISTER": "/usr/bin/true",
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


def test_app_bundle_generation_is_idempotent_and_uses_absolute_paths(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    first = _run("install.sh", home, "--skip-git", "--skip-launchd")
    assert first.returncode == 0, first.stderr
    app = home / "Applications" / "Metsuke.app"
    launcher = app / "Contents" / "MacOS" / "Metsuke"
    plist = app / "Contents" / "Info.plist"
    assert launcher.is_file() and plist.is_file()
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert "macOS app:" in first.stdout
    assert "installed: " + str(launcher) in first.stdout

    snapshot = {path: path.read_bytes() for path in (launcher, plist)}
    modes = {path: path.stat().st_mode for path in (launcher, plist)}
    second = _run("install.sh", home, "--skip-git", "--skip-launchd")
    assert second.returncode == 0, second.stderr
    assert "unchanged: " + str(launcher) in second.stdout
    assert {path: path.read_bytes() for path in (launcher, plist)} == snapshot
    assert {path: path.stat().st_mode for path in (launcher, plist)} == modes
    assert sorted(p.name for p in app.rglob("*")) == ["Contents", "Info.plist", "MacOS", "Metsuke"]

    # A Dock or Spotlight launch has no shell PATH and no uv, so the bundle must
    # name the venv entrypoint by absolute path.
    script = launcher.read_text()
    binary = str(ROOT / ".venv/bin/metsuke")
    assert f'exec "{binary}" dashboard open' in script
    assert f"# metsuke-target: {binary}" in script
    assert Path(binary).is_file()
    assert "uv " not in script and "uv run" not in script
    for line in script.splitlines():
        if line.startswith("exec ") or line.startswith("mkdir "):
            assert '"/' in line, f"relative path in launcher: {line}"
    assert "metsuke dashboard open" not in script, "must not rely on PATH lookup"

    plist_text = plist.read_text()
    for fragment in (
        "<key>CFBundleIdentifier</key>",
        "<string>com.metsuke.app</string>",
        "<key>CFBundleExecutable</key>",
        "<string>Metsuke</string>",
        "<key>CFBundlePackageType</key>",
        "<string>APPL</string>",
    ):
        assert fragment in plist_text

    # plistlib validates the structure on every platform, while plutil is a
    # macOS-only tool that is unavailable on Linux CI runners.
    plist_data = plistlib.loads(plist.read_bytes())
    assert plist_data["CFBundleExecutable"] == "Metsuke"
    assert plist_data["CFBundleIdentifier"] == "com.metsuke.app"
    assert plist_data["CFBundlePackageType"] == "APPL"

    plutil_path = shutil.which("plutil")
    if plutil_path:
        plutil = subprocess.run(
            [plutil_path, "-lint", str(plist)], capture_output=True, text=True
        )
        assert plutil.returncode == 0, plutil.stdout + plutil.stderr


def test_installer_can_skip_the_app(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    result = _run("install.sh", home, "--skip-git", "--skip-launchd", "--skip-app")
    assert result.returncode == 0, result.stderr
    assert "macOS app: skipped" in result.stdout
    assert "macOS app (--skip-app)" in result.stdout
    assert not (home / "Applications").exists()


def test_uninstall_removes_dashboard_surface_and_keeps_ledger_and_archive(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    installed = _run("install.sh", home, "--skip-git", "--skip-launchd")
    assert installed.returncode == 0, installed.stderr
    data_home = home / ".metsuke"
    app = home / "Applications" / "Metsuke.app"
    state_dir = data_home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    dashboard_files = [
        state_dir / "dashboard-state.json",
        state_dir / "dashboard.lock",
        state_dir / "dashboard-secret",
        state_dir / "dashboard-errors.log",
        state_dir / "trace-cache.json",
    ]
    for path in dashboard_files:
        path.write_text("derived")
    traces = data_home / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    (traces / "session.html").write_text("<html></html>")
    ledger_db = data_home / "ledger.db"
    ledger_db.write_bytes(b"irreplaceable ledger")
    archive = data_home / "archive"
    (archive / "segments").mkdir(parents=True)
    (archive / "manifest.jsonl").write_text("{}\n")
    (archive / "segments" / "2026-07.jsonl.zst").write_bytes(b"irreplaceable archive")

    planned = _run("uninstall.sh", home, "--git-root", str(home / "no-repositories"))
    assert planned.returncode == 0 and "dry-run" in planned.stdout
    assert str(app) in planned.stdout
    assert "retained:" in planned.stdout and "ledger.db" in planned.stdout
    assert app.exists() and all(path.exists() for path in dashboard_files)

    removed = _run(
        "uninstall.sh",
        home,
        "--apply",
        "--git-root",
        str(home / "no-repositories"),
        env_overrides={"METSUKE_LAUNCHCTL": "/usr/bin/true"},
    )
    assert removed.returncode == 0, removed.stderr
    assert not app.exists()
    assert not any(path.exists() for path in dashboard_files)
    assert not traces.exists()
    assert ledger_db.read_bytes() == b"irreplaceable ledger"
    assert (archive / "segments" / "2026-07.jsonl.zst").read_bytes() == b"irreplaceable archive"
    assert (archive / "manifest.jsonl").is_file()


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
