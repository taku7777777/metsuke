#!/bin/bash
# Create the central config once, migrating existing Claude settings/env values.
set -euo pipefail
settings=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}
config_path=${METSUKE_CONFIG:-"$HOME/.metsuke/config.env"}
mkdir -p "$(dirname "$config_path")"
chmod 700 "$(dirname "$config_path")"
if [ -e "$config_path" ]; then
  chmod 600 "$config_path"
  echo "unchanged: $config_path"
  exit 0
fi
python3 - "$settings" "$config_path" <<'PY'
import json
import os
import pathlib
import sys

settings, target = map(pathlib.Path, sys.argv[1:])
try:
    claude_env = (json.loads(settings.read_text()).get("env") or {})
except FileNotFoundError:
    claude_env = {}
defaults = {
    "METSUKE_HOME": str(pathlib.Path.home() / ".metsuke"),
    "METSUKE_SOURCE": str(pathlib.Path.home() / ".claude" / "projects"),
    "METSUKE_BUDGET_DAY": None,
    "METSUKE_BUDGET_WEEK": None,
    "METSUKE_BUDGET_MONTH": None,
    "METSUKE_BUDGET_WARN_ENABLED": "0",
    "METSUKE_BURN_WINDOW_S": "1800",
    "METSUKE_BURN_WARN_USD_H": "45",
    "METSUKE_BURN_CRIT_USD_H": "90",
    "METSUKE_PROMPT_WARN_USD": "3",
    "METSUKE_PROMPT_CRIT_USD": "7.5",
    "METSUKE_CONTEXT_WARN_TOKENS": "200000",
    "METSUKE_CONTEXT_CRIT_TOKENS": "500000",
    "METSUKE_RECEIPT_NOTIFY_ENABLED": "0",
    "METSUKE_RUNAWAY_USD": "5",
    "METSUKE_COLDCACHE_MIN_USD": "0.5",
    "METSUKE_TTL_PRENOTIFY_GAP_S": "3000",
    "METSUKE_NUDGE_DAILY_CAP": "3",
    "METSUKE_HOURLY_VALUE_USD": "0",
    "METSUKE_OTEL_PORT": "4319",
}
lines = [
    "# metsuke central configuration; one KEY=VALUE per line",
    "# Set METSUKE_BUDGET_DAY/WEEK/MONTH to your own limits, then set",
    "# METSUKE_BUDGET_WARN_ENABLED=1 to enable budget warnings.",
]
for key, default in defaults.items():
    raw_value = os.environ.get(key, claude_env.get(key, default))
    if raw_value is None:
        continue
    value = str(raw_value)
    if "\n" in value or "\r" in value:
        raise SystemExit(f"invalid newline in {key}")
    lines.append(f"{key}={value}")
restic = os.environ.get("METSUKE_RESTIC_REPO", claude_env.get("METSUKE_RESTIC_REPO"))
if restic:
    lines.append(f"METSUKE_RESTIC_REPO={restic}")
tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
tmp.write_text("\n".join(lines) + "\n")
os.chmod(tmp, 0o600)
tmp.replace(target)
print(f"created: {target}")
PY
