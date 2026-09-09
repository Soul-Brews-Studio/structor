#!/usr/bin/env bash
# launchd entry point for the Structor processes on this Mac.
#
#   scripts/agent.sh serve            local server (bin/structor serve)
#   scripts/agent.sh watch local      structor-cli watch against the local server
#   scripts/agent.sh watch kvmlab1    structor-cli watch against kvmlab1 (5-min rescan)
#
# Credentials never go on the command line: they are read from
# ~/.config/structor/<target>.json (keys url / admin_email / admin_password /
# watch_interval) and handed to the child through the environment. A missing
# local.json falls back to the dev defaults from the Makefile.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONF_DIR="$HOME/.config/structor"
LOG_DIR="$HOME/Library/Logs/Structor"
mkdir -p "$LOG_DIR"

# read one key from ~/.config/structor/<target>.json, empty when absent
conf() { # conf <target> <key>
  local f="$CONF_DIR/$1.json"
  [ -f "$f" ] || return 0
  python3 - "$f" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get(sys.argv[2], "")
print(v if v is not None else "")
PY
}

case "${1:-}" in
  serve)
    HTTP="$(conf local http)";  HTTP="${HTTP:-127.0.0.1:8091}"
    EMAIL="$(conf local admin_email)"; EMAIL="${EMAIL:-admin@structor.local}"
    PASS="$(conf local admin_password)"; PASS="${PASS:-structor-dev-password}"
    DATA="$(conf local data_dir)"; DATA="${DATA:-$APP_DIR/pb_data}"
    export STRUCTOR_ADMIN_EMAIL="$EMAIL" STRUCTOR_ADMIN_PASSWORD="$PASS"
    export STRUCTOR_TZ="${STRUCTOR_TZ:-Asia/Bangkok}"
    exec "$APP_DIR/bin/structor" serve --http="$HTTP" --dir="$DATA"
    ;;
  watch)
    T="${2:-local}"
    URL="$(conf "$T" url)"
    EMAIL="$(conf "$T" admin_email)"
    PASS="$(conf "$T" admin_password)"
    INTERVAL="$(conf "$T" watch_interval)"
    if [ "$T" = "local" ]; then
      URL="${URL:-http://127.0.0.1:8091}"; EMAIL="${EMAIL:-admin@structor.local}"; PASS="${PASS:-structor-dev-password}"
    fi
    if [ -z "$URL" ] || [ -z "$EMAIL" ] || [ -z "$PASS" ]; then
      echo "agent.sh: $CONF_DIR/$T.json needs url, admin_email, admin_password" >&2
      exit 78   # EX_CONFIG; launchd throttles the restart instead of spinning
    fi
    export STRUCTOR_URL="$URL" STRUCTOR_EMAIL="$EMAIL" STRUCTOR_PASSWORD="$PASS"
    exec "$APP_DIR/bin/structor-cli" watch --interval "${INTERVAL:-120}"
    ;;
  *)
    echo "usage: $0 serve | watch <target>" >&2
    exit 64
    ;;
esac
