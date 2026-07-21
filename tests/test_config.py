import json
import shutil
import subprocess
from pathlib import Path

import pytest

from metsuke import cli, config, ledger, state


ROOT = Path(__file__).parents[1]


def _central(home: Path, content: str) -> Path:
    path = home / ".metsuke" / "config.env"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_central_config_and_explicit_env_precedence(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    user_home.mkdir()
    configured_home = tmp_path / "data"
    configured_source = tmp_path / "projects"
    central = _central(
        user_home,
        f"METSUKE_HOME={configured_home}\nMETSUKE_SOURCE={configured_source}\n"
        "METSUKE_BUDGET_DAY=42\n",
    )
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("METSUKE_CONFIG", str(central))
    monkeypatch.delenv("METSUKE_HOME", raising=False)
    monkeypatch.delenv("METSUKE_SOURCE", raising=False)
    assert config.home() == configured_home
    assert config.source_dir() == configured_source
    assert config.float_value("METSUKE_BUDGET_DAY", 150) == 42
    monkeypatch.setenv("METSUKE_HOME", str(tmp_path / "override"))
    assert config.home() == tmp_path / "override"


def test_state_uses_runtime_central_budget(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    user_home.mkdir()
    data = tmp_path / "data"
    central = _central(
        user_home,
        f"METSUKE_HOME={data}\nMETSUKE_SOURCE={tmp_path / 'source'}\n"
        "METSUKE_BUDGET_DAY=41\nMETSUKE_BUDGET_WEEK=142\nMETSUKE_BUDGET_MONTH=543\n",
    )
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("METSUKE_CONFIG", str(central))
    for key in ("METSUKE_HOME", "METSUKE_SOURCE", "METSUKE_BUDGET_DAY"):
        monkeypatch.delenv(key, raising=False)
    conn = ledger.connect()
    result = state.build(conn)
    assert result["today"]["budget_usd"] == 41
    assert result["week"]["budget_usd"] == 142
    assert result["month"]["budget_usd"] == 543
    conn.close()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_hook_sensor_reads_central_config(tmp_path):
    user_home = tmp_path / "user"
    user_home.mkdir()
    data = tmp_path / "central-data"
    _central(user_home, f"METSUKE_HOME={data}\n")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/hook-sensor.sh"), "SessionStart"],
        input='{"session_id":"central"}',
        text=True,
        env={"HOME": str(user_home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 0
    assert len(list((data / "spool/hooks").glob("*.ndjson"))) == 1


def test_install_config_migrates_claude_env_once(tmp_path):
    user_home = tmp_path / "user"
    settings = user_home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"env": {"METSUKE_BUDGET_DAY": "77", "METSUKE_SOURCE": "/old/source"}})
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-config.sh")],
        env={"HOME": str(user_home), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    path = user_home / ".metsuke/config.env"
    content = path.read_text()
    assert "METSUKE_BUDGET_DAY=77" in content
    assert "METSUKE_BUDGET_WARN_ENABLED=0" in content
    assert "METSUKE_PROMPT_WARN_USD=3" in content
    assert "METSUKE_PROMPT_CRIT_USD=7.5" in content
    assert "METSUKE_CONTEXT_WARN_TOKENS=200000" in content
    assert "METSUKE_CONTEXT_CRIT_TOKENS=500000" in content
    assert "METSUKE_RECEIPT_NOTIFY_ENABLED=0" in content
    assert "METSUKE_SOURCE=/old/source" in content
    assert path.stat().st_mode & 0o777 == 0o600
    before = content
    settings.write_text(json.dumps({"env": {"METSUKE_BUDGET_DAY": "999"}}))
    subprocess.run(
        ["bash", str(ROOT / "scripts/install-config.sh")],
        env={"HOME": str(user_home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
    )
    assert path.read_text() == before

    public_home = tmp_path / "public-user"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-config.sh")],
        env={"HOME": str(public_home), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    content = (public_home / ".metsuke/config.env").read_text()
    assert "METSUKE_BUDGET_DAY=" not in content
    assert "METSUKE_BUDGET_WEEK=" not in content
    assert "METSUKE_BUDGET_MONTH=" not in content
    assert "METSUKE_BUDGET_WARN_ENABLED=0" in content
    assert "Set METSUKE_BUDGET_DAY/WEEK/MONTH to your own limits" in content


def test_config_cli_and_invalid_key(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.env"
    path.write_text(f"METSUKE_HOME={tmp_path / 'data'}\n")
    path.chmod(0o600)
    monkeypatch.setenv("METSUKE_CONFIG", str(path))
    monkeypatch.delenv("METSUKE_HOME", raising=False)
    assert cli.main(["config", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["path"] == str(path)
    path.write_text("UNKNOWN=value\n")
    assert cli.main(["config"]) == 1
    assert "invalid config" in capsys.readouterr().err
