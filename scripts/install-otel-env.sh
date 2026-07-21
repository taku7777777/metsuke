#!/bin/bash
# Merge privacy-minimized native OTel settings. The operator controls when this is run.
set -euo pipefail
umask 077

settings=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}
repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/load-config.sh"
port=${METSUKE_OTEL_PORT:-4319}
case "$port" in ''|*[!0-9]*) echo "invalid METSUKE_OTEL_PORT: $port" >&2; exit 2 ;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || { echo "invalid METSUKE_OTEL_PORT: $port" >&2; exit 2; }
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || printf '{}\n' >"$settings"
python3 - "$settings" "$port" <<'PY'
import json
import os
import pathlib
import shutil
import socket
import sys

path = pathlib.Path(sys.argv[1])
port = int(sys.argv[2])
data = json.loads(path.read_text())
target = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{port}",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0",
}
env = data.setdefault("env", {})
if all(env.get(key) == value for key, value in target.items()):
    print(f"unchanged: {path}")
    raise SystemExit(0)
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    probe.bind(("127.0.0.1", port))
except OSError as exc:
    raise SystemExit(f"OTel port {port} is unavailable: {exc}") from exc
finally:
    probe.close()
shutil.copy2(path, path.with_name(path.name + ".bak-metsuke-otel"))
env.update(target)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
os.chmod(tmp, 0o600)
tmp.replace(path)
print(f"metsuke OTel env merged into {path}")
PY
