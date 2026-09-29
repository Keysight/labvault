# `scripts/`

Developer and agent helpers. Nothing here is used by the running appliance or the
installers.

| File | Purpose |
|---|---|
| `context_pack.py` | Harvests local Cursor chat digests, plans, and research notes into a target repo's `.context/` directory (redacting password/token/PEM strings) and optionally runs graphify. `python3 scripts/context_pack.py --target <repo> [--graphify]`. `.context/` is gitignored for the public repository |

Install and deploy scripts live in [`../deploy/`](../deploy/README.md); release gates live in
[`../tools/`](../tools/README.md).
