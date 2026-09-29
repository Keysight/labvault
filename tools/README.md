# `tools/`

Release gates and packaging helpers. CI (`.github/workflows/ci.yml`, `security.yml`,
`release.yml`) runs the same checks. Details:
[docs/development/subsystems/operations.md](../docs/development/subsystems/operations.md#release-gates-and-ci)
and [docs/development/RELEASE_PROCESS.md](../docs/development/RELEASE_PROCESS.md).

| File | Purpose |
|---|---|
| `check_public_source.py [--target DIR]` | Fail closed on excluded product surfaces, forbidden imports, internal lab address prefixes, internal hostnames/credentials, and an advertised demo login. Prints `OK — public source checks passed` or `FAIL` + findings |
| `check_docs.py` | Require the customer doc map (`REQUIRED`) and reject internal runbooks (`FORBIDDEN`) |
| `run_release_gates.sh` | Both checks + `manage.py check` + `makemigrations --check --dry-run` + the fast test subset + `docker compose config` |
| `run_security_scans.sh` | `gitleaks`, `pip-audit`, `trivy` when installed (reports `SKIP` otherwise); exit code = number of failing scanners |
| `generate_sbom.sh` | CycloneDX SBOM to `dist/sbom.cdx.json`, or `dist/sbom.fallback.txt` without `cyclonedx-bom` |
| `build_public_source.py --source SRC --target DST` | Build a clean public tree from a private source: allowlist copy, hard-dump excluded modules/commands/tests, write `public-source-manifest.txt` and `tools/public_source_build.json`. **Deletes `DST` first** — never point it at a working tree you want to keep |
| `public_source_build.json` | Manifest from the last `build_public_source.py` run (copied/removed file lists) |
| `release_gates.md` | Which gates ship in the repo vs. which are operator-owned |

```bash
python3 tools/check_public_source.py
python3 tools/check_docs.py
bash tools/run_release_gates.sh
```
