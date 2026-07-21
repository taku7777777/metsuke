"""Project-key display helpers."""

from pathlib import Path


def display_name(value: str | None) -> str:
    """Shorten Claude's encoded current-home prefix without assuming a username."""
    project = value or "—"
    encoded_home = Path.home().as_posix().replace("/", "-")
    return project.replace(f"{encoded_home}-", "~", 1)
