# Security Policy

## Supported versions

Security fixes are published for the latest signed `vX.Y.Z` release of this LabVault customer SKU.

## Reporting a vulnerability

Report undisclosed vulnerabilities through GitHub private vulnerability reporting
on this repository. Do not include lab addresses, passwords, or tokens in the report.

Do not file public issues for undisclosed vulnerabilities.

## Hardening summary (customer SKU)

- No `docker.sock`; ops via `labvault-opsd` allowlist only
- Startup `validate_external_config` rejects demo secrets, `ALLOWED_HOSTS=*`, DEBUG
- `/health/live` and `/health/ready` expose no secrets or lab inventory
- Staff-only LabVault CLI; no free-form device shell / host PTY
- Default compose DB password `labvault` is for lab installers only — change before shared use
- Oneshot UI/API bootstrap is `admin` / `labvault!` (`LABVAULT_DEMO_DEFAULTS=1`). A random file-only password is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`. Rotate on shared hosts.
