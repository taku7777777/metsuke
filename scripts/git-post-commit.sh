#!/bin/bash
# Git post-commit sensor: fail-open and append one minimal fact envelope to spool.
set +e
umask 077
script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
. "$script_dir/load-config.sh"
command -v git >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0
toplevel=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
sha=$(git rev-parse HEAD 2>/dev/null) || exit 0
home=${METSUKE_HOME:-"$HOME/.metsuke"}
repo=${toplevel##*/}
branch=$(git symbolic-ref --short -q HEAD 2>/dev/null || printf HEAD)
subject=$(git log -1 --format=%s 2>/dev/null) || exit 0
body=$(git log -1 --format=%B 2>/dev/null)
body=${body:0:1000}
now=$(jq -n 'now')
ns=$(jq -n 'now * 1000000000 | floor')
stats=$(git diff-tree --no-commit-id --numstat -r --root HEAD 2>/dev/null)
insertions=0
deletions=0
while IFS=$'\t' read -r added removed _path; do
  case $added in ''|*[!0-9]*) added=0;; esac
  case $removed in ''|*[!0-9]*) removed=0;; esac
  insertions=$((insertions + added))
  deletions=$((deletions + removed))
done <<<"$stats"
files=$(git diff-tree --no-commit-id --name-only -r --root HEAD 2>/dev/null | jq -R -s 'split("\n") | map(select(length>0)) | .[:100]')
dir="$home/spool/hooks"
mkdir -p "$dir" 2>/dev/null || exit 0
jq -cn \
  --argjson ts "$now" --arg repo_path "$toplevel" --arg repo "$repo" \
  --arg branch "$branch" --arg sha "$sha" --arg subject "$subject" --arg body "$body" \
  --argjson insertions "${insertions:-0}" --argjson deletions "${deletions:-0}" \
  --argjson files "$files" --arg cwd "$PWD" \
  '{metsuke_event:"git_commit",metsuke_ts:$ts,payload:{repo_path:$repo_path,repo:$repo,branch:$branch,sha:$sha,subject:$subject,body:$body,insertions:$insertions,deletions:$deletions,files:$files,cwd:$cwd}}' \
  >"$dir/$ns-$$-git_commit.ndjson" 2>/dev/null || true
chmod 600 "$dir/$ns-$$-git_commit.ndjson" 2>/dev/null || true
exit 0
