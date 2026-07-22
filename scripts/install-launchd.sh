#!/bin/bash
# Install launchd jobs. Unchanged loaded jobs are left running; changed jobs are
# stopped completely before bootstrap so KeepAlive cleanup cannot race reload.
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "$0")/.." && pwd)"
"$REPO/scripts/install-config.sh"
export METSUKE_CONFIG_OVERRIDE=1
. "$REPO/scripts/load-config.sh"
unset METSUKE_CONFIG_OVERRIDE
METSUKE_BIN="$REPO/.venv/bin/metsuke"
LOG_DIR="${METSUKE_HOME:-$HOME/.metsuke}/logs"
OTEL_DIR="${METSUKE_HOME:-$HOME/.metsuke}/otel"
OTEL_PORT=${METSUKE_OTEL_PORT:-4319}
AGENTS="$HOME/Library/LaunchAgents"
LAUNCHCTL=${METSUKE_LAUNCHCTL:-launchctl}
POLL_SECONDS=${METSUKE_LAUNCHD_POLL_SECONDS:-0.1}
WAIT_ATTEMPTS=${METSUKE_LAUNCHD_WAIT_ATTEMPTS:-50}
BOOTSTRAP_ATTEMPTS=${METSUKE_LAUNCHD_BOOTSTRAP_ATTEMPTS:-5}

[ -x "$METSUKE_BIN" ] || { echo "missing $METSUKE_BIN — run: uv sync"; exit 1; }
mkdir -p "$LOG_DIR" "$OTEL_DIR" "$AGENTS"
UID_N=$(id -u)

service_loaded() {
  "$LAUNCHCTL" print "gui/$UID_N/$1" >/dev/null 2>&1
}

replace_if_changed() {
  local rendered=$1
  local target=$2
  if [ -f "$target" ] && cmp -s "$rendered" "$target"; then
    rm -f "$rendered"
    return 1
  fi
  mv "$rendered" "$target"
  return 0
}

wait_until_removed() {
  local label=$1
  local attempt=0
  while service_loaded "$label"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$WAIT_ATTEMPTS" ]; then
      echo "timed out waiting for launchd removal: $label" >&2
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

bootstrap_with_retry() {
  local label=$1
  local plist=$2
  local attempt=1
  while ! "$LAUNCHCTL" bootstrap "gui/$UID_N" "$plist"; do
    if [ "$attempt" -ge "$BOOTSTRAP_ATTEMPTS" ]; then
      echo "failed to bootstrap after $attempt attempts: $label" >&2
      return 1
    fi
    attempt=$((attempt + 1))
    sleep "$POLL_SECONDS"
  done
}

refresh_agent() {
  local label=$1
  local plist=$2
  local changed=$3
  if [ "$changed" = false ] && service_loaded "$label"; then
    echo "unchanged: $plist (loaded)"
    return 0
  fi
  if service_loaded "$label"; then
    "$LAUNCHCTL" bootout "gui/$UID_N/$label" 2>/dev/null || true
    wait_until_removed "$label"
  fi
  bootstrap_with_retry "$label" "$plist"
  "$LAUNCHCTL" print "gui/$UID_N/$label" | grep -E "state|program" | head -3
  echo "installed: $plist"
}

disable_agent() {
  local label=$1
  local plist=$2
  local reason=${3:-METSUKE_RESTIC_REPO is unset}
  if service_loaded "$label"; then
    "$LAUNCHCTL" bootout "gui/$UID_N/$label" 2>/dev/null || true
    wait_until_removed "$label"
  fi
  rm -f "$plist"
  echo "disabled: $label ($reason)"
}

labels=(com.metsuke.archiver com.metsuke.tick)
if [ "${METSUKE_SKIP_ANALYST:-0}" = 1 ]; then
  disable_agent com.metsuke.analyst "$AGENTS/com.metsuke.analyst.plist" "claude executable unavailable"
  disable_agent com.metsuke.deadman "$AGENTS/com.metsuke.deadman.plist" "analyst disabled"
else
  labels+=(com.metsuke.analyst com.metsuke.deadman)
fi
for label in "${labels[@]}"; do
  plist="$AGENTS/$label.plist"
  rendered="$plist.tmp.$$"
  sed -e "s|__METSUKE_BIN__|$METSUKE_BIN|g" -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__REPO__|$REPO|g" \
    "$REPO/launchd/$label.plist.template" >"$rendered"
  changed=false
  if replace_if_changed "$rendered" "$plist"; then changed=true; fi
  refresh_agent "$label" "$plist" "$changed"
done

label=com.metsuke.backup
plist="$AGENTS/$label.plist"
if [ -n "${METSUKE_RESTIC_REPO:-}" ]; then
  rendered="$plist.tmp.$$"
  sed -e "s|__METSUKE_BIN__|$METSUKE_BIN|g" -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__REPO__|$REPO|g" \
    "$REPO/launchd/$label.plist.template" >"$rendered"
  changed=false
  if replace_if_changed "$rendered" "$plist"; then changed=true; fi
  refresh_agent "$label" "$plist" "$changed"
else
  disable_agent "$label" "$plist"
fi

OTELCOL_BIN=${METSUKE_OTELCOL_BIN:-$(command -v otelcol-contrib 2>/dev/null || true)}
if [ -z "$OTELCOL_BIN" ] && [ -x /opt/homebrew/bin/otelcol-contrib ]; then
  OTELCOL_BIN=/opt/homebrew/bin/otelcol-contrib
fi
if [ -z "$OTELCOL_BIN" ]; then
  echo "skip: com.metsuke.otelcol — otelcol-contrib 未検出。GitHub Releases から取得しPATHへ配置（またはMETSUKE_OTELCOL_BINで指定）して再実行: https://github.com/open-telemetry/opentelemetry-collector-releases/releases"
else
  label=com.metsuke.otelcol
  config_path="$OTEL_DIR/otelcol.yaml"
  plist="$AGENTS/$label.plist"
  rendered_config="$config_path.tmp.$$"
  sed -e "s|__OTEL_DIR__|$OTEL_DIR|g" -e "s|__OTEL_PORT__|$OTEL_PORT|g" \
    "$REPO/otel/otelcol.yaml" >"$rendered_config"
  config_changed=false
  if replace_if_changed "$rendered_config" "$config_path"; then config_changed=true; fi
  rendered="$plist.tmp.$$"
  sed -e "s|__OTELCOL_BIN__|$OTELCOL_BIN|g" -e "s|__OTEL_CONFIG__|$config_path|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" "$REPO/launchd/$label.plist.template" >"$rendered"
  plist_changed=false
  if replace_if_changed "$rendered" "$plist"; then plist_changed=true; fi
  changed=false
  if [ "$config_changed" = true ] || [ "$plist_changed" = true ]; then changed=true; fi
  refresh_agent "$label" "$plist" "$changed"
fi
