#!/usr/bin/env bash
# packaging/install/home/bootstrap-cert.sh
#
# One-time setup: issues the initial TLS cert for home.agience.ai and
# publishes it to the Agience/get.agience.ai GitHub repository so it is
# served publicly at https://get.agience.ai/home/tls.{crt,key}.
#
# Prerequisites:
#   - acme.sh installed (https://github.com/acmesh-official/acme.sh)
#   - gh CLI installed and authenticated (gh auth login)
#     Token must have write access to Agience/get.agience.ai
#   - EasyDNS API token and key (Account → API Access)
#
# Usage:
#   export EASYDNS_Token=your_easydns_token
#   export EASYDNS_Key=your_easydns_key
#   bash packaging/install/home/bootstrap-cert.sh

set -euo pipefail

REPO="Agience/get.agience.ai"

if [[ -z "${EASYDNS_Token:-}" ]]; then
  echo "ERROR: EASYDNS_Token env var is required"
  exit 1
fi
if [[ -z "${EASYDNS_Key:-}" ]]; then
  echo "ERROR: EASYDNS_Key env var is required"
  exit 1
fi

echo "==> Issuing certificate for home.agience.ai via DNS-01 (EasyDNS)..."
~/.acme.sh/acme.sh \
  --issue \
  --dns dns_easydns \
  -d home.agience.ai \
  --server letsencrypt \
  --force \
  --email ops@agience.ai \
  --fullchainpath /tmp/home.crt \
  --keypath /tmp/home.key

echo "==> Publishing cert to $REPO..."

upload_file() {
  local path=$1
  local file=$2
  local msg=$3

  CONTENT=$(base64 -w0 "$file")
  EXISTING_SHA=$(gh api "repos/$REPO/contents/$path" --jq '.sha' 2>/dev/null || true)

  if [ -n "$EXISTING_SHA" ]; then
    gh api --method PUT "repos/$REPO/contents/$path" \
      --field message="$msg" \
      --field content="$CONTENT" \
      --field sha="$EXISTING_SHA" \
      --silent
  else
    gh api --method PUT "repos/$REPO/contents/$path" \
      --field message="$msg" \
      --field content="$CONTENT" \
      --silent
  fi
  echo "  Published $path"
}

upload_file "home/tls.crt" "/tmp/home.crt" "chore: initial home.agience.ai cert"
upload_file "home/tls.key" "/tmp/home.key" "chore: initial home.agience.ai key"

echo "==> Cleaning up temporary cert files..."
rm -f /tmp/home.crt /tmp/home.key

echo ""
echo "Done. Cert is live at https://get.agience.ai/home/tls.{crt,key}"
echo ""
echo "Next steps:"
echo "  1. Add EASYDNS_TOKEN, EASYDNS_KEY, and DEPLOY_TOKEN_MY as GitHub secrets"
echo "     in Agience/agience-core for automated monthly renewal."
echo "  2. The renew-cert workflow runs on the 1st of each month automatically."
