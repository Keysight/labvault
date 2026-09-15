#!/usr/bin/env bash
# Generate nginx LabVault TLS config from env and install.
#
# Usage (as root):
#   sudo LABVAULT_ROOT=/opt/labvault/current ./deploy/scripts/install-labvault-nginx.sh
#   sudo ./deploy/scripts/install-labvault-nginx.sh \
#     --hostname labvault.example \
#     --cert /etc/labvault/tls/fullchain.pem \
#     --key /etc/labvault/tls/privkey.pem
#
# Reads (first found): $LABVAULT_ROOT/.env then /etc/labvault/labvault.env
#   LABVAULT_PUBLIC_HOSTNAME
#   LABVAULT_TLS_PORT (default 9443)
#   LABVAULT_TLS_CERT or LABVAULT_SSL_CERT
#   LABVAULT_TLS_KEY  or LABVAULT_SSL_KEY

set -euo pipefail

LABVAULT_ROOT="${LABVAULT_ROOT:-/opt/labvault/current}"
ENV_FILE="${ENV_FILE:-}"
TEMPLATE="$LABVAULT_ROOT/deploy/nginx/labvault-https.conf.template"
OUT_NAME="${OUT_NAME:-labvault-9443.conf}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx/conf.d}"
NGINX_SERVICE="${NGINX_SERVICE:-nginx}"

HOSTNAME=""
SSL_CERT=""
SSL_KEY=""

load_env() {
    local f
    for f in "$ENV_FILE" "$LABVAULT_ROOT/.env" /etc/labvault/labvault.env; do
        [[ -n "$f" && -f "$f" ]] || continue
        # shellcheck disable=SC1090
        set -a
        # shellcheck disable=SC1091
        source "$f"
        set +a
        ENV_FILE="$f"
        break
    done
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hostname) HOSTNAME="$2"; shift 2 ;;
        --cert) SSL_CERT="$2"; shift 2 ;;
        --key) SSL_KEY="$2"; shift 2 ;;
        --root) LABVAULT_ROOT="$2"; TEMPLATE="$LABVAULT_ROOT/deploy/nginx/labvault-https.conf.template"; shift 2 ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

load_env

HOSTNAME="${HOSTNAME:-${LABVAULT_PUBLIC_HOSTNAME:-}}"
TLS_PORT="${LABVAULT_TLS_PORT:-9443}"
SSL_CERT="${SSL_CERT:-${LABVAULT_TLS_CERT:-${LABVAULT_SSL_CERT:-/var/lib/labvault/tls/fullchain.pem}}}"
SSL_KEY="${SSL_KEY:-${LABVAULT_TLS_KEY:-${LABVAULT_SSL_KEY:-/var/lib/labvault/tls/privkey.pem}}}"

if [[ -z "$HOSTNAME" ]]; then
    echo "Set LABVAULT_PUBLIC_HOSTNAME or pass --hostname" >&2
    exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
    echo "Missing template: $TEMPLATE" >&2
    exit 1
fi
if [[ ! -f "$SSL_CERT" || ! -f "$SSL_KEY" ]]; then
    echo "TLS cert/key not found:" >&2
    echo "  cert: $SSL_CERT" >&2
    echo "  key:  $SSL_KEY" >&2
    exit 1
fi

mkdir -p "$NGINX_CONF_DIR"
OUT_PATH="$NGINX_CONF_DIR/$OUT_NAME"

sed -e "s|@@LABVAULT_SERVER_NAME@@|$HOSTNAME|g" \
    -e "s|@@LABVAULT_SSL_CERT@@|$SSL_CERT|g" \
    -e "s|@@LABVAULT_SSL_KEY@@|$SSL_KEY|g" \
    -e "s|@@LABVAULT_TLS_PORT@@|$TLS_PORT|g" \
    "$TEMPLATE" >"$OUT_PATH"

chmod 644 "$OUT_PATH"
echo "Wrote $OUT_PATH (server_name=$HOSTNAME, tls_port=$TLS_PORT, upstream=127.0.0.1:8000)"

nginx -t
# Rocky/RHEL SELinux only allows nginx on labeled HTTP ports (80/443). 9443 needs http_port_t.
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; then
  if ! command -v semanage >/dev/null 2>&1; then
    if command -v dnf >/dev/null; then
      dnf install -y policycoreutils-python-utils || true
    elif command -v apt-get >/dev/null; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y policycoreutils-python-utils || true
    fi
  fi
  if command -v semanage >/dev/null 2>&1; then
    semanage port -a -t http_port_t -p tcp "$TLS_PORT" 2>/dev/null || \
      semanage port -m -t http_port_t -p tcp "$TLS_PORT" 2>/dev/null || true
  fi
  # nginx → 127.0.0.1:8000 (gunicorn)
  setsebool -P httpd_can_network_connect 1 2>/dev/null || true
fi
systemctl enable --now "$NGINX_SERVICE"
systemctl reload "$NGINX_SERVICE" || systemctl restart "$NGINX_SERVICE"
echo "Reloaded $NGINX_SERVICE"
