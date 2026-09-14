# Security Policy

## Supported versions

Security fixes are published for the latest signed `vX.Y.Z` release of this LabVault customer SKU.

## Reporting a vulnerability

Prefer private disclosure: email the security contact published with your release packet,
or use the repository’s private vulnerability reporting feature when enabled.

Do not file public issues for undisclosed vulnerabilities.

## Hardening summary (customer SKU)

- No `docker.sock`; ops via `labvault-opsd` allowlist only
- Startup `validate_external_config` rejects demo secrets, `ALLOWED_HOSTS=*`, DEBUG
- `/health/live` and `/health/ready` expose no secrets or lab inventory
- Staff-only LabVault CLI; no free-form device shell / host PTY
- Capex / Hyperview / LAAS / AI Nexus / Snappi / Demo Stage are hard-dumped from this tree
- Default compose DB password `labvault` is for lab installers only — change before shared use
- Random UI/API bootstrap secrets are the default; published demo logins are not the default (`LABVAULT_DEMO_DEFAULTS=1` only)
