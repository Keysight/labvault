#!/usr/bin/env bash
# Optional local secret / dependency / image scans. Missing scanners are reported, not faked.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
FAIL=0
if command -v gitleaks >/dev/null; then
  gitleaks detect --no-banner --source "$ROOT" || FAIL=$((FAIL + 1))
else
  echo "SKIP gitleaks (not installed)"
fi
if command -v pip-audit >/dev/null; then
  if [[ -f requirements.lock ]]; then
    pip-audit -r requirements.lock || FAIL=$((FAIL + 1))
  else
    pip-audit -r requirements.txt || FAIL=$((FAIL + 1))
  fi
else
  echo "SKIP pip-audit (not installed)"
fi
if command -v trivy >/dev/null; then
  trivy fs --severity HIGH,CRITICAL "$ROOT" || FAIL=$((FAIL + 1))
else
  echo "SKIP trivy (not installed)"
fi
exit "$FAIL"
