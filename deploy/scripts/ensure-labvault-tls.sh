#!/usr/bin/env bash
# Create a lab self-signed cert if the operator has not installed one.
# Default public TLS port is 9443 (unused in this SKU; 443/8000/18000 stay free).
set -euo pipefail

TLS_DIR="${LABVAULT_TLS_DIR:-/var/lib/labvault/tls}"
CERT="${LABVAULT_TLS_CERT:-${LABVAULT_SSL_CERT:-$TLS_DIR/fullchain.pem}}"
KEY="${LABVAULT_TLS_KEY:-${LABVAULT_SSL_KEY:-$TLS_DIR/privkey.pem}}"
CN="${LABVAULT_PUBLIC_HOSTNAME:-$(hostname -f 2>/dev/null || hostname || echo labvault)}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

mkdir -p "$(dirname "$CERT")" "$(dirname "$KEY")" "$TLS_DIR"
chmod 755 "$TLS_DIR"

if [[ -f "$CERT" && -f "$KEY" ]]; then
  echo "tls_existing cert=$CERT key=$KEY"
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl required to generate LABVAULT_TLS_CERT" >&2; exit 1; }

SAN="DNS:localhost,DNS:${CN},IP:127.0.0.1"
if [[ -n "$IP" ]]; then
  SAN="${SAN},IP:${IP}"
fi

tmp_cnf="$(mktemp)"
trap 'rm -f "$tmp_cnf"' EXIT
cat > "$tmp_cnf" <<EOF
[req]
distinguished_name = req_dn
x509_extensions = v3_req
prompt = no
[req_dn]
CN = ${CN}
[v3_req]
subjectAltName = ${SAN}
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF

openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout "$KEY" -out "$CERT" \
  -config "$tmp_cnf"

chmod 644 "$CERT"
chmod 640 "$KEY"
if getent group nginx >/dev/null 2>&1; then
  chown root:nginx "$KEY" 2>/dev/null || chown root:root "$KEY"
elif getent group labvault >/dev/null 2>&1; then
  chown root:labvault "$KEY" 2>/dev/null || true
fi

echo "tls_generated cert=$CERT key=$KEY cn=$CN"
echo "tls_note=Replace with an operator cert (LABVAULT_TLS_CERT / LABVAULT_TLS_KEY) for shared hosts."
