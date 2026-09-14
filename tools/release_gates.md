# Public Keysight release gates

Binary checklist before requesting `Keysight/labvault` creation / public push:

1. Corporate IP/patent/export/license/trademark approvals recorded (`tools/release_packet/CORPORATE_SUBMISSION.md`)
2. `python tools/check_public_source.py` and `python tools/check_docs.py` pass
3. Secret/PII scan clean on new history (`gitleaks` via `tools/run_security_scans.sh`)
4. SBOM + `pip-audit` + image scan attached
5. Compose + systemd fresh install smoke READY
6. Backup/restore `--drill` pass
7. 24h empty-lab soak
8. Private staging GitHub rehearsal green

Local fail-closed subset: `bash tools/run_release_gates.sh`.

Public push is the final action after this packet is approved — not how hardening is performed.

See `tools/release_packet/STAGING_BLOCKERS.md`.
