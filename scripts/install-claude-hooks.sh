#!/bin/bash
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
settings=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}
skip_hooks=false
skip_statusline=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-hooks) skip_hooks=true ;;
    --skip-statusline) skip_statusline=true ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
"$repo/scripts/install-config.sh"
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || printf '{}\n' >"$settings"
cp "$settings" "$settings.bak-metsuke-stage3"
python3 - "$settings" "$repo" "$skip_hooks" "$skip_statusline" <<'PY'
import json, pathlib, sys
path, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
skip_hooks, skip_statusline = sys.argv[3] == "true", sys.argv[4] == "true"
data = json.loads(path.read_text())
env = data.get("env") or {}
for key in (
    "METSUKE_HOME", "METSUKE_SOURCE", "METSUKE_BUDGET_DAY", "METSUKE_BUDGET_WEEK",
    "METSUKE_BUDGET_MONTH", "METSUKE_BUDGET_WARN_ENABLED", "METSUKE_BURN_WINDOW_S", "METSUKE_BURN_WARN_USD_H",
    "METSUKE_BURN_CRIT_USD_H", "METSUKE_PROMPT_WARN_USD", "METSUKE_PROMPT_CRIT_USD",
    "METSUKE_CONTEXT_WARN_TOKENS", "METSUKE_CONTEXT_CRIT_TOKENS",
    "METSUKE_RECEIPT_NOTIFY_ENABLED", "METSUKE_RUNAWAY_USD", "METSUKE_COLDCACHE_MIN_USD",
    "METSUKE_TTL_PRENOTIFY_GAP_S", "METSUKE_NUDGE_DAILY_CAP", "METSUKE_HOURLY_VALUE_USD",
    "METSUKE_OTEL_PORT", "METSUKE_RESTIC_REPO",
):
    env.pop(key, None)
if env:
    data["env"] = env
else:
    data.pop("env", None)
status = str(repo / "scripts/statusline.sh")
if not skip_statusline:
    if "statusLine" in data:
        print("warning: statusLine already exists; skipped", file=sys.stderr)
    else:
        data["statusLine"] = {"type": "command", "command": status}
if not skip_hooks:
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "PostCompact", "PostToolUse", "Notification"):
        command = f"{repo}/scripts/hook-sensor.sh {event}"
        entries = hooks.setdefault(event, [])
        existing = any(h.get("command") == command for e in entries for h in e.get("hooks", []))
        if not existing:
            entries.append(
                {"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 2}]}
            )
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
tmp.replace(path)
PY
if [ "$skip_hooks" = false ]; then
  commands_dir="$(dirname "$settings")/commands"
  target="$commands_dir/handoff.md"
  mkdir -p "$commands_dir"
  if [ ! -e "$target" ] || grep -q '<!-- metsuke handoff command v' "$target"; then
    cp "$repo/claude/commands/handoff.md" "$target"
  else
    echo "warning: $target is user-managed; skipped" >&2
  fi
fi
echo "metsuke hooks merged into $settings"
