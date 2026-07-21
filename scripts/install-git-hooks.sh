#!/bin/bash
# Install the metsuke post-commit sensor into repositories under one root.
set -euo pipefail
umask 077

repo_abs=$(cd "$(dirname "$0")/.." && pwd)
root=${1:-"$HOME/github"}
marker="# metsuke post-commit v1"
call="\"$repo_abs/scripts/git-post-commit.sh\" || true"

for git_dir in "$root"/*/.git; do
  [ -d "$git_dir" ] || continue
  hook="$git_dir/hooks/post-commit"
  if [ -f "$hook" ] && grep -qF "$marker" "$hook"; then
    echo "skip: $hook"
    continue
  fi
  if [ -e "$hook" ]; then
    echo "refused: existing hook left unchanged: $hook" >&2
    continue
  fi
  printf '%s\n%s\n%s\n' '#!/bin/bash' "$marker" "$call" >"$hook"
  chmod +x "$hook"
  echo "installed: $hook"
done
