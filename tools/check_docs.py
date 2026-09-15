#!/usr/bin/env python3
"""Require the customer documentation map and reject internal runbooks."""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED = [
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/INDEX.md",
    "docs/getting-started/QUICKSTART.md",
    "docs/getting-started/FIRST_LOGIN.md",
    "docs/getting-started/CONCEPTS.md",
    "docs/getting-started/FIRST_LAB.md",
    "docs/install/REQUIREMENTS.md",
    "docs/install/BARE_METAL.md",
    "docs/install/DOCKER.md",
    "docs/install/PROXMOX.md",
    "docs/install/AIRGAP.md",
    "docs/install/CONFIGURATION.md",
    "docs/install/TLS.md",
    "docs/install/LDAP.md",
    "docs/install/UPGRADE.md",
    "docs/install/ROLLBACK.md",
    "docs/install/UNINSTALL.md",
    "docs/user/INVENTORY.md",
    "docs/user/DEVICES.md",
    "docs/user/KEYSIGHT_CHASSIS.md",
    "docs/user/TOPOLOGY.md",
    "docs/user/FABRIC.md",
    "docs/user/RESERVATIONS.md",
    "docs/user/INSIGHTS.md",
    "docs/user/REPORTS.md",
    "docs/user/ALERTS.md",
    "docs/user/AUDIT.md",
    "docs/admin/ADMINISTRATION.md",
    "docs/admin/USERS_AND_ROLES.md",
    "docs/admin/RUNTIME_SETTINGS.md",
    "docs/admin/SERVICES.md",
    "docs/admin/BACKUP_RESTORE.md",
    "docs/admin/MONITORING.md",
    "docs/admin/TROUBLESHOOTING.md",
    "docs/admin/CAPACITY.md",
    "docs/cli/LABVAULT_CLI.md",
    "docs/cli/COMMAND_REFERENCE.md",
    "docs/cli/SERVICE_CONTROL.md",
    "docs/cli/AUTOMATION.md",
    "docs/api/API_AUTH.md",
    "docs/api/OPENAPI.md",
    "docs/security/HARDENING.md",
    "docs/security/THREAT_MODEL.md",
    "docs/security/NETWORK_PORTS.md",
    "docs/security/CREDENTIALS.md",
    "docs/security/VULNERABILITY_REPORTING.md",
    "docs/development/ARCHITECTURE.md",
    "docs/development/DEVELOPMENT.md",
    "docs/development/TESTING.md",
    "docs/development/RELEASE_PROCESS.md",
]

FORBIDDEN = [
    "docs/install/DEPLOY.md",
    "docs/NP_TIMESERIES_DEPLOY.md",
    "docs/usage-graph-views.md",
    "docs/PA7080_Eagle_nnIpv4_VR_testpath_SSL_SR_proxy.csv",
    "docs/1593619662_9b1ce8335a274f7fb2c67c6171f8e2c8-300326-0049-60.pdf",
    "docs/[External] GGN EngProd & Keysight Requirements_ Top Priorities (Q3 2026).docx",
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    missing = [p for p in REQUIRED if not (root / p).is_file()]
    present_forbidden = [p for p in FORBIDDEN if (root / p).exists()]
    if missing or present_forbidden:
        print("FAIL")
        for p in missing:
            print(f"missing {p}")
        for p in present_forbidden:
            print(f"forbidden {p}")
        return 1
    print("OK — docs map present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
