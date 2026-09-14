# How to finish the LabVault customer SKU delivery

Work from `/root/Apps/labvault-public` on `labvaultvm`. Do **not** treat
`/opt/LabVault` as this tree. Production stays on `google-demo`.

Source gates can finish here. Private staging, 24h soak, signed tags, and
making the repo public cannot. Create the private remote **only after** the
working-tree review below.

## Already finished in this customer SKU

- Local fail-closed gates: `bash tools/run_release_gates.sh`
- Runtime `10.36.*` lab maps removed from driver/view/command Python
- Shipped `resources/` examples use TEST-NET; HBG/Eagle dumps deleted
- `topology_b2b_export.py` deleted; LaaS summary/manifest stay 404
- Wheelhouse host-dep / `python-ldap` wheels fail closed unless skip env is set

## Operator order (updated)

### 0. Review, then commit (no remote yet)

See the review buckets in chat / this file. When you ask for the commit:

```bash
cd /root/Apps/labvault-public
python3 tools/check_public_source.py --target .
bash tools/run_release_gates.sh
git add -A
git diff --cached --stat
# commit only when requested
```

Do not invent ticket IDs in `CORPORATE_SUBMISSION.md`.

### 1. Private staging repo (after review)

Keep it private. After internal review you can make the same repo public.
Email to `pdl-public-github-repos@keysight.com` is optional if you already
have an org you control.

```bash
cd /root/Apps/labvault-public
gh repo create YOUR_ORG/labvault --private --source=. --remote=origin --disable-wiki
git push -u origin main
```

Do **not** `gh repo create Keysight/labvault --public`.

### 2. MIT / legal ticket (before public)

Open a real Keysight legal/IP ticket with the questions in
`CORPORATE_SUBMISSION.md`. Paste the ID on the MIT line. Private staging
may start with an empty ID; public must not.

### 3. Staging host rehearsal

On a **non-production** host (not `/opt/LabVault` on this VM unless you
explicitly accept that risk):

```bash
sudo ./deploy/install/oneshot-compose.sh    # or oneshot-systemd.sh
./deploy/scripts/post_deploy_verify.sh
./labvaultctl backup
./labvaultctl restore --drill <backup-dir>
```

Actions: `ci.yml`, `security.yml`, `release.yml` green.

### 4. 24h empty-lab soak

Fill [SOAK_RESTORE_TEMPLATE.md](SOAK_RESTORE_TEMPLATE.md).

### 5. Signed tag + SBOM (on the private repo first)

```bash
git tag -s v0.1.0 -m "LabVault customer SKU v0.1.0"
git push origin v0.1.0
bash tools/generate_sbom.sh
```

### 6. Make public last

Only after the packet IDs are filled and soak is attached: GitHub
Settings → Change repository visibility → Public. No force-push to `main`.

## Review leftovers (decide before commit)

- `THIRD_PARTY_NOTICES.md` incomplete vs `requirements.lock` + CDN libs
- Pulse Instant / opsd / nginx port-80 work is mixed into the same dirty tree
- `hbg` preset IDs are kept on purpose

## Local verify

```bash
python3 tools/check_public_source.py --target .
python3 tools/check_docs.py
bash tools/run_release_gates.sh
```
