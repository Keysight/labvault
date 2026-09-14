# Operator-owned release blockers

These cannot be completed from the customer source tree alone.
Step-by-step commands: [OPERATOR_FINISH.md](OPERATOR_FINISH.md).

1. **Private GitHub repo** — create only after working-tree review; keep private until internal review. Public flip is last.
2. **MIT (or alternate) license approval** — LICENSE is MIT text; a real legal/IP ticket ID is required before public. See questions in `CORPORATE_SUBMISSION.md`.
3. **Private staging rehearsal** — enable Actions/branch rules, run the deploy matrix on a non-production host.
4. **24h empty-lab soak** — run on staging; attach the report using `SOAK_RESTORE_TEMPLATE.md`.
5. **Signed tags / images** — Cosign or org signing after the staging repo exists.
6. **Public launch** — push only after the corporate packet is approved.

## Finished in-tree (do not block on these anymore)

- Runtime `10.36.*` maps stripped from driver/view/command Python
- Shipped `resources/` examples are TEST-NET; HBG/Eagle dumps deleted
- UHD hardware / bfshell / ucli removed; no IxOS/KCOS CI keys or baked root passwords
- `topology_b2b_export.py` removed; LaaS routes/nav stay dumped
- Wheelhouse `|| true` removed; host-dep / python-ldap skips are explicit env only

Local fail-closed subset: `bash tools/run_release_gates.sh`.
