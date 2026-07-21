#!/bin/bash
# Source the central metsuke config without evaluating its contents as shell code.
metsuke_config=${METSUKE_CONFIG:-"$HOME/.metsuke/config.env"}
[ -r "$metsuke_config" ] || return 0 2>/dev/null || exit 0
while IFS= read -r metsuke_line || [ -n "$metsuke_line" ]; do
  case "$metsuke_line" in ''|'#'*) continue ;; esac
  metsuke_key=${metsuke_line%%=*}
  metsuke_value=${metsuke_line#*=}
  case "$metsuke_key" in
    METSUKE_HOME|METSUKE_SOURCE|METSUKE_BUDGET_DAY|METSUKE_BUDGET_WEEK|METSUKE_BUDGET_MONTH|METSUKE_BUDGET_WARN_ENABLED|METSUKE_BURN_WINDOW_S|METSUKE_BURN_WARN_USD_H|METSUKE_BURN_CRIT_USD_H|METSUKE_PROMPT_WARN_USD|METSUKE_PROMPT_CRIT_USD|METSUKE_CONTEXT_WARN_TOKENS|METSUKE_CONTEXT_CRIT_TOKENS|METSUKE_RECEIPT_NOTIFY_ENABLED|METSUKE_RUNAWAY_USD|METSUKE_COLDCACHE_MIN_USD|METSUKE_TTL_PRENOTIFY_GAP_S|METSUKE_NUDGE_DAILY_CAP|METSUKE_HOURLY_VALUE_USD|METSUKE_OTEL_PORT|METSUKE_RESTIC_REPO)
      if [ "${METSUKE_CONFIG_OVERRIDE:-0}" = 1 ] || [ -z "${!metsuke_key+x}" ]; then
        export "$metsuke_key=$metsuke_value"
      fi
      ;;
  esac
done <"$metsuke_config"
unset metsuke_line metsuke_key metsuke_value metsuke_config
