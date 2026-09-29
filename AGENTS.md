# LabVault customer SKU — agent notes

## Code map (read before changing a subsystem)

[docs/development/ARCHITECTURE.md](docs/development/ARCHITECTURE.md) — processes, databases, caches.
[docs/development/DATA_FLOW.md](docs/development/DATA_FLOW.md) — device page, chassis, topology, metrics, CLI.
[docs/development/EXTENDING.md](docs/development/EXTENDING.md) — where to add a page, driver, fleet endpoint, CLI verb, or metric.
Per-area guides: [docs/development/subsystems/](docs/development/subsystems/).
App file list: [connect/README.md](connect/README.md).
Documentation index: [docs/INDEX.md](docs/INDEX.md).

## Mandatory checks

1. `docs/getting-started/FIRST_LOGIN.md` — first login comes from the credential file written at install.
2. `python tools/check_public_source.py` — fail closed on excluded surfaces and lab leakage.
3. `python tools/check_docs.py` — required docs present, no internal runbooks.

## Safety

- Product name is **LabVault** only.
- Do not commit `.env`, SQLite databases, export dumps, credential files, or private keys.
- Do not add Capex, Hyperview, LAAS, AI Nexus, Snappi, Demo Stage, or UHD hardware.
- Free-form device shells (`device_terminal`, `api_execute_command`) must stay absent.
- Do not commit inventory, addresses, or credentials from any private lab.

## Quick regress

```bash
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
python manage.py test connect.tests.test_hard_dump connect.tests.test_bootstrap_defaults --verbosity=1
python tools/check_public_source.py
python tools/check_docs.py
```
