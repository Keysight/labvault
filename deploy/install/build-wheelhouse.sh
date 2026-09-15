#!/usr/bin/env bash
# Build an offline wheelhouse for air-gapped installs (run on a networked builder).
# Usage: ./deploy/install/build-wheelhouse.sh [/path/to/wheelhouse]
#
# Optional skips (explicit only — do not hide host-dep / python-ldap failures):
#   LABVAULT_SKIP_WHEEL_HOSTDEPS=1  — headers already installed
#   LABVAULT_SKIP_LDAP_WHEEL=1      — skip compiling python-ldap (airgap must already have a wheel)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/dist/wheelhouse}"
mkdir -p "$OUT"
cd "$ROOT"

install_host_deps() {
  if [[ "${LABVAULT_SKIP_WHEEL_HOSTDEPS:-}" == "1" ]]; then
    echo "skip host deps (LABVAULT_SKIP_WHEEL_HOSTDEPS=1)"
    return 0
  fi
  if command -v dnf >/dev/null; then
    sudo dnf install -y python3-devel gcc openldap-devel openssl-devel cyrus-sasl-devel
  elif command -v apt-get >/dev/null; then
    sudo apt-get install -y python3-dev build-essential libldap2-dev libsasl2-dev
  else
    echo "No dnf/apt-get. Install python-ldap build headers, or set LABVAULT_SKIP_WHEEL_HOSTDEPS=1" >&2
    exit 1
  fi
}
install_host_deps

# Prefer a throwaway venv so PEP 668 / externally-managed hosts still work.
# Match EL9 oneshot (python3.11) when the builder has it.
PY=python3
if command -v python3.11 >/dev/null; then
  PY=python3.11
elif command -v python3.12 >/dev/null; then
  PY=python3.12
fi
BUILD_VENV="$(mktemp -d)/wheelhouse-venv"
"$PY" -m venv "$BUILD_VENV"
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"
python -m pip install -U pip wheel setuptools
# Build tooling must be in the wheelhouse so air-gapped hosts can compile sdists (python-ldap).
python -m pip download pip wheel setuptools -d "$OUT"
python -m pip download -r requirements.txt gunicorn -d "$OUT"
if [[ "${LABVAULT_SKIP_LDAP_WHEEL:-}" == "1" ]]; then
  echo "skip python-ldap wheel (LABVAULT_SKIP_LDAP_WHEEL=1)"
else
  python -m pip wheel --no-deps python-ldap -w "$OUT"
fi
python -c "
import platform, pathlib, sys
p = pathlib.Path('$OUT')
(p / 'PLATFORM.txt').write_text(
    f'python={sys.version.split()[0]}\nmachine={platform.machine()}\nsystem={platform.system()}\n',
    encoding='utf-8',
)
print('wheelhouse', p)
"
deactivate
rm -rf "$(dirname "$BUILD_VENV")"
echo "DONE wheelhouse=$OUT"
echo "Copy this directory + the source tarball to the air-gapped host."
