#!/usr/bin/env bash
# Post-deploy verify: required services up + fleet/Pulse plumbing healthy enough to use.
# Usage:
#   BASE_URL=https://127.0.0.1:9443 TOKEN=<fleet-token> \
#     ./deploy/scripts/post_deploy_verify.sh
# Compose adapter (default): also checks `docker compose ps`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_URL="${BASE_URL:-https://127.0.0.1:9443}"
CURL_OPTS=()
if [[ "$BASE_URL" == https://* ]]; then
  CURL_OPTS+=(-k)
fi
if [[ -z "${TOKEN:-}" && -f "${LABVAULT_STATE_DIR:-/var/lib/labvault}/fleet-token" ]]; then
  TOKEN="$(awk -F= '/^token=/{print $2}' "${LABVAULT_STATE_DIR:-/var/lib/labvault}/fleet-token" | tr -d '[:space:]')"
fi
TOKEN="${TOKEN:-}"
[[ -n "$TOKEN" ]] || { echo "TOKEN required (or \$LABVAULT_STATE_DIR/fleet-token)"; exit 2; }
ADAPTER="${ADAPTER:-compose}"   # compose | systemd | none
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/docker-compose.yml}"
export BASE_URL TOKEN LABVAULT_WORKER_MODE="${LABVAULT_WORKER_MODE:-}"
FAIL=0

pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }

need_http() {
  local name="$1" expect="$2" url="$3"
  local code
  code=$(curl -sS "${CURL_OPTS[@]}" -o /tmp/lv_verify_body -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" "$url" || echo 000)
  if [[ "$code" == "$expect" ]]; then
    pass "$name ($code)"
  else
    fail "$name got=$code want=$expect"
  fi
}

echo "verify BASE_URL=$BASE_URL ADAPTER=$ADAPTER"

need_http 'health/live' 200 "$BASE_URL/health/live"
need_http 'health/ready' 200 "$BASE_URL/health/ready"
need_http 'login' 200 "$BASE_URL/login/"
need_http 'fleet health' 200 "$BASE_URL/api/fleet/health.json"
need_http 'fleet heartbeat' 200 "$BASE_URL/api/fleet/heartbeat.json"

if [[ "$ADAPTER" == "compose" ]]; then
  if ! command -v docker >/dev/null; then
    fail "docker missing"
  else
    mapfile -t missing < <(docker compose -f "$COMPOSE_FILE" ps --services --status running 2>/dev/null | sort)
    for svc in db metrics-db web heartbeat collector refresh jobs cli-ssh nginx; do
      if printf '%s\n' "${missing[@]:-}" | grep -qx "$svc"; then
        pass "compose service running: $svc"
      else
        # service names may differ slightly (metrics-db vs metrics-db)
        if docker compose -f "$COMPOSE_FILE" ps --status running 2>/dev/null | grep -Eq "\\b${svc}\\b|${svc//_/-}"; then
          pass "compose service running: $svc"
        else
          fail "compose service not running: $svc — start with: docker compose -f $COMPOSE_FILE up -d"
        fi
      fi
    done
    # Host opsd socket must exist for service control
    if [[ -S /run/labvault/ops.sock ]]; then
      pass "opsd socket present"
    else
      fail "opsd socket missing at /run/labvault/ops.sock — enable labvault-opsd"
    fi
    # CLI SSH port
    if python3 - <<'PY'
import os, socket
port=int(os.environ.get("LABVAULT_CLI_SSH_PORT","2222"))
s=socket.socket(); s.settimeout(2)
try:
  s.connect(("127.0.0.1", port)); print("open")
except Exception as e:
  raise SystemExit(str(e))
finally:
  s.close()
PY
    then
      pass "cli-ssh port ${LABVAULT_CLI_SSH_PORT:-2222} open"
    else
      fail "cli-ssh port ${LABVAULT_CLI_SSH_PORT:-2222} not open"
    fi
  fi
elif [[ "$ADAPTER" == "systemd" ]]; then
  for unit in labvault-web labvault-refresh labvault-heartbeat labvault-collector labvault-cli-worker labvault-opsd labvault-cli-ssh; do
    if systemctl is-active --quiet "$unit"; then
      pass "systemd active: $unit"
    else
      fail "systemd inactive: $unit — systemctl enable --now $unit"
    fi
  done
fi

python3 - <<'PY' || FAIL=$((FAIL + 1))
import json, os, ssl, urllib.request
base = os.environ["BASE_URL"]
token = os.environ["TOKEN"]
ctx = ssl._create_unverified_context() if base.startswith("https://") else None
req = urllib.request.Request(
    f"{base}/api/fleet/heartbeat.json",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
    data = json.load(r)
mode = data.get("mode")
counts = data.get("counts") or {}
print(f"INFO heartbeat mode={mode} counts={counts}")
# Idle install is OK (workers up, no probes). Live should have a non-empty store.
worker = (os.environ.get("LABVAULT_WORKER_MODE") or "").strip().lower()
if worker == "live" or mode == "live":
    if int(counts.get("total") or 0) < 1:
        raise SystemExit("live mode but heartbeat store empty — check heartbeat service + LABVAULT_CACHE_DIR share")
print("PASS heartbeat plumbing")
PY

if [[ -x "$ROOT/deploy/scripts/fleet_api_smoke.sh" ]]; then
  echo "---- fleet_api_smoke ----"
  BASE_URL="$BASE_URL" TOKEN="$TOKEN" bash "$ROOT/deploy/scripts/fleet_api_smoke.sh" || FAIL=$((FAIL + 1))
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "VERIFY_OK"
  exit 0
fi
echo "VERIFY_FAILED count=$FAIL — see docs/admin/SERVICES.md"
exit 1
