# Release process

1. `python tools/check_public_source.py` and `python tools/check_docs.py`
2. `bash tools/run_release_gates.sh`
3. `bash tools/generate_sbom.sh` and `bash tools/run_security_scans.sh`
4. `./deploy/install/build-wheelhouse.sh` for airgap consumers
5. Fill [../../tools/release_packet/CORPORATE_SUBMISSION.md](../../tools/release_packet/CORPORATE_SUBMISSION.md)
6. Private staging rehearsal, soak, restore — [../../tools/release_packet/OPERATOR_FINISH.md](../../tools/release_packet/OPERATOR_FINISH.md)
7. After Keysight approval: signed `vX.Y.Z` tag, attach SBOM + SHA-256
8. Customers follow [../install/UPGRADE.md](../install/UPGRADE.md)
