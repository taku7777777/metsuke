#!/bin/bash
# Claude statusline hot path: state cache read + throttled sensor, fail-open.
set +e
umask 077
script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
. "$script_dir/load-config.sh"
home=${METSUKE_HOME:-"$HOME/.metsuke"}
input=
sid=
sensor_sid=
ctx_pct=0
ctx_total=
ctx_kind=
ctx_scaled=
ctx_warn=${METSUKE_CONTEXT_WARN_TOKENS:-200000}
ctx_crit=${METSUKE_CONTEXT_CRIT_TOKENS:-500000}
case "$ctx_warn" in ''|*[!0-9]*) ctx_warn=200000 ;; esac
case "$ctx_crit" in ''|*[!0-9]*) ctx_crit=500000 ;; esac
sess=0
now=$(date +%s 2>/dev/null)
case "$now" in ''|*[!0-9]*) now=0 ;; esac

if command -v jq >/dev/null 2>&1; then
  parsed=$(jq -cr '
    . as $src
    | [($src.session_id // "unknown"),
       ($src.session_id // "__METSUKE_EMPTY__"),
       (($src.context_window.used_percentage // 0) | round),
       (if ($src.context_window.total_input_tokens // null) == null then "__METSUKE_EMPTY__"
        else ($src.context_window.total_input_tokens | round) end),
       ($src.cost.total_cost_usd // 0),
       (if ($src.context_window.total_input_tokens // null) == null then "__METSUKE_EMPTY__"
        elif $src.context_window.total_input_tokens < 1000 then "raw"
        elif $src.context_window.total_input_tokens < 10000 then "one_k"
        else "whole_k" end),
       (if ($src.context_window.total_input_tokens // null) == null then "__METSUKE_EMPTY__"
        else ($src.context_window.total_input_tokens / 1000) end)] | @tsv,
      ($src | tojson)' 2>/dev/null) || parsed=
  if [ -n "$parsed" ]; then
    IFS=$'\t' read -r raw_sensor_sid raw_sid ctx_pct ctx_total sess ctx_kind ctx_scaled <<<"$parsed"
    [ "$raw_sid" = __METSUKE_EMPTY__ ] && raw_sid=
    [ "$ctx_total" = __METSUKE_EMPTY__ ] && ctx_total=
    [ "$ctx_kind" = __METSUKE_EMPTY__ ] && ctx_kind=
    [ "$ctx_scaled" = __METSUKE_EMPTY__ ] && ctx_scaled=
    input=${parsed#*$'\n'}
    sensor_sid=$(printf '%s' "$raw_sensor_sid" | tr -cd 'A-Za-z0-9._-')
    sid=$(printf '%s' "$raw_sid" | tr -cd 'A-Za-z0-9._-')
  fi
fi

if [ -n "$input" ]; then
  marker="$home/state/sl-$sensor_sid.last"
  last=$(cat "$marker" 2>/dev/null || printf 0)
  last=${last%%.*}
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $((now - last)) -ge 15 ]; then
    mkdir -p "$home/state" "$home/spool/hooks" 2>/dev/null || true
    printf '%s\n' "$now" >"$marker" 2>/dev/null || true
    # Seconds precision is sufficient: the PID suffix keeps spool filenames unique.
    ns=$((now * 1000000000))
    jq -cn --argjson ts "$now" --argjson src "$input" \
      '{metsuke_event:"statusline_sample",metsuke_ts:$ts,payload:{session_id:($src.session_id // null),version:($src.version // null),cost:($src.cost // null),context_window:($src.context_window // null)}}' \
      >"$home/spool/hooks/$ns-$$-statusline_sample.ndjson" 2>/dev/null || true
  fi
fi

if [ -n "$input" ] && [ -n "$sid" ] && [ "$ctx_pct" -ge 60 ] 2>/dev/null; then
  mkdir -p "$home/state" 2>/dev/null || true
  if [ ! -e "$home/state/ctxwarned-$sid" ]; then
    printf '%s\n' "$ctx_pct" >"$home/state/ctxwarn-$sid" 2>/dev/null || true
  fi
fi

state="$home/state.json"
if [ ! -r "$state" ] || ! command -v jq >/dev/null 2>&1; then
  printf '%s\n' 'metsuke: no data yet'
  exit 0
fi
state_parsed=$(jq -r --arg sid "$sid" --argjson now "$now" \
  --arg prompt_warn "${METSUKE_PROMPT_WARN_USD:-3}" \
  --arg prompt_crit "${METSUKE_PROMPT_CRIT_USD:-7.5}" '
  def cost_color($cost; $warn; $crit):
    if $cost >= $crit then "31"
    elif $cost >= $warn then "33"
    else "__METSUKE_EMPTY__"
    end;
  . as $root
  | ($prompt_warn | tonumber? // 3) as $warn
  | ($prompt_crit | tonumber? // 7.5) as $crit
  | (.sessions[$sid] // {}) as $session
  | ($session.last_ts // null) as $last_ts
  | ($session.cache_max_expires_at // (if $last_ts == null then null else $last_ts + 3600 end)) as $cache_expiry
  | (if $cache_expiry == null then ["__METSUKE_EMPTY__", "__METSUKE_EMPTY__"]
     elif (($cache_expiry - $now) | floor) <= 0 then ["expired", "__METSUKE_EMPTY__"]
     elif (($cache_expiry - $now) | floor) <= 900 then
       ["near", ($cache_expiry | localtime | strftime("%H:%M"))]
     else ["normal", ($cache_expiry | localtime | strftime("%H:%M"))]
     end) as $ttl
  | [(.today.cost_usd // 0),
     (.today.budget_usd // "__METSUKE_EMPTY__"),
     (.today.burn_rate_usd_h // "__METSUKE_EMPTY__"),
     (if (.today.burn_rate_usd_h // null) == null then "__METSUKE_EMPTY__"
      elif .today.burn_rate_usd_h < 45 then "32"
      elif .today.burn_rate_usd_h <= 90 then "33" else "31" end),
     (.today.landing_usd // "__METSUKE_EMPTY__"),
     ((.stale // false) or ($now - (.generated_at // 0) > 900)),
     $ttl[0], $ttl[1]] | @tsv,
    "--",
    ([(if ($session.inflight_usd // null) != null then
         [($session.inflight_usd | tostring), "⏵", cost_color($session.inflight_usd; $warn; $crit), "__METSUKE_EMPTY__"] | @tsv
       else empty end)]
     + (($session.recent_prompts // [])
        | map(select((.cost_usd // null) != null)
              | [(.cost_usd | tostring),
                 (if .interrupted then "⚡" else "__METSUKE_EMPTY__" end),
                 cost_color(.cost_usd; $warn; $crit),
                 (.detail_url // "__METSUKE_EMPTY__")] | @tsv))
     | .[])
' "$state" 2>/dev/null) || { printf '%s\n' 'metsuke: no data yet'; exit 0; }
if [ -z "$state_parsed" ]; then
  # jq treats an empty/whitespace-only file as successful empty input. Keep the
  # statusline visible, but mark it stale so a broken state cache cannot hide.
  state_parsed=$(printf '0\t0\t__METSUKE_EMPTY__\t__METSUKE_EMPTY__\t__METSUKE_EMPTY__\ttrue\t__METSUKE_EMPTY__\t__METSUKE_EMPTY__\n--')
fi
state_head=${state_parsed%%$'\n'*}
prompt_rows=${state_parsed#*$'\n'--}
prompt_rows=${prompt_rows#$'\n'}
IFS=$'\t' read -r today budget burn color landing stale ttl_status expiry_text <<<"$state_head"
[ "$burn" = __METSUKE_EMPTY__ ] && burn=
[ "$budget" = __METSUKE_EMPTY__ ] && budget=
[ "$color" = __METSUKE_EMPTY__ ] && color=
[ "$landing" = __METSUKE_EMPTY__ ] && landing=
[ "$ttl_status" = __METSUKE_EMPTY__ ] && ttl_status=
[ "$expiry_text" = __METSUKE_EMPTY__ ] && expiry_text=

if [ -n "$ctx_total" ]; then
  if [ "$ctx_kind" = raw ]; then
    ctx_value=$(printf '%.0f' "$ctx_total")
  elif [ "$ctx_kind" = one_k ]; then
    ctx_value=$(printf '%.1fK' "$ctx_scaled")
  else
    ctx_value=$(printf '%.0fK' "$ctx_scaled")
  fi
else
  ctx_value=$(printf '%s%%' "$ctx_pct")
fi
if [ -n "$ctx_total" ]; then
  if [ "$ctx_total" -ge "$ctx_crit" ] 2>/dev/null; then
    ctx_text=$(printf '\033[31m%s\033[0m' "$ctx_value")
  elif [ "$ctx_total" -ge "$ctx_warn" ] 2>/dev/null; then
    ctx_text=$(printf '\033[33m%s\033[0m' "$ctx_value")
  else
    ctx_text=$ctx_value
  fi
elif [ "$ctx_pct" -ge 80 ] 2>/dev/null; then
  ctx_text=$(printf '\033[31m%s\033[0m' "$ctx_value")
elif [ "$ctx_pct" -ge 60 ] 2>/dev/null; then
  ctx_text=$(printf '\033[33m%s\033[0m' "$ctx_value")
else
  ctx_text=$ctx_value
fi
burn_text=
[ -n "$burn" ] && burn_text=$(printf ' \033[%sm$%.0f/h\033[0m' "$color" "$burn")
landing_text=
[ -n "$landing" ] && landing_text=$(printf ' 着地$%.0f' "$landing")
prompt_cost_text=
while IFS=$'\t' read -r cost prompt_marker prompt_color detail_url; do
  [ -n "$cost" ] || continue
  [ "$prompt_marker" = __METSUKE_EMPTY__ ] && prompt_marker=
  [ "$prompt_color" = __METSUKE_EMPTY__ ] && prompt_color=
  [ "$detail_url" = __METSUKE_EMPTY__ ] && detail_url=
  [ -n "$prompt_cost_text" ] && prompt_cost_text="$prompt_cost_text "
  printf -v formatted_cost '%s$%.2f' "$prompt_marker" "$cost"
  if [ -n "$prompt_color" ]; then
    printf -v colored_cost '\033[%sm%s\033[0m' "$prompt_color" "$formatted_cost"
    formatted_cost=$colored_cost
  fi
  if [ -n "$detail_url" ]; then
    printf -v linked_cost '\033]8;;%s\033\\%s\033]8;;\033\\' "$detail_url" "$formatted_cost"
    formatted_cost=$linked_cost
  fi
  prompt_cost_text="${prompt_cost_text}${formatted_cost}"
done <<<"$prompt_rows"
[ -n "$prompt_cost_text" ] && prompt_cost_text=" | $prompt_cost_text"
stale_text=
[ "$stale" = true ] && stale_text=$(printf ' | \033[31m⚠stale\033[0m')
ttl_text=
if [ "$ttl_status" = normal ]; then
  ttl_text=$(printf ' 🔥%s' "$expiry_text")
elif [ "$ttl_status" = near ]; then
  ttl_text=$(printf ' \033[31m🔥%s\033[0m' "$expiry_text")
elif [ "$ttl_status" = expired ]; then
  ttl_text=' ❄'
fi
if [ -n "$budget" ]; then
  usage_text=$(printf '$%.1f/$%.0f' "$today" "$budget")
else
  usage_text=$(printf '$%.1f' "$today")
fi
printf '⛽%s%s%s%s | sess $%.1f ctx %s%s%s\n' "$usage_text" "$burn_text" "$landing_text" "$prompt_cost_text" "$sess" "$ctx_text" "$ttl_text" "$stale_text"
exit 0
