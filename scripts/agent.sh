#!/usr/bin/env bash
# launchd entry point for the Structor processes on this Mac.
#
#   scripts/agent.sh serve            local server (bin/structor serve)
#   scripts/agent.sh watch local      structor-cli watch against the local server
#   scripts/agent.sh watch kvmlab1    structor-cli watch against kvmlab1 (5-min rescan)
#   scripts/agent.sh lance            structor-lance (Bun): LanceDB replica + admin
#   scripts/agent.sh lance-py         structor-lance (Python): the same, on its own port and data dir
#
# Credentials never go on the command line: they are read from
# ~/.config/structor/<target>.json (keys url / admin_email / admin_password /
# watch_interval) and handed to the child through the environment. A missing
# local.json falls back to the dev defaults from the Makefile. The lance and
# lance-py cases pass no credentials at all — those processes read the same
# config files themselves and pick up every target they find there.
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

# same, for a key that may hold a list: printed as one comma-separated line
conf_csv() { # conf_csv <target> <key>
  local f="$CONF_DIR/$1.json"
  [ -f "$f" ] || return 0
  python3 - "$f" "$2" <<'PY'
import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2]) or []
if isinstance(v, str):
    v = v.split(",")
print(",".join(str(x).strip() for x in v if str(x).strip()))
PY
}

# bun is installed per-user and launchd starts us with a bare PATH
find_bun() {
  local c
  for c in "$HOME/.bun/bin/bun" /opt/homebrew/bin/bun /usr/local/bin/bun; do
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  command -v bun 2>/dev/null || true
}

# uv is installed per-user too, and it owns lance-py's venv
find_uv() {
  local c
  for c in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv; do
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  command -v uv 2>/dev/null || true
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
  lance)
    BUN="$(find_bun)"
    if [ -z "$BUN" ]; then
      echo "agent.sh: bun not found (looked in ~/.bun/bin, /opt/homebrew/bin, /usr/local/bin, then PATH) — install bun, then 'make lance-install'" >&2
      exit 78   # EX_CONFIG; launchd throttles the restart instead of spinning
    fi
    export STRUCTOR_LANCE_HTTP="${STRUCTOR_LANCE_HTTP:-127.0.0.1:8092}"
    export STRUCTOR_LANCE_DATA="${STRUCTOR_LANCE_DATA:-$APP_DIR/lance_data}"
    # optional: ~/.config/structor/lance.json {"targets": ["local", "kvmlab1"]}
    # narrows which stores get replicated; absent means every configured target
    TARGETS="$(conf_csv lance targets)"
    if [ -n "$TARGETS" ]; then export STRUCTOR_LANCE_TARGETS="$TARGETS"; fi
    exec "$BUN" "$APP_DIR/lance/src/main.ts"
    ;;
  lance-py)
    UV="$(find_uv)"
    if [ -z "$UV" ]; then
      echo "agent.sh: uv not found (looked in ~/.local/bin, /opt/homebrew/bin, then PATH) — install uv, then 'make lance-py-install'" >&2
      exit 78   # EX_CONFIG; launchd throttles the restart instead of spinning
    fi
    export STRUCTOR_LANCE_PY_HTTP="${STRUCTOR_LANCE_PY_HTTP:-127.0.0.1:8094}"
    export STRUCTOR_LANCE_PY_DATA="${STRUCTOR_LANCE_PY_DATA:-$APP_DIR/lance_data_py}"
    # same optional ~/.config/structor/lance.json {"targets": [...]} as the Bun edition
    TARGETS="$(conf_csv lance targets)"
    if [ -n "$TARGETS" ]; then export STRUCTOR_LANCE_TARGETS="$TARGETS"; fi
    exec "$UV" run --project "$APP_DIR/lance-py" structor-lance serve
    ;;
  *)
    echo "usage: $0 serve | watch <target> | lance | lance-py" >&2
    exit 64
    ;;
esac
