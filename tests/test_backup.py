import json
import shutil
import subprocess
from pathlib import Path

from metsuke import archiver, backup, config


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_backup_requires_an_explicit_repository(monkeypatch, capsys):
    monkeypatch.delenv("METSUKE_RESTIC_REPO", raising=False)
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/restic")
    assert backup.run() == 1
    assert "set METSUKE_RESTIC_REPO explicitly" in capsys.readouterr().out


def test_restic_binary_falls_back_to_homebrew_path(tmp_path, monkeypatch):
    binary = tmp_path / "restic"
    binary.touch(mode=0o700)
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)
    monkeypatch.setattr(backup, "RESTIC_CANDIDATES", (binary,))
    assert backup.restic_binary() == str(binary)


def test_backup_includes_decision_artifacts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "offsite"
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_RESTIC_REPO", str(repo))
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/restic")
    config.ensure_dirs()
    (config.archive_dir() / "manifest.jsonl").write_text("{}\n")
    (config.reports_dir() / "week.md").write_text("report")
    (config.handoffs_dir() / "handoff.md").write_text("handoff")
    calls = []

    def fake(args):
        calls.append(args)
        return _completed(args)

    monkeypatch.setattr(backup, "_restic", fake)
    assert backup.run() == 0
    command = next(args for args in calls if args and args[0] == "backup")
    assert str(config.archive_dir()) in command
    assert str(config.reports_dir()) in command
    assert str(config.handoffs_dir()) in command
    marker = json.loads((config.state_dir() / "last_backup.json").read_text())
    assert marker["repo"] == str(repo) and str(config.reports_dir()) in marker["targets"]


def test_backup_verify_checks_restored_segment_sha(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("METSUKE_HOME", str(home))
    monkeypatch.setenv("METSUKE_SOURCE", str(source))
    monkeypatch.setenv("METSUKE_RESTIC_REPO", str(tmp_path / "offsite"))
    transcript = source / "project/session.jsonl"
    transcript.parent.mkdir()
    transcript.write_text('{"type":"system"}\n')
    archiver.run(source)
    entry = archiver.manifest_entries()[-1]

    def fake(args):
        if args[0] == "snapshots":
            return _completed(args, stdout='[{"id":"snapshot"}]')
        target = Path(args[args.index("--target") + 1]) / "restored/archive"
        target.mkdir(parents=True)
        shutil.copy2(config.manifest_path(), target / "manifest.jsonl")
        segment_target = target / "segments" / entry["seg"]
        segment_target.parent.mkdir(parents=True)
        shutil.copy2(config.segments_dir() / entry["seg"], segment_target)
        return _completed(args)

    monkeypatch.setattr(backup, "_restic", fake)
    assert backup.verify_restore() == 0
