# Keysight LabVault repository packet

Start **private**. Flip to public only after internal review and recorded
approvals. This file is a checklist — it does not record approval by itself.
Do not invent ticket IDs.

## Repository request

- Proposed name: `labvault` (private staging first; public later)
- Visibility: **private** until internal review; then public
- Description: Lab infrastructure inventory, topology, reservation, telemetry, and operations platform.
- Default branch: `main`; releases from signed `vX.Y.Z` tags
- Requested features: Issues, private vulnerability reporting, Actions, Dependabot, secret scanning, CodeQL

Email to `pdl-public-github-repos@keysight.com` is **optional** if you already
control a Keysight (or rehearsal) GitHub org and will keep the repo private
until review. Do not `gh repo create Keysight/labvault --public` from this host.

## Corporate approvals (blocking for *public*)

Record ticket IDs here when obtained:

- [ ] Manager / product-owner release approval — ID:
- [ ] IP / right-to-use — ID:
- [ ] Patent — ID:
- [ ] Trademark / branding — ID:
- [ ] Privacy — ID:
- [ ] Export-control — ID:
- [ ] Security review — ID:
- [ ] Third-party license / SBOM review — ID:
- [ ] MIT license confirmation (or stop and follow the approved license) — ID:

Private staging can proceed with empty IDs. Public visibility must not.

## What legal must answer (MIT / open source)

`LICENSE` is already MIT text. That only licenses **this** source if Keysight
has the right to publish it that way. A source scan cannot issue that right.

Ask legal/IP (ServiceNow / Jira / your IP counsel — use the real Keysight
process) these questions, then paste the ticket ID on the MIT line above:

1. **Right to publish** — Is LabVault Keysight-owned (or otherwise cleared) so
   the company may release this tree under MIT? The current copyright line is
   `Copyright (c) 2026 LabVault contributors`, not Keysight, Inc. Confirm the
   correct copyright holder.
2. **Product API clients** — `connect/keysight_drivers/` (IxOS, KCOS, UHD
   Connect, BPS) are original Python wrappers around appliance REST/OpenAPI.
   Confirm that publishing those clients is allowed (not a Keysight SDK extract,
   NDA sample, or unpublished protocol).
3. **Trademarks** — MIT does not grant trademark rights. Confirm use of
   Keysight, Ixia, IxOS, KCOS, AresONE, BreakingPoint, Novus, etc. in UI/docs.
4. **Third-party stack** — Confirm the lockfile + CDN notices are acceptable
   next to an MIT project (see license notes below). Attach `requirements.lock`
   and `THIRD_PARTY_NOTICES.md`.
5. **Export / crypto** — The tree depends on `cryptography`, `paramiko`,
   `asyncssh`, `PyNaCl`, `bcrypt` (TLS/SSH). Confirm export-control review.
6. **External contributions** — `CONTRIBUTING.md` already holds PRs until a
   DCO/CLA decision. Confirm that policy before public.

If legal rejects MIT, stop and use the license they specify. Do not keep MIT
text in `LICENSE` after a rejection.

## Technical license notes (not a legal opinion)

Runtime Python deps in `requirements.lock` are OSI-style licenses. None of the
resolved packages is a known proprietary/commercial library.

Compatible-as-**dependencies** of an MIT app (you do **not** relicense them as MIT):

| Package | License | Note |
|---------|---------|------|
| Django, asgiref, sqlparse, idna, psutil, … | BSD / MIT / Apache-2.0 | Typical permissive |
| paramiko | LGPL-2.1 | Copyleft library; keep notices; do not vendor-and-relicense |
| psycopg2-binary | LGPL-3.0 with exceptions | Same |
| asyncssh | EPL-2.0 OR GPL-2.0-or-later | File-level copyleft; keep notices |
| certifi | MPL-2.0 | CA bundle; keep notices |
| python-ldap | PSF-style / project license | Confirm from the wheel METADATA at release |
| requests-unixsocket | metadata incomplete | Transitive; confirm at SBOM time |

UI loaded from CDN / vendored (add to `THIRD_PARTY_NOTICES.md` before public):

| Component | License (typical) |
|-----------|-------------------|
| Bootstrap 5.3 (jsDelivr) | MIT |
| Font Awesome 6.4 Free (cdnjs) | Code MIT; fonts SIL OFL; icons CC-BY-4.0 |
| D3 v7, dagre 0.8.5 (jsDelivr) | ISC / MIT |
| swagger-ui-dist 5.11 (unpkg) | Apache-2.0 |
| jQuery 3.6.0 (vendored) | MIT |
| Bootstrap 5.1.3 CSS (vendored `bootstrap.min.css`) | MIT |

`bash tools/generate_sbom.sh` plus `pip-audit` is the release artifact. The
table in `THIRD_PARTY_NOTICES.md` is incomplete vs the lockfile — update it
before public.

## Attachments

- Hard-dump / public-source check output
- `gitleaks` / secret scan on the clean-room history
- SBOM + `pip-audit` / image scan
- Fresh install + restore + soak reports
- Sanitized screenshots

## Governance

- Org team for `.github/CODEOWNERS` (not personal-only ownership)
- Branch protection: PR required, CODEOWNERS review, signed commits/tags, required CI/security checks
- External PRs reviewed only after Keysight DCO/CLA decision
- Support: community / best-effort unless Keysight approves an SLA
