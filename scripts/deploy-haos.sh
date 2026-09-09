#!/usr/bin/env bash
# Deploy the Structor add-on to a Home Assistant OS box as a LOCAL add-on.
#
#   scripts/deploy-haos.sh [guest] [slug]      default: kvmlab1 structor
#
# Recipe for a Home Assistant OS "local add-on":
#   rsync to /addons/<slug>/  →  `ha store reload`  →  install or rebuild.
# Supervisor accepts exactly ONE of update/rebuild; bumping version without a
# rebuild leaves the old image running, so this script always rebuilds.
set -euo pipefail

GUEST="${1:-kvmlab1}"
SLUG="${2:-structor}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/haos"

for arch in amd64 aarch64; do
  [ -x "$SRC/bin/structor-$arch" ] || { echo "missing $SRC/bin/structor-$arch — run 'make linux' first" >&2; exit 1; }
done

echo "→ rsync $SRC/ → $GUEST:/addons/$SLUG/"
ssh "$GUEST" "mkdir -p /addons/$SLUG"
rsync -a --delete --exclude '.DS_Store' "$SRC/" "$GUEST:/addons/$SLUG/"
ssh "$GUEST" "chmod 0755 /addons/$SLUG/run.sh /addons/$SLUG/bin/*"

echo "→ ha store reload"
ssh "$GUEST" "ha store reload >/dev/null"

# `ha apps info` succeeds for any add-on in the store, installed or not; the
# installed version is null until it is actually installed.
STATE=$(ssh "$GUEST" "ha apps info local_$SLUG --raw-json 2>/dev/null" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]; print("installed" if d.get("version") else "absent")
except Exception: print("absent")')

if [ "$STATE" = "installed" ]; then
  echo "→ installed already: rebuild"
  ssh "$GUEST" "ha apps rebuild local_$SLUG"
else
  echo "→ install local_$SLUG"
  ssh "$GUEST" "ha apps install local_$SLUG"
fi

# Options: the ha CLI has no flag for them; the Supervisor API from inside the
# SSH add-on does. A creds file on the deploying machine keeps them out of git.
CREDS="$HOME/.config/structor/$GUEST.json"
if [ -f "$CREDS" ]; then
  echo "→ options from $CREDS"
  OPTS=$(python3 -c 'import json,sys
c=json.load(open(sys.argv[1]))
o={"admin_email":c.get("admin_email","admin@structor.local"),"admin_password":c["admin_password"],"tz":c.get("tz","Asia/Bangkok")}
for k in ("mcp_token","public_url","scan_dir","scan_interval"):
    if c.get(k): o[k]=c[k]
print(json.dumps({"options":o}))' "$CREDS")
  ssh "$GUEST" "curl -s -X POST -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" -H 'Content-Type: application/json' http://supervisor/addons/local_$SLUG/options -d '$OPTS'" ; echo
else
  echo "→ no $CREDS — set admin_password in the HA add-on UI before starting"
fi

echo "→ start"
ssh "$GUEST" "ha apps start local_$SLUG || true"
sleep 3
ssh "$GUEST" "ha apps info local_$SLUG --raw-json" | python3 -c 'import json,sys;d=json.load(sys.stdin)["data"];print("state:",d.get("state"),"version:",d.get("version"),"ingress:",d.get("ingress_url"))'
# the add-on's reachable hostname; defaults to the ssh alias, override when they differ (a mesh FQDN, an IP)
HOST="${STRUCTOR_GUEST_HOST:-$GUEST}"
echo "health: $(curl -s -m 8 "http://$HOST:8090/api/health" || echo unreachable)"
