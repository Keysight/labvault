#!/usr/bin/env bash
# Fail-closed local/CI subset of Keysight public release gates.
# Corporate org create / MIT approval / public push are operator-owned.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
export DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')}"
export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1,testserver}"
export DJANGO_DEBUG=false

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi
"$PY" tools/check_public_source.py --target .
"$PY" tools/check_docs.py
"$PY" manage.py check
"$PY" manage.py makemigrations --check --dry-run
"$PY" manage.py test connect.tests.test_hard_dump connect.tests.test_bootstrap_defaults \
  connect.tests.test_csrf_exempt_inventory connect.tests.test_labvault_cli_catalog \
  connect.tests.test_redact_payload connect.tests.test_validate_external_config \
  connect.tests.test_device_detail_render connect.tests.test_tls_settings \
  connect.tests.test_diagnostics connect.tests.test_cli_ssh_auth \
  connect.tests.test_labvault_cli_commands connect.tests.test_health_endpoints \
  --verbosity=1
if [[ -f .env ]]; then
  docker compose -f deploy/compose/docker-compose.yml config >/dev/null
elif command -v docker >/dev/null; then
  cp -n .env.example .env
  docker compose -f deploy/compose/docker-compose.yml config >/dev/null
fi
echo "release_gates local subset: OK"
