# API authentication

| Client | Auth |
|--------|------|
| Browser UI / CLI APIs | Session cookie + CSRF |
| Fleet JSON | Bearer API token |

Send `Authorization: Bearer <token>` on `/api/fleet/*`. Missing/invalid token → 401.

## Install token (rotate on first login)

Oneshot seeds token **name** `demo-api`. The **value** is random and written to `/var/lib/labvault/fleet-token` unless `LABVAULT_DEMO_DEFAULTS=1`, which uses the published demo value `labvault-default-api-token`.

Rotate before sharing the host. Steps: [getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md) §2.

Short version:

1. Log in at `/login/`.
2. Open `/settings/` → **API Tokens**.
3. **Generate** a new named token and copy the full value from the success banner.
4. **Revoke** the `demo-api` row.
5. Update `/var/lib/labvault/fleet-token` and any scripts (`curl -H "Authorization: Bearer …"`).

Prefer a dedicated token per integration. Install-time override: `LABVAULT_DEMO_API_TOKEN` or `LABVAULT_FLEET_TOKEN`.
