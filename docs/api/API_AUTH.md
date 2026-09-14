# API authentication

| Client | Auth |
|--------|------|
| Browser UI / CLI APIs | Session cookie + CSRF |
| Fleet JSON | Bearer API token |

Send `Authorization: Bearer <token>` on `/api/fleet/*`. Missing/invalid token → 401.

## Install token (rotate on first login)

Oneshot sets `LABVAULT_DEMO_DEFAULTS=1` so token **name** is `demo-api` and **value** is `labvault-default-api-token` (also written to `/var/lib/labvault/fleet-token`). A random file-only token is **not** the default unless `LABVAULT_BOOTSTRAP_RANDOM=1`.

Rotate before sharing the host. Steps: [getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md) §2.

Short version:

1. Log in at `/login/`.
2. Open `/settings/` → **API Tokens**.
3. **Generate** a new named token and copy the full value from the success banner.
4. **Revoke** the `demo-api` row.
5. Update `/var/lib/labvault/fleet-token` and any scripts (`curl -H "Authorization: Bearer …"`).

Prefer a dedicated token per integration. Install-time override: `LABVAULT_DEMO_API_TOKEN` or `LABVAULT_FLEET_TOKEN`.
