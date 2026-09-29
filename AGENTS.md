# LabVault customer SKU — agent notes

## Context first (mandatory)

1. `.context/INDEX.md`
2. `.context/session/SESSION_BRIEF.md`
3. `.context/memory/OPERATOR_FACTS.md`
4. `.context/session/chats/INDEX.md` and `.context/plans/INDEX.md`
5. `graphify-out/GRAPH_REPORT.md` or `graphify-out/wiki/index.md` (if present)

Refresh: `python3 scripts/context_pack.py --target /root/Apps/labvault-public --graphify`

This tree is the **customer SKU** (Capex, Hyperview, LAAS, AI Nexus, Snappi, Demo Stage, and UHD hardware are omitted).

## Code map (read before changing a subsystem)

[docs/development/ARCHITECTURE.md](docs/development/ARCHITECTURE.md) — processes, databases, caches.
[docs/development/DATA_FLOW.md](docs/development/DATA_FLOW.md) — device page, chassis, topology, metrics, CLI.
[docs/development/EXTENDING.md](docs/development/EXTENDING.md) — where to add a page, driver, fleet endpoint, CLI verb, or metric.
Per-area guides: [docs/development/subsystems/](docs/development/subsystems/).
App file list: [connect/README.md](connect/README.md).

## Mandatory checks

1. `docs/INDEX.md` — documentation map
2. `docs/getting-started/FIRST_LOGIN.md` — first login is the credential file, not a published password
3. `python tools/check_public_source.py` — fail closed on excluded surfaces and lab leakage
4. `python tools/check_docs.py` — required docs present, no internal runbooks

## Do not mix environments

| Tree | Role |
|------|------|
| This repository | Public/customer source |
| Internal production hosts | Private; never commit their inventory, IPs, or credentials here |

## Safety

- Product name is **LabVault** only
- Do not commit `.env`, SQLite DBs, export dumps, or credential files
- Do not add Capex / Hyperview / LAAS / AI Nexus / Snappi / Demo Stage surfaces
- Free-form device shells (`device_terminal`, `api_execute_command`) must stay absent

## Quick regress

```bash
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
python manage.py test connect.tests.test_hard_dump connect.tests.test_bootstrap_defaults --verbosity=1
python tools/check_public_source.py
python tools/check_docs.py
```
