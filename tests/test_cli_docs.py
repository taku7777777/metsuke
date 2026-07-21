import argparse
import re
from pathlib import Path

import pytest

from metsuke import cli


ROOT = Path(__file__).parents[1]


def test_documented_top_level_commands_match_parser(monkeypatch, capsys):
    parser_commands = set()
    original = argparse._SubParsersAction.add_parser

    def recording_add_parser(action, name, *args, **kwargs):
        if action.dest == "cmd":
            parser_commands.add(name)
        return original(action, name, *args, **kwargs)

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", recording_add_parser)
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    capsys.readouterr()

    source = (ROOT / "docs/cli/commands.md").read_text()
    documented = set(re.findall(r"^\| `metsuke ([a-z][a-z-]*)", source, re.MULTILINE))
    assert documented == parser_commands
