#!/bin/bash
# Claude hook hot path: fail-open, bash+jq, spool/state only.
set +e
umask 077
event=${1:-Unknown}
script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
. "$script_dir/load-config.sh"
home=${METSUKE_HOME:-"$HOME/.metsuke"}

if [ "$event" = PostToolUse ]; then
  command -v jq >/dev/null 2>&1 || exit 0
  now=$(jq -n 'now')
  marker="$home/state/sync-trigger.last"
  last=$(cat "$marker" 2>/dev/null || printf 0)
  due=$(jq -n --argjson n "$now" --argjson l "${last:-0}" '$n-$l >= 30' 2>/dev/null)
  if [ "$due" = true ]; then
    mkdir -p "$home/state" 2>/dev/null || true
    printf '%s\n' "$now" >"$marker" 2>/dev/null || true
    nohup "$script_dir/../.venv/bin/metsuke" sync --quiet </dev/null >/dev/null 2>&1 &
  fi
  exit 0
fi

command -v jq >/dev/null 2>&1 || exit 0
active_task=
if [ "$event" = UserPromptSubmit ] && [ -r "$home/state/active-task" ]; then
  active_task=$(tr -cd 'A-Za-z0-9._-' <"$home/state/active-task" 2>/dev/null)
fi
input=$(jq -c --arg task "$active_task" \
  'if $task == "" then . else . + {metsuke_task_id:$task} end' 2>/dev/null) || input=
[ -n "$input" ] || exit 0
now=$(jq -n 'now')
ns=$(jq -n 'now * 1000000000 | floor')
dir="$home/spool/hooks"
mkdir -p "$dir" 2>/dev/null || true
spool_tmp="$dir/.tmp-$ns-$$-$event"
spool_final="$dir/$ns-$$-$event.ndjson"
if jq -cn --arg event "$event" --argjson ts "$now" --argjson payload "$input" \
  '{metsuke_event:$event,metsuke_ts:$ts,payload:$payload}' \
  >"$spool_tmp" 2>/dev/null; then
  mv "$spool_tmp" "$spool_final" 2>/dev/null || true
else
  rm -f "$spool_tmp" 2>/dev/null || true
fi

if [ "$event" = Stop ]; then
  nohup "$script_dir/../.venv/bin/metsuke" sync --quiet </dev/null >/dev/null 2>&1 &
  exit 0
fi
if [ "$event" = PostCompact ]; then
  sid=$(jq -r '.session_id // ""' <<<"$input" | tr -cd 'A-Za-z0-9._-')
  if [ -n "$sid" ]; then
    mkdir -p "$home/state" 2>/dev/null || true
    printf '%s\n' "$now" >"$home/state/compacted-$sid" 2>/dev/null || true
    rm -f "$home/state/ctxwarn-$sid" "$home/state/ctxwarned-$sid" 2>/dev/null || true
  fi
  exit 0
fi
[ "$event" = UserPromptSubmit ] || exit 0
mkdir -p "$home/state" 2>/dev/null || true

sid=$(jq -r '.session_id // ""' <<<"$input" | tr -cd 'A-Za-z0-9._-')
ctxwarn_marker="$home/state/ctxwarn-$sid"
compacted_marker="$home/state/compacted-$sid"
ctxwarn_pending=false
compacted_pending=false
ctxwarn_pct=
[ -n "$sid" ] && [ -e "$ctxwarn_marker" ] && { ctxwarn_pending=true; ctxwarn_pct=$(cat "$ctxwarn_marker" 2>/dev/null); }
[ -n "$sid" ] && [ -e "$compacted_marker" ] && compacted_pending=true

state="$home/state.json"
fresh=false
if [ -r "$state" ]; then
  generated=$(jq -r '.generated_at // 0' "$state" 2>/dev/null)
  fresh=$(jq -n --argjson n "$now" --argjson g "${generated:-0}" '$n-$g <= 900' 2>/dev/null)
fi
date=$(date +%F)
messages=
additional_context=

fire_nudge() {
  rule=$1
  detail=$2
  n=$(jq -n 'now * 1000000000 | floor')
  jq -cn --argjson ts "$now" --arg rule "$rule" --arg sid "$sid" --argjson detail "$detail" \
    '{metsuke_event:"nudge_fired",metsuke_ts:$ts,payload:{rule:$rule,session_id:$sid,detail:$detail}}' \
    >"$dir/$n-$$-nudge-$rule.ndjson" 2>/dev/null || true
}
append_message() {
  if [ -n "$messages" ]; then messages="$messages
$1"; else messages=$1; fi
}

