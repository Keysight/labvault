#!/usr/bin/env bash
# LabVault customer SKU — one-shot Docker Compose install.
# Usage: sudo ./deploy/install/oneshot-compose.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

log() { printf '+ %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# shellcheck disable=SC1091
source "$ROOT/deploy/install/lib-ready.sh"

command -v docker >/dev/null || die "Docker Engine required"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin required (docker compose)"

# Host dir for optional opsd socket mount (compose binds this read-only).
mkdir -p /run/labvault
chmod 755 /run/labvault

# Privileged ops socket group (Compose app joins via LABVAULT_OPS_GID)
if ! getent group labvault-ops >/dev/null; then
  groupadd --system labvault-ops
fi
export LABVAULT_OPS_GID="$(getent group labvault-ops | cut -d: -f3)"
mkdir -p /run/labvault
chown root:labvault-ops /run/labvault
chmod 755 /run/labvault

# Host-side Compose ops broker (no docker.sock in containers)
# Unified opsd unit name on every deploy mode (labvault-opsd.service).
install -m 0644 "$ROOT/deploy/systemd/labvault-opsd.service" /etc/systemd/system/labvault-opsd.service
sed -i "s|/opt/labvault/current|$ROOT|g" /etc/systemd/system/labvault-opsd.service
mkdir -p /etc/systemd/system/labvault-opsd.service.d
cat > /etc/systemd/system/labvault-opsd.service.d/compose.conf <<EOF
[Service]
Environment=LABVAULT_OPS_ADAPTER=compose
Environment=LABVAULT_COMPOSE_PROJECT_DIR=$ROOT
Environment=LABVAULT_COMPOSE_FILE=deploy/compose/docker-compose.yml
Environment=LABVAULT_COMPOSE_PROJECT_NAME=compose
EOF
# Migrate off legacy unit name if present.
systemctl disable --now labvault-opsd-compose 2>/dev/null || true
rm -f /etc/systemd/system/labvault-opsd-compose.service
systemctl daemon-reload
systemctl enable --now labvault-opsd

# Ensure CLI SSH host key volume path exists with safe perms
mkdir -p /var/lib/labvault/cli-ssh
chmod 700 /var/lib/labvault/cli-ssh


if [[ ! -f .env ]]; then
  log "Creating .env from .env.example"
  cp .env.example .env
  # shellcheck disable=SC2016
  SK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  HOST="$(hostname -f 2>/dev/null || hostname || echo localhost)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=${SK}|" .env
  sed -i "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,${HOST},${IP}|" .env
  ORIGINS="http://127.0.0.1:8000,http://localhost:8000"
  [[ -n "$IP" ]] && ORIGINS="${ORIGINS},http://${IP}:8000,http://${IP},https://${IP},https://${IP}:443"
  if grep -q '^LABVAULT_CSRF_TRUSTED_ORIGINS=' .env; then
    sed -i "s|^LABVAULT_CSRF_TRUSTED_ORIGINS=.*|LABVAULT_CSRF_TRUSTED_ORIGINS=${ORIGINS}|" .env
  else
    echo "LABVAULT_CSRF_TRUSTED_ORIGINS=${ORIGINS}" >> .env
  fi
  # Compose overrides DATABASE_URL in container; keep .env coherent for tooling.
  sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgres://labvault:labvault@db:5432/labvault|' .env
  sed -i 's|^NP_TIMESERIES_DATABASE_URL=.*|NP_TIMESERIES_DATABASE_URL=postgres://labvault:labvault@metrics-db:5432/labvault_metrics|' .env
  grep -q '^LABVAULT_CSRF_TRUSTED_ORIGINS=' .env || \
    echo "LABVAULT_CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000" >> .env
  grep -q '^LABVAULT_USE_TLS=' .env || echo "LABVAULT_USE_TLS=false" >> .env
  # Pulse / fleet plug-and-play defaults (shared cache + sane tick interval).
  grep -q '^LABVAULT_CACHE_DIR=' .env || echo "LABVAULT_CACHE_DIR=/app/var/django_cache" >> .env
  grep -q '^LABVAULT_HEARTBEAT_LOCK=' .env || \
    echo "LABVAULT_HEARTBEAT_LOCK=/app/var/django_cache/fleet_heartbeat.lock" >> .env
  grep -q '^LABVAULT_HEARTBEAT_INTERVAL_SECONDS=' .env || \
    echo "LABVAULT_HEARTBEAT_INTERVAL_SECONDS=120" >> .env
fi

# Ensure Pulse defaults even when .env already existed from an older tree.
grep -q '^LABVAULT_CACHE_DIR=' .env || echo "LABVAULT_CACHE_DIR=/app/var/django_cache" >> .env
grep -q '^LABVAULT_HEARTBEAT_LOCK=' .env || \
  echo "LABVAULT_HEARTBEAT_LOCK=/app/var/django_cache/fleet_heartbeat.lock" >> .env
grep -q '^LABVAULT_HEARTBEAT_INTERVAL_SECONDS=' .env || \
  echo "LABVAULT_HEARTBEAT_INTERVAL_SECONDS=120" >> .env

# Restoring a live lab dataset turns Lab Pulse on with no extra UI steps.
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  export LABVAULT_WORKER_MODE=live
fi
export LABVAULT_WORKER_MODE="${LABVAULT_WORKER_MODE:-idle}"
if grep -q '^LABVAULT_WORKER_MODE=' .env; then
  sed -i "s|^LABVAULT_WORKER_MODE=.*|LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}|" .env
else
  echo "LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}" >> .env
fi

if [[ ! "${LABVAULT_DEMO_DEFAULTS:-}" =~ ^(1|true|yes)$ ]]; then
  export LABVAULT_BOOTSTRAP_RANDOM="${LABVAULT_BOOTSTRAP_RANDOM:-1}"
fi

# Reject placeholder secrets early
python3 - <<'PY'
import os, re, pathlib
from pathlib import Path
env = {}
for line in Path(".env").read_text().splitlines():
    if not line.strip() or line.strip().startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip()
sk = env.get("DJANGO_SECRET_KEY") or env.get("SECRET_KEY") or ""
bad = (len(sk) < 32) or bool(re.search(r"(change.?me|placeholder|secret_key|demo)", sk, re.I))
if bad:
    raise SystemExit("DJANGO_SECRET_KEY missing, <32 chars, or looks like a placeholder — edit .env")
print("secret_ok")
PY

export LABVAULT_OPS_SOCK_HOST="${LABVAULT_OPS_SOCK_HOST:-/run/labvault}"
export LABVAULT_HTTP_PORT="${LABVAULT_HTTP_PORT:-8000}"

log "Building and starting compose stack"
docker compose -f deploy/compose/docker-compose.yml up -d --build

log "Waiting for /health/ready"
deadline=$((SECONDS + 180))
until curl -fsS "http://127.0.0.1:${LABVAULT_HTTP_PORT}/health/ready" >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    docker compose -f deploy/compose/docker-compose.yml ps
    die "/health/ready not ready within 180s"
  fi
  sleep 3
done

log "Running migrations + bootstrap via labvaultctl"
# Ensure env visible to ctl (compose already migrated on first boot via entry? — ctl migrate is idempotent)
set -a
# shellcheck disable=SC1091
source <(grep -E '^[A-Z0-9_]+=' .env | sed 's/\r$//') 
set +a
# Point ctl at published URL host DB only if running ctl on host against compose network — skip host migrate;
# instead exec into web:
docker compose -f deploy/compose/docker-compose.yml exec -T -u labvault web \
  python manage.py migrate --noinput 
docker compose -f deploy/compose/docker-compose.yml exec -T -u labvault web \
  python manage.py migrate --database np_timeseries --noinput 

STATE="${LABVAULT_STATE_DIR:-/var/lib/labvault}"
mkdir -p "$STATE"

BOOTSTRAP=(python manage.py bootstrap_labvault)
if [[ "${LABVAULT_WORKER_MODE}" == "live" ]]; then
  BOOTSTRAP+=(--live)
fi
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  [[ -f "$LABVAULT_RESTORE_DATASET" ]] || die "LABVAULT_RESTORE_DATASET not a file: $LABVAULT_RESTORE_DATASET"
  log "Copying restore dataset into web container"
  docker compose -f deploy/compose/docker-compose.yml cp "$LABVAULT_RESTORE_DATASET" web:/tmp/labvault_export.json
  BOOTSTRAP+=(--restore /tmp/labvault_export.json)
fi
log "bootstrap_labvault ${BOOTSTRAP[*]}"
docker compose -f deploy/compose/docker-compose.yml exec -T -u labvault web "${BOOTSTRAP[@]}"
docker compose -f deploy/compose/docker-compose.yml cp web:/tmp/labvault-fleet-token \
  "$STATE/fleet-token"
docker compose -f deploy/compose/docker-compose.yml cp web:/tmp/bootstrap-credentials \
  "$STATE/bootstrap-credentials"
chmod 600 "$STATE/fleet-token" "$STATE/bootstrap-credentials"
if [[ "${LABVAULT_WORKER_MODE}" == "live" ]]; then
  log "Ensuring heartbeat/collector are up in live (Lab Pulse) mode"
  docker compose -f deploy/compose/docker-compose.yml up -d --no-deps heartbeat collector
fi

log "Confirming required Compose services are running"
for svc in db metrics-db web heartbeat collector refresh jobs cli-ssh nginx; do
  if docker compose -f deploy/compose/docker-compose.yml ps --status running --services 2>/dev/null | grep -qx "$svc"; then
    log "running: $svc"
  else
    log "starting missing service: $svc"
    docker compose -f deploy/compose/docker-compose.yml up -d --no-deps "$svc"
  fi
done

if [[ "${LABVAULT_SKIP_VERIFY:-}" =~ ^(1|true|yes)$ ]]; then
  log "Skipping post_deploy_verify (LABVAULT_SKIP_VERIFY set)"
else
  log "Running post_deploy_verify"
  TOKEN="$(awk -F= '/^token=/{print $2}' "$STATE/fleet-token" | tr -d '[:space:]')"
  [[ -n "$TOKEN" ]] || die "fleet token missing after bootstrap"
  BASE_URL="http://127.0.0.1:${LABVAULT_HTTP_PORT}" TOKEN="$TOKEN" \
    LABVAULT_WORKER_MODE="${LABVAULT_WORKER_MODE}" ADAPTER=compose \
    LABVAULT_STATE_DIR="$STATE" \
    bash "$ROOT/deploy/scripts/post_deploy_verify.sh" || \
    die "post_deploy_verify failed — see docs/admin/SERVICES.md"
fi

echo "services=docs/admin/SERVICES.md"
echo "verify=./deploy/scripts/post_deploy_verify.sh"
maybe_install_http80 "$ROOT" ""
print_ready_banner "$STATE" "${LABVAULT_HTTP_PORT}"
echo "ssh_cli=ssh -p ${LABVAULT_CLI_SSH_PORT:-2222} <staff-user>@127.0.0.1"
echo "opsd=labvault-opsd (host) sock=/run/labvault/ops.sock"
echo "pulse=LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}"
