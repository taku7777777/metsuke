#!/bin/bash
# Dry-run by default. --apply removes integrations; --purge-data moves data to Trash.
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
apply=false
purge=false
git_root=${METSUKE_GIT_ROOT:-"$HOME/github"}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) apply=true; shift ;;
    --purge-data) purge=true; shift ;;
    --git-root) git_root=$2; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
settings=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}
export METSUKE_CONFIG_OVERRIDE=1
. "$repo/scripts/load-config.sh"
unset METSUKE_CONFIG_OVERRIDE
otel_port=${METSUKE_OTEL_PORT:-4319}
agents="$HOME/Library/LaunchAgents"
launchctl_bin=${METSUKE_LAUNCHCTL:-launchctl}
labels=(com.metsuke.archiver com.metsuke.tick com.metsuke.analyst com.metsuke.deadman com.metsuke.backup com.metsuke.otelcol)
data_home=${METSUKE_HOME:-"$HOME/.metsuke"}
app=${METSUKE_APPS_DIR:-"$HOME/Applications"}/Metsuke.app
# The dashboard owns exactly these derived files. Everything else under the data
# home -- above all ledger.db and archive/ -- is the user's record and survives.
dashboard_files=(
  "$data_home/state/dashboard-state.json"
  "$data_home/state/dashboard.lock"
  "$data_home/state/dashboard-secret"
  "$data_home/state/dashboard-errors.log"
  "$data_home/state/trace-cache.json"
)
trace_cache_dir="$data_home/traces"
echo "metsuke uninstall ($([ "$apply" = true ] && printf apply || printf dry-run))"
echo "  Claude settings: $settings"
echo "  LaunchAgents: ${labels[*]}"
echo "  Git root: $git_root"
echo "  macOS app: $app"
echo "  dashboard server state: ${dashboard_files[*]}"
echo "  trace cache: $trace_cache_dir"
echo "  retained: $data_home/ledger.db, $data_home/archive (never removed here)"
if [ "$apply" = false ]; then
  [ "$purge" = true ] && echo "  data: would move metsuke home to Trash"
  echo "re-run with --apply to perform these operations"
  exit 0
fi

uid_n=$(id -u)
for label in "${labels[@]}"; do
  "$launchctl_bin" bootout "gui/$uid_n/$label" 2>/dev/null || true
  plist="$agents/$label.plist"
  [ -f "$plist" ] && rm -f "$plist"
done

python3 - "$settings" "$repo" "$otel_port" <<'PY'
import json
import os
import pathlib
import shutil
import sys
import time

path, repo, otel_port = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
if not path.exists():
    raise SystemExit(0)
data = json.loads(path.read_text())
env = data.get("env") or {}
metsuke_otel = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{otel_port}",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0",
}
for key, value in metsuke_otel.items():
    if env.get(key) == value:
        env.pop(key, None)
if env:
    data["env"] = env
else:
    data.pop("env", None)
status = data.get("statusLine") or {}
if str(repo / "scripts/statusline.sh") == status.get("command"):
    data.pop("statusLine", None)
hooks = data.get("hooks") or {}
for event, groups in list(hooks.items()):
    kept_groups = []
    for group in groups:
        kept = [
            hook for hook in group.get("hooks", [])
            if str(repo / "scripts/hook-sensor.sh") not in hook.get("command", "")
        ]
        if kept:
            copy = dict(group)
            copy["hooks"] = kept
            kept_groups.append(copy)
    if kept_groups:
        hooks[event] = kept_groups
    else:
        hooks.pop(event, None)
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)
backup = path.with_name(path.name + f".bak-metsuke-uninstall-{int(time.time())}")
shutil.copy2(path, backup)
tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
os.chmod(tmp, 0o600)
tmp.replace(path)
PY

python3 - "$git_root" "$repo" <<'PY'
import pathlib
import sys

root, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
marker = "# metsuke post-commit v1"
call = f'"{repo}/scripts/git-post-commit.sh" || true'
if root.is_dir():
    for hook in root.glob("*/.git/hooks/post-commit"):
        lines = hook.read_text().splitlines()
        kept = [line for line in lines if line not in {marker, call}]
        if kept != lines:
            hook.write_text("\n".join(kept).rstrip() + "\n")
PY

if [ -x "$repo/.venv/bin/metsuke" ]; then
  "$repo/.venv/bin/metsuke" dashboard stop >/dev/null 2>&1 || true
fi
case "$app" in
  */Metsuke.app) ;;
  *) echo "refusing unsafe app path: $app" >&2; exit 1 ;;
esac
if [ -d "$app" ]; then
  rm -rf "$app"
  echo "removed: $app"
fi
for dashboard_file in "${dashboard_files[@]}"; do
  if [ -e "$dashboard_file" ]; then
    rm -f "$dashboard_file"
    echo "removed: $dashboard_file"
  fi
done
case "$trace_cache_dir" in
  */traces) ;;
  *) echo "refusing unsafe trace cache path: $trace_cache_dir" >&2; exit 1 ;;
esac
if [ -d "$trace_cache_dir" ]; then
  rm -rf "$trace_cache_dir"
  echo "removed: $trace_cache_dir"
fi

if [ "$purge" = true ]; then
  export METSUKE_CONFIG_OVERRIDE=1
  . "$repo/scripts/load-config.sh"
  unset METSUKE_CONFIG_OVERRIDE
  data_home=${METSUKE_HOME:-"$HOME/.metsuke"}
  case "$data_home" in "$HOME"|/|"") echo "refusing unsafe data home: $data_home" >&2; exit 1 ;; esac
  if [ -d "$data_home" ]; then
    trash="$HOME/.Trash/metsuke-data-$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$HOME/.Trash"
    mv "$data_home" "$trash"
    echo "data moved to $trash (recoverable from Trash)"
  fi
fi
echo "metsuke integrations removed"
