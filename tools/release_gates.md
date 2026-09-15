# Local release gates

Fail-closed checks that ship with the customer SKU:

```bash
python tools/check_public_source.py
python tools/check_docs.py
bash tools/run_release_gates.sh
```

That subset is what CI runs. Corporate approvals, staging soak, signed tags, and a public visibility flip are operator-owned and are **not** in this repository.
