# Runtime settings

DB-backed `RuntimeSetting` values control feature toggles and worker behavior.

| CLI | `settings list`, `settings set` (mutating, confirm) |
| Default | Workers **idle** until explicitly enabled |

Changing live collection affects chassis load — enable in controlled windows.

API tokens are **not** runtime settings. Create/revoke them at `/settings/` → **API Tokens**. First-login rotation: [getting-started/FIRST_LOGIN.md](../getting-started/FIRST_LOGIN.md).