session=
[ "$fresh" = true ] && session=$(jq -c --arg sid "$sid" '.sessions[$sid] // empty' "$state" 2>/dev/null)
if [ "$fresh" = true ] && [ -n "$session" ]; then
  last_ts=$(jq -r '.last_ts // 0' <<<"$session")
  rebuild=$(jq -r '.rebuild_cost_usd // 0' <<<"$session")
  rebuild_low=$(jq -r '.rebuild_cost_low_usd // .rebuild_cost_usd // 0' <<<"$session")
  rebuild_high=$(jq -r '.rebuild_cost_high_usd // .rebuild_cost_usd // 0' <<<"$session")
  cache_expiry=$(jq -r '.cache_max_expires_at // (.last_ts + 3600) // 0' <<<"$session")
  coldcache_min=$(jq -r '.thresholds.coldcache_min_usd // 0.5' "$state" 2>/dev/null)
  cold=$(jq -n --argjson n "$now" --argjson e "$cache_expiry" --argjson r "$rebuild" \
    --argjson m "${coldcache_min:-0.5}" '$e > 0 and $n > $e and $r >= $m')
  cold_marker="$home/state/nudge-coldcache-$sid-${last_ts%.*}"
  cap="$home/state/nudge-cap-coldcache-$date"
  count=$(cat "$cap" 2>/dev/null || printf 0)
  case $count in ''|*[!0-9]*) count=0;; esac
  if [ "$cold" = true ] && [ ! -e "$cold_marker" ] && [ "$count" -lt 3 ]; then
    mkdir -p "$home/state" 2>/dev/null || true
    : >"$cold_marker" 2>/dev/null
    printf '%s\n' "$((count + 1))" >"$cap" 2>/dev/null
    text=$(printf '🧊 観測できたキャッシュ期限は失効済み — 再構築推定 $%.2f–$%.2f。文脈維持が必要なら同じセッションを続行し、タスク境界なら /handoff を検討してください（この送信自体の費用は不可避）' "$rebuild_low" "$rebuild_high")
    append_message "$text"
    detail=$(jq -cn --argjson gap "$(jq -n --argjson n "$now" --argjson l "$last_ts" '$n-$l')" --argjson rebuild "$rebuild" '{gap_s:$gap,rebuild_cost_usd:$rebuild}')
    fire_nudge coldcache_warn "$detail"
  fi
fi

if [ "$fresh" = true ] && [ "${METSUKE_BUDGET_WARN_ENABLED:-0}" = 1 ]; then
cost=$(jq -r '.today.cost_usd // 0' "$state")
budget=$(jq -r '.today.budget_usd // 0' "$state")
pct=$(jq -n --argjson c "$cost" --argjson b "$budget" 'if $b>0 then $c/$b else 0 end')
detail=$(jq -cn --argjson cost "$cost" --argjson pct "$pct" '{cost_usd:$cost,pct:$pct}')
if [ "$(jq -n --argjson p "$pct" '$p>=1')" = true ]; then
  marker="$home/state/nudge-cap-budget100-$date"
  if [ ! -e "$marker" ]; then
    : >"$marker" 2>/dev/null
    append_message "$(printf '⛔ API換算の日次目安100%%到達（$%.2f/$%.0f）。送信は停止しません。継続価値を確認し、区切れるなら /handoff または明日に回してください' "$cost" "$budget")"
    fire_nudge budget_warn_100 "$detail"
  fi
elif [ "$(jq -n --argjson p "$pct" '$p>=0.8')" = true ]; then
  marker="$home/state/nudge-cap-budget80-$date"
  if [ ! -e "$marker" ]; then
    : >"$marker" 2>/dev/null
    append_message "$(printf '⚠️ 日次予算80%%超（$%.2f/$%.0f）。重い依頼は明日へ、または /handoff で軽量続行を' "$cost" "$budget")"
    fire_nudge budget_warn_80 "$detail"
  fi
elif [ "$(jq -n --argjson p "$pct" '$p>=0.5')" = true ]; then
  marker="$home/state/nudge-cap-budget50-$date"
  if [ ! -e "$marker" ]; then
    : >"$marker" 2>/dev/null
    landing=$(jq -r '.today.landing_usd // empty' "$state")
    if [ -n "$landing" ]; then text=$(printf '日次予算50%%通過（$%.2f/$%.0f・着地見込み$%.2f）' "$cost" "$budget" "$landing"); else text=$(printf '日次予算50%%通過（$%.2f/$%.0f）' "$cost" "$budget"); fi
    append_message "$text"
    fire_nudge budget_warn_50 "$detail"
  fi
fi
fi

if [ "$ctxwarn_pending" = true ]; then
    rm -f "$ctxwarn_marker" 2>/dev/null || true
    touch "$home/state/ctxwarned-$sid" 2>/dev/null || true
    cap="$home/state/nudge-cap-ctxwarn-$date"
    count=$(cat "$cap" 2>/dev/null || printf 0)
    case $count in ''|*[!0-9]*) count=0;; esac
    if [ "$count" -lt 3 ]; then
      printf '%s\n' "$((count + 1))" >"$cap" 2>/dev/null || true
      append_message "$(printf '📚 context %s%%消費 — auto-compact接近。区切りの良いところで /handoff が安価です（圧縮は要約+再読で高くつき、却下案・制約などの判断文脈も失われます）' "$ctxwarn_pct")"
      if jq -ne --arg value "$ctxwarn_pct" '$value | tonumber' >/dev/null 2>&1; then
        detail=$(jq -cn --argjson pct "$ctxwarn_pct" '{ctx_pct:$pct}')
      else
        detail='{"ctx_pct":null}'
      fi
      fire_nudge ctx_warn "$detail"
    fi
fi
if [ "$compacted_pending" = true ]; then
    rm -f "$compacted_marker" 2>/dev/null || true
    additional_context='[COMPACT RECOVERY] 直前にコンテキスト圧縮が発生した。作業再開前に:
- 圧縮サマリーは「過去の作業ログ」であり「次の行動指示」ではない。サマリー由来の next step は仮説として扱い、ユーザー指示・plan・TaskList を正とする
- サマリー中で言及された案には却下済みのものが含まれうる。却下案を再提案・再実行しない
- 検証→実装などフェーズ前提を再確認し、破壊的操作(デプロイ・上書き・削除)の前に前提を確かめる
- TaskList・plan ファイル・編集中ファイルを読み直してから続行する'
    fire_nudge compact_recovery '{}'
fi
if [ -n "$messages" ] || [ -n "$additional_context" ]; then
  jq -cn --arg message "$messages" --arg ctx "$additional_context" \
    '({} + (if $message != "" then {systemMessage:$message} else {} end) + (if $ctx != "" then {hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}} else {} end))'
fi
exit 0
