#!/usr/bin/env bash
# Generate a CycloneDX SBOM when cyclonedx-bom is installed; otherwise write a fallback inventory.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
mkdir -p dist
if python3 -c "import cyclonedx" 2>/dev/null; then
  python3 -m cyclonedx_py requirements -o dist/sbom.cdx.json requirements.txt
elif command -v cyclonedx-py >/dev/null; then
  cyclonedx-py requirements -o dist/sbom.cdx.json requirements.txt
else
  python3 - <<'PY'
from pathlib import Path
reqs = Path("requirements.txt").read_text(encoding="utf-8")
Path("dist/sbom.fallback.txt").write_text(
    "# Install cyclonedx-bom for a CycloneDX SBOM.\n" + reqs, encoding="utf-8"
)
print("wrote dist/sbom.fallback.txt (cyclonedx-bom not installed)")
PY
fi
echo "sbom artifacts under dist/"
