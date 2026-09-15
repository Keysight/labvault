# Release process

1. `python tools/check_public_source.py` and `python tools/check_docs.py`
2. `bash tools/run_release_gates.sh`
3. `bash tools/generate_sbom.sh` and `bash tools/run_security_scans.sh`
4. `./deploy/install/build-wheelhouse.sh` for airgap consumers
5. Corporate / legal approvals and staging soak are operator-owned (not in this tree)
6. After approval: signed `vX.Y.Z` tag, attach SBOM + SHA-256
7. Customers follow [../install/UPGRADE.md](../install/UPGRADE.md)
