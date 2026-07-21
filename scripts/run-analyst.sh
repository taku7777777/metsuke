#!/bin/bash
# ADR 0005: the analyst receives a read-only snapshot, a single query command, and two write roots.
set -euo pipefail
umask 077

repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/load-config.sh"
home=${METSUKE_HOME:-"$HOME/.metsuke"}
week=$(python3 -c 'import datetime as d; x=d.date.today()-d.timedelta(days=7); y,w,_=x.isocalendar(); print(f"{y}-W{w:02d}")')
workdir="$home/analyst/$week"
snapshot="$workdir/snapshot.db"
reports="$home/reports"
proposals="$home/spool/proposals"
report_path="$reports/$week.md"
query="$repo/scripts/analyst-query.py"
schema="$repo/docs/SCHEMA.md"
metrics="$repo/docs/METRICS.md"
claude_bin=${METSUKE_CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}
if [ -z "$claude_bin" ]; then
  for candidate in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [ -x "$candidate" ]; then claude_bin=$candidate; break; fi
  done
fi
[ -n "$claude_bin" ] || { echo "claude executable not found" >&2; exit 1; }

mkdir -p "$workdir" "$reports" "$proposals"
python3 -c 'import sqlite3,sys; from pathlib import Path; src=Path(sys.argv[1])/"ledger.db"; dst=Path(sys.argv[2]); dst.unlink(missing_ok=True); a=sqlite3.connect(f"file:{src}?mode=ro",uri=True); b=sqlite3.connect(dst); a.backup(b); b.close(); a.close()' "$home" "$snapshot"
chmod 400 "$snapshot"

prompt=$(sed \
  -e "s|__WEEK__|$week|g" \
  -e "s|__SCHEMA__|$schema|g" \
  -e "s|__METRICS__|$metrics|g" \
  -e "s|__SNAPSHOT__|$snapshot|g" \
  -e "s|__QUERY__|$query|g" \
  -e "s|__REPORT_PATH__|$report_path|g" \
  -e "s|__PROPOSALS_DIR__|$proposals|g" \
  "$repo/claude/analyst-prompt.md")

cd "$workdir"
set +e
"$claude_bin" -p "$prompt" \
  --model claude-sonnet-5 \
  --max-turns 100 \
  --max-budget-usd 10 \
  --permission-mode dontAsk \
  --tools "Read,Write,Bash" \
  --add-dir "$repo/docs" "$workdir" "$reports" "$proposals" \
  --allowedTools "Read(/$repo/docs/**),Read(/$workdir/**),Edit(/$reports/**),Edit(/$proposals/**),Bash(python3 $repo/scripts/analyst-query.py:*)" \
  --disallowedTools "WebFetch,WebSearch,Task" &
analyst_pid=$!
(
  sleep 1200
  kill -TERM "$analyst_pid" 2>/dev/null || exit 0
  sleep 5
  kill -KILL "$analyst_pid" 2>/dev/null || true
) &
watchdog_pid=$!
wait "$analyst_pid"
rc=$?
kill "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true
set -e

if [ -f "$report_path" ]; then
  osascript -e 'display notification "週次レポート完成" with title "metsuke"' 2>/dev/null || true
else
  osascript -e 'display notification "アナリスト失敗" with title "metsuke"' 2>/dev/null || true
  [ "$rc" -eq 0 ] && rc=1  # exiting cleanly without a report is still a failure
fi
exit "$rc"
