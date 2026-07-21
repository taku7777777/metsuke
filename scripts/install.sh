#!/bin/bash
# Unified installer for the checkout-based macOS deployment.
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
git_root=${METSUKE_GIT_ROOT:-"$HOME/github"}
with_git=false
skip_launchd=false
skip_claude_hooks=false
skip_statusline=false
skip_otel=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --git-root) git_root=$2; shift 2 ;;
    --with-git-hooks) with_git=true; shift ;;
    --skip-git) with_git=false; shift ;;
    --skip-launchd) skip_launchd=true; shift ;;
    --skip-claude-hooks) skip_claude_hooks=true; shift ;;
    --skip-statusline) skip_statusline=true; shift ;;
    --skip-otel) skip_otel=true; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

settings=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}
config_path=${METSUKE_CONFIG:-"$HOME/.metsuke/config.env"}
claude_commands=$(dirname "$settings")/commands/handoff.md
echo "metsuke installer plan (supported OS: macOS)"
echo "  central config: $config_path (create only; no backup needed)"
echo "  Claude hooks: $([ "$skip_claude_hooks" = false ] && printf '%s' "$settings" || printf skipped)"
echo "  Claude handoff command: $([ "$skip_claude_hooks" = false ] && printf '%s (managed file; no backup)' "$claude_commands" || printf skipped)"
echo "  Claude statusline: $([ "$skip_statusline" = false ] && printf '%s' "$settings" || printf skipped)"
if [ "$skip_claude_hooks" = false ] || [ "$skip_statusline" = false ]; then
  echo "  Claude settings backup: $settings.bak-metsuke-stage3"
fi
if [ "$skip_otel" = false ]; then
  echo "  Claude OTel env: $settings (backup: $settings.bak-metsuke-otel)"
else
  echo "  Claude OTel env: skipped"
fi
if [ "$with_git" = true ]; then
  echo "  Git post-commit hooks: new hooks under $git_root only (existing hooks are refused)"
else
  echo "  Git post-commit hooks: skipped (opt in with --with-git-hooks)"
fi
if [ "$skip_launchd" = false ]; then
  echo "  launchd: $HOME/Library/LaunchAgents/com.metsuke.*.plist (generated files; no backup)"
else
  echo "  launchd: skipped"
fi

if [ "${METSUKE_SKIP_PREREQ_CHECK:-0}" != 1 ]; then
  [ "$(uname -s)" = Darwin ] || { echo "unsupported OS: metsuke installer supports macOS only" >&2; exit 1; }
  missing=()
  for command_name in uv jq python3; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
  done
  [ "${#missing[@]}" -eq 0 ] || { echo "missing required commands: ${missing[*]}" >&2; exit 1; }
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
    echo "Python 3.12 or newer is required" >&2
    exit 1
  }
  [ -x "$repo/.venv/bin/metsuke" ] || {
    echo "missing $repo/.venv/bin/metsuke; run: uv sync --frozen" >&2
    exit 1
  }
fi

"$repo/scripts/install-config.sh"
skipped=()
claude_args=()
if [ "$skip_claude_hooks" = true ]; then
  claude_args+=(--skip-hooks)
  skipped+=("Claude hooks (--skip-claude-hooks)")
fi
if [ "$skip_statusline" = true ]; then
  claude_args+=(--skip-statusline)
  skipped+=("Claude statusline (--skip-statusline)")
fi
if [ "$skip_claude_hooks" = false ] || [ "$skip_statusline" = false ]; then
  if [ "${#claude_args[@]}" -gt 0 ]; then
    "$repo/scripts/install-claude-hooks.sh" "${claude_args[@]}"
  else
    "$repo/scripts/install-claude-hooks.sh"
  fi
fi
if [ "$skip_otel" = false ]; then
  "$repo/scripts/install-otel-env.sh"
else
  skipped+=("OTel export (--skip-otel)")
fi
if [ "$with_git" = true ]; then
  "$repo/scripts/install-git-hooks.sh" "$git_root"
else
  skipped+=("Git hooks (not opted in)")
fi
if [ "$skip_launchd" = false ]; then
  claude_bin=${METSUKE_CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}
  if [ -z "$claude_bin" ]; then
    for candidate in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
      if [ -x "$candidate" ]; then claude_bin=$candidate; break; fi
    done
  fi
  if [ -z "$claude_bin" ]; then
    export METSUKE_SKIP_ANALYST=1
    skipped+=("weekly analyst/deadman (claude executable not found)")
  fi
  "$repo/scripts/install-launchd.sh"
  if ! command -v otelcol-contrib >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/otelcol-contrib ]; then
    skipped+=("OTel collector (otelcol-contrib not found)")
  fi
else
  skipped+=("launchd jobs (--skip-launchd)")
fi
echo "installation refreshed"
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "Skipped optional features:"
  for reason in "${skipped[@]}"; do echo "  - $reason"; done
fi
"$repo/.venv/bin/metsuke" config
"$repo/.venv/bin/metsuke" prices
"$repo/.venv/bin/metsuke" doctor || true
