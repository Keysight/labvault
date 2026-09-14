#!/usr/bin/env bash
# Install host nginx edge for bare IP/hostname on every deploy mode:
#   http://<ip>/  → https://<ip>/login/
#   https://<ip>/ → TLS (lab self-signed) → gunicorn :8000
#
# Usage (root):
#   sudo LABVAULT_ROOT=/opt/labvault/current ./deploy/scripts/install-labvault-http80.sh
#
# Skip with LABVAULT_SKIP_HTTP80=1.
# Reuse operator certs via LABVAULT_TLS_CERT / LABVAULT_TLS_KEY.
set -euo pipefail

LABVAULT_ROOT="${LABVAULT_ROOT:-/opt/labvault/current}"
SRC="${LABVAULT_ROOT}/deploy/nginx/labvault-edge.conf"
[[ -f "$SRC" ]] || SRC="${LABVAULT_ROOT}/deploy/nginx/labvault-port80-to-8000.conf"
OUT_NAME="${OUT_NAME:-labvault-edge.conf}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx/conf.d}"
NGINX_SERVICE="${NGINX_SERVICE:-nginx}"
TLS_DIR="${LABVAULT_TLS_DIR:-/etc/labvault/tls}"
TLS_CERT="${LABVAULT_TLS_CERT:-${TLS_DIR}/labvault.crt}"
TLS_KEY="${LABVAULT_TLS_KEY:-${TLS_DIR}/labvault.key}"

log() { printf '+ %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root"

if [[ "${LABVAULT_SKIP_HTTP80:-}" =~ ^(1|true|yes)$ ]]; then
  log "Skipping edge install (LABVAULT_SKIP_HTTP80 set)"
  exit 0
fi

if [[ -f "${NGINX_CONF_DIR}/labvault-443.conf" ]]; then
  log "Production TLS vhost labvault-443.conf present — skipping lab edge"
  exit 0
fi

if ! command -v nginx >/dev/null 2>&1; then
  log "Installing nginx for :80/:443 edge"
  if command -v dnf >/dev/null; then
    dnf install -y nginx
  elif command -v apt-get >/dev/null; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
  else
    die "nginx not found and no supported package manager"
  fi
fi
command -v openssl >/dev/null || die "openssl required"

[[ -f "$SRC" ]] || die "missing $SRC"

mkdir -p "$NGINX_CONF_DIR" "$TLS_DIR"
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
[[ -L /etc/nginx/sites-enabled/default ]] && rm -f /etc/nginx/sites-enabled/default
# Remove legacy HTTP-only redirect so :80 is not double-bound.
rm -f "${NGINX_CONF_DIR}/labvault-port80.conf" 2>/dev/null || true

if [[ -f /etc/nginx/nginx.conf ]] && grep -qE '^[[:space:]]*listen[[:space:]]+80' /etc/nginx/nginx.conf; then
  sed -i -E 's/^([[:space:]]*)listen([[:space:]]+\[::\]:80)/\1# listen\2/; s/^([[:space:]]*)listen([[:space:]]+80)/\1# listen\2/' /etc/nginx/nginx.conf || true
fi

HOST="$(hostname -f 2>/dev/null || hostname || echo localhost)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if [[ ! -f "$TLS_CERT" || ! -f "$TLS_KEY" ]]; then
  SAN="DNS:localhost,DNS:${HOST}"
  [[ -n "$IP" ]] && SAN="${SAN},IP:${IP}"
  log "Generating lab TLS cert at $TLS_CERT (SAN=$SAN)"
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -keyout "$TLS_KEY" -out "$TLS_CERT" \
    -subj "/CN=${HOST}" \
    -addext "subjectAltName=${SAN}"
fi
# validate_external_config requires key mode without group/other bits.
chmod 600 "$TLS_KEY"
chmod 644 "$TLS_CERT"
chown root:root "$TLS_CERT" "$TLS_KEY"

install -m 0644 "$SRC" "${NGINX_CONF_DIR}/${OUT_NAME}"
sed -i \
  -e "s|/etc/labvault/tls/labvault.crt|${TLS_CERT}|g" \
  -e "s|/etc/labvault/tls/labvault.key|${TLS_KEY}|g" \
  "${NGINX_CONF_DIR}/${OUT_NAME}"
log "Wrote ${NGINX_CONF_DIR}/${OUT_NAME}"

# SELinux: nginx must be allowed to proxy to gunicorn on :8000.
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]]; then
  if command -v setsebool >/dev/null 2>&1; then
    setsebool -P httpd_can_network_connect 1 || true
    log "SELinux httpd_can_network_connect=on"
  fi
fi

if systemctl is-active --quiet firewalld 2>/dev/null; then
  firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-service=https >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  log "firewalld: http/https allowed"
fi

# Wire Django env when present so https PUBLIC_ORIGIN validates.
for ENV_FILE in /etc/labvault/labvault.env "${LABVAULT_ROOT}/.env"; do
  [[ -f "$ENV_FILE" ]] || continue
  if grep -q '^LABVAULT_TLS_CERT=' "$ENV_FILE"; then
    sed -i "s|^LABVAULT_TLS_CERT=.*|LABVAULT_TLS_CERT=${TLS_CERT}|" "$ENV_FILE"
  else
    echo "LABVAULT_TLS_CERT=${TLS_CERT}" >> "$ENV_FILE"
  fi
  if grep -q '^LABVAULT_TLS_KEY=' "$ENV_FILE"; then
    sed -i "s|^LABVAULT_TLS_KEY=.*|LABVAULT_TLS_KEY=${TLS_KEY}|" "$ENV_FILE"
  else
    echo "LABVAULT_TLS_KEY=${TLS_KEY}" >> "$ENV_FILE"
  fi
  if [[ -n "$IP" ]]; then
    if grep -q '^LABVAULT_PUBLIC_ORIGIN=' "$ENV_FILE"; then
      sed -i "s|^LABVAULT_PUBLIC_ORIGIN=.*|LABVAULT_PUBLIC_ORIGIN=https://${IP}|" "$ENV_FILE"
    else
      echo "LABVAULT_PUBLIC_ORIGIN=https://${IP}" >> "$ENV_FILE"
    fi
    # Ensure https origins are trusted for CSRF (append if missing).
    if grep -q '^LABVAULT_CSRF_TRUSTED_ORIGINS=' "$ENV_FILE"; then
      cur="$(grep '^LABVAULT_CSRF_TRUSTED_ORIGINS=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
      for o in "https://${IP}" "https://${HOST}" "https://${IP}:443"; do
        [[ "$cur" == *"$o"* ]] || cur="${cur},${o}"
      done
      sed -i "s|^LABVAULT_CSRF_TRUSTED_ORIGINS=.*|LABVAULT_CSRF_TRUSTED_ORIGINS=${cur}|" "$ENV_FILE"
    fi
  fi
  log "Updated TLS/origin settings in $ENV_FILE"
  break
done

nginx -t
systemctl enable --now "$NGINX_SERVICE"
systemctl reload "$NGINX_SERVICE" 2>/dev/null || systemctl restart "$NGINX_SERVICE"
log "edge active: http://<ip>/ → https://<ip>/login/ ; https://<ip>/ → app :8000"
