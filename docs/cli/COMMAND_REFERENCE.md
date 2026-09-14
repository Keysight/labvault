# CLI command reference

Authoritative table: [LABVAULT_CLI.md](LABVAULT_CLI.md).

Service capability matrix: [SERVICE_CONTROL.md](SERVICE_CONTROL.md).

### Example invoke (session + CSRF)

```bash
# After logging in with cookie jar:
TOKEN=$(grep csrftoken cj | awk '{print $NF}')
curl -b cj -c cj -H "X-CSRFToken: $TOKEN" -H "Content-Type: application/json" \
  -H "Referer: http://127.0.0.1:8000/cli/" \
  -d '{"line":"whoami"}' \
  http://127.0.0.1:8000/api/cli/v1/invoke/
```

Mutating flow:

1. `POST /api/cli/v1/confirm/` with `{"line":"service restart heartbeat reason=\"x\""}`
2. `POST /api/cli/v1/invoke/` with same line + `confirmation_nonce`

Prefer Fleet Bearer APIs for unattended automation ([../api/OPENAPI.md](../api/OPENAPI.md)).
Primary operator path remains `ssh -p 2222`.
