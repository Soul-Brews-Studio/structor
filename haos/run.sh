#!/usr/bin/with-contenv bashio
set -euo pipefail

# Supervisor persists /data and includes it in backups; PocketBase keeps every
# byte of state under pb_data, so the two are pointed at each other.
DATA_DIR=/data/pb_data
mkdir -p "$DATA_DIR"

export STRUCTOR_ADMIN_EMAIL="$(bashio::config 'admin_email')"
export STRUCTOR_ADMIN_PASSWORD="$(bashio::config 'admin_password')"
export STRUCTOR_TZ="$(bashio::config 'tz')"
export TZ="$STRUCTOR_TZ"

# has_value, not plain config: bashio::config prints the literal string "null"
# for an unset optional, which would become a real (guessable) token.
if bashio::config.has_value 'public_url'; then
  export STRUCTOR_PUBLIC_URL="$(bashio::config 'public_url')"
  bashio::log.info "public_url ${STRUCTOR_PUBLIC_URL} — OAuth metadata will advertise it"
fi
if bashio::config.has_value 'mcp_token'; then
  export STRUCTOR_MCP_TOKEN="$(bashio::config 'mcp_token')"
  bashio::log.info "mcp_token set — /mcp accepts the static bearer"
else
  bashio::log.info "mcp_token unset — /mcp accepts OAuth or PocketBase superuser tokens only"
fi
if bashio::config.has_value 'scan_dir'; then
  export STRUCTOR_SCAN_DIR="$(bashio::config 'scan_dir')"
  if bashio::config.has_value 'scan_interval'; then
    export STRUCTOR_SCAN_INTERVAL="$(bashio::config 'scan_interval')"
  fi
  bashio::log.info "server-side scan of ${STRUCTOR_SCAN_DIR} every ${STRUCTOR_SCAN_INTERVAL:-60s}"
fi

bashio::log.info "starting Structor on :8090 (data: $DATA_DIR, tz: $STRUCTOR_TZ)"
exec /usr/local/bin/structor serve --http=0.0.0.0:8090 --dir="$DATA_DIR"
