#!/usr/bin/env bash
# Fleet + health API smoke test (customer SKU).
# Empty-lab safe. Optional live inventory checks via LABVAULT_SMOKE_CHASSIS_ID / LABVAULT_SMOKE_OCS_IP.
# Usage: BASE_URL=https://host:9443 TOKEN=... ./deploy/scripts/fleet_api_smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-https://127.0.0.1:9443}"
CURL_OPTS=()
if [[ "$BASE_URL" == https://* ]]; then
  CURL_OPTS+=(-k)
fi
TOKEN="${TOKEN:-}"
if [[ -z "$TOKEN" && -f "${LABVAULT_STATE_DIR:-/var/lib/labvault}/fleet-token" ]]; then
  TOKEN="$(awk -F= '/^token=/{print $2}' "${LABVAULT_STATE_DIR:-/var/lib/labvault}/fleet-token" | tr -d '[:space:]')"
fi
[[ -n "$TOKEN" ]] || { echo "TOKEN required (or $LABVAULT_STATE_DIR/fleet-token)"; exit 2; }
AUTH=(-H "Authorization: Bearer ${TOKEN}")
FAIL=0

check() {
  local name="$1" expect="$2" url="$3"
  local code body
  code=$(curl -sS "${CURL_OPTS[@]}" -o /tmp/lv_smoke_body -w '%{http_code}' "${AUTH[@]}" "$url" || echo 000)
  body=$(head -c 200 /tmp/lv_smoke_body | tr '\n' ' ')
  if [[ "$code" == "$expect" ]]; then
    printf 'PASS %-42s %s\n' "$name" "$code"
  else
    printf 'FAIL %-42s got=%s want=%s %s\n' "$name" "$code" "$expect" "$body"
    FAIL=$((FAIL + 1))
  fi
}

check_noauth() {
  local name="$1" expect="$2" url="$3"
  local code body
  code=$(curl -sS "${CURL_OPTS[@]}" -o /tmp/lv_smoke_body -w '%{http_code}' "$url" || echo 000)
  body=$(head -c 120 /tmp/lv_smoke_body | tr '\n' ' ')
  if [[ "$code" == "$expect" ]]; then
    printf 'PASS %-42s %s\n' "$name" "$code"
  else
    printf 'FAIL %-42s got=%s want=%s %s\n' "$name" "$code" "$expect" "$body"
    FAIL=$((FAIL + 1))
  fi
}

export BASE_URL TOKEN
printf 'Smoke %s\n' "$BASE_URL"

check 'health/live' 200 "$BASE_URL/health/live"
check 'health/ready' 200 "$BASE_URL/health/ready"
check 'login' 200 "$BASE_URL/login/"
check_noauth 'fleet health no auth' 401 "$BASE_URL/api/fleet/health.json"
check 'fleet index' 200 "$BASE_URL/api/fleet/"
check 'fleet openapi' 200 "$BASE_URL/api/fleet/openapi.json"
check 'fleet health' 200 "$BASE_URL/api/fleet/health.json"
check 'fleet heartbeat' 200 "$BASE_URL/api/fleet/heartbeat.json"
check 'fleet inventory' 200 "$BASE_URL/api/fleet/inventory.json"
check 'fleet sla' 200 "$BASE_URL/api/fleet/sla.json"
check 'fleet summary' 200 "$BASE_URL/api/fleet/summary.json"
check 'fleet transmission' 200 "$BASE_URL/api/fleet/metrics/transmission.json"
check 'fleet inventory csv' 200 "$BASE_URL/api/fleet/inventory.csv"
check 'fleet reservations' 200 "$BASE_URL/api/fleet/reservations.json"
check 'fleet ownership' 200 "$BASE_URL/api/fleet/ownership.json"
check 'fleet conflicts' 200 "$BASE_URL/api/fleet/conflicts.json"
check 'fleet ports telemetry' 200 "$BASE_URL/api/fleet/ports/telemetry.json"
check 'fleet ports preflight' 409 "$BASE_URL/api/fleet/ports/preflight.json"

if [[ -n "${LABVAULT_SMOKE_CHASSIS_ID:-}" ]]; then
  check 'chassis health' 200 "$BASE_URL/api/fleet/chassis/${LABVAULT_SMOKE_CHASSIS_ID}/health.json"
  check 'chassis ports' 200 "$BASE_URL/api/fleet/chassis/${LABVAULT_SMOKE_CHASSIS_ID}/ports.json"
fi
if [[ -n "${LABVAULT_SMOKE_OCS_IP:-}" ]]; then
  check 'ocs crossconnects' 200 "$BASE_URL/api/ocs/${LABVAULT_SMOKE_OCS_IP}/crossconnects/"
fi

SMOKE_USER="${LABVAULT_SMOKE_USER:-}"
SMOKE_PASS="${LABVAULT_SMOKE_PASSWORD:-}"
CRED="${LABVAULT_STATE_DIR:-/var/lib/labvault}/bootstrap-credentials"
if [[ -z "$SMOKE_USER" && -f "$CRED" ]]; then
  SMOKE_USER="$(awk -F= '/^username=/{print $2}' "$CRED" | tr -d '[:space:]')"
  SMOKE_PASS="$(awk -F= '/^password=/{print $2}' "$CRED" | tr -d '[:space:]')"
fi
if [[ -n "$SMOKE_USER" && -n "$SMOKE_PASS" ]]; then
  JAR=/tmp/lv_smoke_cookies.txt
  rm -f "$JAR"
  csrf=$(curl -sS "${CURL_OPTS[@]}" -c "$JAR" "$BASE_URL/login/" | sed -n 's/.*name="csrfmiddlewaretoken" value="\([^"]*\)".*/\1/p' | head -1)
  curl -sS "${CURL_OPTS[@]}" -b "$JAR" -c "$JAR" -X POST "$BASE_URL/login/" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "csrfmiddlewaretoken=${csrf}" \
    --data-urlencode "username=${SMOKE_USER}" \
    --data-urlencode "password=${SMOKE_PASS}" \
    -o /dev/null
  code=$(curl -sS "${CURL_OPTS[@]}" -b "$JAR" -o /tmp/lv_smoke_body -w '%{http_code}' "$BASE_URL/api/live-status/" || echo 000)
  if [[ "$code" == "200" ]]; then
    printf 'PASS %-42s %s\n' 'live-status (session)' "$code"
  else
    printf 'FAIL %-42s got=%s want=200\n' 'live-status (session)' "$code"
    FAIL=$((FAIL+1))
  fi
else
  printf 'SKIP %-42s %s\n' 'live-status (session)' 'no credential file'
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
counts = data.get("counts") or {}
mode = data.get("mode")
print("PASS heartbeat plumbing", "mode=", mode, "counts=", counts)
chassis_id = (os.environ.get("LABVAULT_SMOKE_CHASSIS_ID") or "").strip()
worker = (os.environ.get("LABVAULT_WORKER_MODE") or "").strip().lower()
if chassis_id and worker == "live":
    req = urllib.request.Request(
        f"{base}/api/fleet/health.json",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        health = json.load(r)
    row = next((c for c in health.get("chassis", []) if str(c.get("chassis_id")) == chassis_id), None)
    if not row:
        raise SystemExit(f"chassis {chassis_id} missing from fleet health")
    print("PASS telemetry sanity", row.get("heartbeat_ok"))
PY

exit "$FAIL"
