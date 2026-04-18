#!/bin/sh
# agience-home entrypoint — fetch TLS cert from CDN, then start Caddy.
#
# Runs on every container start so monthly cert renewals take effect
# without rebuilding the image.
#
# Fallback order:
#   1. Download fresh cert from get.agience.ai
#   2. Use cached cert if download fails (offline restart)
#   3. If no cert at all: skip — Caddy won't bind 443.
#      User uses localhost service ports until connectivity is restored.

set -e

CERT_URL="${HOME_TLS_CERT_URL:-https://get.agience.ai/home/tls}"
KEYS_DIR="/run/keys"
CRT_PATH="${KEYS_DIR}/home.crt"
KEY_PATH="${KEYS_DIR}/home.key"

fetch_cert() {
    echo "[agience-home] Fetching TLS cert from ${CERT_URL}..."
    if wget -q -T 10 -O "${CRT_PATH}.tmp" "${CERT_URL}.crt" && \
       wget -q -T 10 -O "${KEY_PATH}.tmp" "${CERT_URL}.key"; then
        mv "${CRT_PATH}.tmp" "${CRT_PATH}"
        mv "${KEY_PATH}.tmp" "${KEY_PATH}"
        chmod 444 "${CRT_PATH}"
        chmod 440 "${KEY_PATH}"
        echo "[agience-home] TLS cert updated"
    else
        rm -f "${CRT_PATH}.tmp" "${KEY_PATH}.tmp"
        if [ -f "${CRT_PATH}" ] && [ -f "${KEY_PATH}" ]; then
            echo "[agience-home] WARNING: cert download failed — using cached cert"
        else
            echo "[agience-home] WARNING: no TLS cert available — HTTPS will not work"
            echo "[agience-home] Use localhost service ports until connectivity is restored"
        fi
    fi
}

fetch_cert

# Hand off to Caddy (the default caddy:alpine entrypoint)
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
