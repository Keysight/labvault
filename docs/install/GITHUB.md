# GitHub submission (customer SKU)

## Before first push

1. Confirm this tree is the **customer SKU** (`LABVAULT_CUSTOMER_SKU = True`).
2. `python tools/check_public_source.py` and `python tools/check_docs.py` must print `OK`.
   `bash tools/run_release_gates.sh` is the local fail-closed subset. Org create and public visibility remain operator-owned (not documented in this tree).
3. Do **not** commit `.env`, sqlite DBs, `*.tgz` backups, `labvault_export.json`, or credential/token files.
4. `.gitignore` already excludes secrets, DBs, venv, backups, IDE files, and export dumps.
5. `.gitattributes` forces LF for `.sh` / `.py` so oneshots do not fail with `pipefail\r`.
6. LICENSE is MIT — LabVault contributors.
7. Product name is **LabVault** (not “Keysight LabVault”).
8. Read [CUSTOMER_DISTRIBUTION.md](../distribution/CUSTOMER_DISTRIBUTION.md) and [PRE_DEPLOY_HARDENING.md](../distribution/PRE_DEPLOY_HARDENING.md).

```bash
git status   # review: no .env, no *.sqlite3, no export dumps
python tools/check_public_source.py
# optional local CI
# .github/workflows/ci.yml — run matching jobs locally when possible
```

Remote is the Keysight GitHub org repository your release packet names. Do not force-push `main`.

Corporate approvals, SBOM upload, and signed release tags are tracked separately (submission packet).
