#!/usr/bin/env bash
# Install (or remove) the launchd agents that keep Structor running on this Mac:
# the local server, the two watchers, the LanceDB replica, and the menu-bar tray.
#
#   scripts/install-agents.sh                 install all five
#   scripts/install-agents.sh serve tray      install a subset
#   scripts/install-agents.sh --uninstall     bootout + remove all five
#
# Templates live in launchd/; @APP_DIR@ and @HOME@ are filled in at install
# time so the repo copy stays machine-neutral. Any hand-started copy of the
# same process is stopped first so exactly one instance runs afterwards.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/Structor"
DOMAIN="gui/$(id -u)"
ALL="serve watch-local watch-kvmlab1 lance tray"
mkdir -p "$LA" "$LOGS"

label() { echo "studio.soulbrews.structor.$1"; }

# the hand-started (nohup) process each agent replaces
stop_manual() {
  case "$1" in
    serve)         pkill -f "bin/structor serve" 2>/dev/null || true ;;
    watch-local)   pkill -f "structor-cli --url http://127.0.0.1:8091 .* watch" 2>/dev/null || true ;;
    watch-kvmlab1) pkill -f "structor-cli --url http://kvmlab1[^ ]* .* watch" 2>/dev/null || true ;;
    # a hand-started replica may have been launched as "bun src/main.ts" from
    # inside lance/, which no path pattern can tell apart from other Bun apps
    # (this Mac runs a dozen unrelated "src/main.ts" servers) — so stop whoever
    # listens on the admin port, plus any copy started by full path
    lance)         port="${STRUCTOR_LANCE_HTTP:-127.0.0.1:8092}"; port="${port##*:}"
                   lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null || true
                   pkill -f "$APP_DIR/lance/src/main.ts" 2>/dev/null || true ;;
    tray)          pkill -x StructorTray 2>/dev/null || true ;;
  esac
}

if [ "${1:-}" = "--uninstall" ]; then
  for a in $ALL; do
    l="$(label "$a")"
    launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
    rm -f "$LA/$l.plist"
    echo "removed $l"
  done
  exit 0
fi

AGENTS="${*:-$ALL}"
for a in $AGENTS; do
  l="$(label "$a")"
  src="$APP_DIR/launchd/$l.plist"
  [ -f "$src" ] || { echo "no template for '$a' ($src)" >&2; exit 64; }
  if [ "$a" = "tray" ] && [ ! -d /Applications/StructorTray.app ]; then
    echo "tray: /Applications/StructorTray.app missing — run 'make install-tray' first" >&2; exit 64
  fi
  if [ "$a" = "watch-kvmlab1" ] && [ ! -f "$HOME/.config/structor/kvmlab1.json" ]; then
    echo "watch-kvmlab1: ~/.config/structor/kvmlab1.json missing — skipping" >&2; continue
  fi
  if [ "$a" = "lance" ]; then
    if ! { [ -x "$HOME/.bun/bin/bun" ] || [ -x /opt/homebrew/bin/bun ] || [ -x /usr/local/bin/bun ] || command -v bun >/dev/null 2>&1; }; then
      echo "lance: bun not installed — skipping" >&2; continue
    fi
    if [ ! -d "$APP_DIR/lance/node_modules" ]; then
      echo "lance: lance/node_modules missing — run 'make lance-install' first, skipping" >&2; continue
    fi
  fi
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$HOME|g" "$src" > "$LA/$l.plist"
  plutil -lint -s "$LA/$l.plist"
  launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
  stop_manual "$a"
  sleep 1
  launchctl bootstrap "$DOMAIN" "$LA/$l.plist"
done

sleep 3
for a in $AGENTS; do
  l="$(label "$a")"
  state="$(launchctl print "$DOMAIN/$l" 2>/dev/null | awk -F' = ' '/^\tstate =/{s=$2} /^\tpid =/{p=$2} END{print s " pid=" p}')"
  echo "$l: ${state:-not loaded}"
done
