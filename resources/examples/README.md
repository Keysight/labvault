# Example LabVault datasets

These are `labvault-full-export` v1 JSON files for **DATA → Import**, not `labvaultctl` appliance backups.

| File | Use |
|------|-----|
| [dc8-pickup-export.json](dc8-pickup-export.json) | 8 AresONE + 4 Arista + 1 OCS + one topology. **Edit IPs and passwords first.** |
| [empty-labvault-export.json](empty-labvault-export.json) | Empty schema (no inventory). |

**What to change, how to import, and what goes live:** [docs/getting-started/DC8_PICKUP.md](../../docs/getting-started/DC8_PICKUP.md).

Placeholders are RFC 5737 (`192.0.2.*`) and `changeme` / `admin`. Do not add a live-lab copy to this folder.
