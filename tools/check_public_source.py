#!/usr/bin/env python3
"""Fail closed if excluded customer-SKU surfaces or lab leakage remain."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FORBIDDEN_PATH_RE = re.compile(
    r"(capex_views|hyperview_views|views_laas|snappi_regression|presentation_views|"
    r"seed_google_demo|uhd_bfshell|uhd_fanout_ucli|uhd_connect\.py|uhd_fetch|"
    r"uhd_views|keysight_uhd|uhd-l1-panel|_uhd_l1|_uhd_launchpad|_uhd_peer|"
    r"nginx-eagle|eaglelite|eagleheavy|"
    r"templates/connect/capex|templates/connect/hyperview|"
    r"views_hw_resolve|views_hw_release|"
    r"DEVELOPMENT_CONTEXT|google-demo|DRIVER_SDK\.md|"
    r"docs/install/DEPLOY\.md|"
    r"reference-labvaultvm-nginx)",
    re.I,
)

FORBIDDEN_IMPORT_RE = re.compile(
    r"\b(capex_views|hyperview_views|views_laas_reserve|snappi_regression_views|"
    r"presentation_views|seed_google_demo|uhd_bfshell|uhd_connect|uhd_fetch|"
    r"uhd_views|keysight_uhd|uhd_lab_context|uhd_mode_switch|"
    r"views_hw_resolve|views_hw_release|"
    r"topology_b2b_export)\b"
)

FORBIDDEN_CONTENT_RE = re.compile(
    r"(nginx-eagle\.lbj\.is\.keysight\.com|"
    r"lbj\.is\.keysight\.com|"
    r"artifactorylbj\.it\.keysight\.com|"
    r"/ftp/virtual/ixia|"
    r"id_rsa_ci|"
    r"Ixia4Ixia|"
    r"eaglelite|eagleheavy|EagleLite|EagleHeavy|"
    r"\bHogan\b|"
    r"chassis74|"
    r"calabasas|"
    r"cobalt_fabric|"
    r"LAAS manual reserve|"
    r"/var/run/docker\.sock|"
    r"Dockerfile\.log-agent|"
    r"_GRAPHIFY_OUT|"
    r"from \.views_hw_release|"
    r"from \.views_hw_resolve|"
    r"10\.36\.82\.(74|249)|"
    r"10\.36\.83\.(209|30)|"
    r"VMID\s*\*?\*?100\b|"
    r"google-demo VM|"
    r"rakesh\.kumar@keysight\.com|"
    r"Rakesh Kumar)",
    re.I,
)

# Published customer docs must not advertise the old demo password as the login.
FORBIDDEN_DEMO_LOGIN_RE = re.compile(
    r"`admin`\s*/\s*`labvault!`|admin / labvault!",
)

# Customer-facing files must not embed the internal Keysight lab prefixes.
CUSTOMER_FACING_LAB_IP_RE = re.compile(r"10\.36\.(81|82|83|84)\.")
RUNTIME_LAB_IP_RE = re.compile(r"10\.36\.\d+")
CUSTOMER_FACING_PREFIXES = (
    "docs/",
    "scripts/",
    "resources/",
    "deploy/",
    "connect/templates/",
    "connect/forms.py",
    "connect/fleet_openapi.py",
    "README.md",
    ".env.example",
)

ALLOW_PATH_PREFIXES = (
    "tools/",
    ".git/",
    ".context/",
    "docs/security/HARDENING.md",
    "docs/security/CREDENTIALS.md",
    "docs/getting-started/FIRST_LOGIN.md",
    "SECURITY.md",
    "public-source-manifest.txt",
    "CHANGELOG.md",
    "connect/tests/",
    "connect/migrations/",
)

ALLOW_PATHS = {
    "connect/demo_mode.py",
    "connect/snmp_utils.py",
    "connect/labvault_bootstrap_defaults.py",
}


def _allowed(rel: str) -> bool:
    if rel in ALLOW_PATHS:
        return True
    return any(rel.startswith(p) for p in ALLOW_PATH_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=Path, default=Path("."))
    args = ap.parse_args()
    root = args.target.resolve()
    errors: list[str] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        if "/__pycache__/" in rel or rel.startswith(".venv/") or rel.startswith("staticfiles/") or rel.startswith("data/"):
            continue
        if _allowed(rel):
            continue
        if FORBIDDEN_PATH_RE.search(rel):
            errors.append(f"forbidden path: {rel}")

    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if _allowed(rel) or rel.startswith(".venv/") or rel.startswith("data/"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN_IMPORT_RE.search(text):
            errors.append(f"forbidden import/symbol in {rel}")
        if RUNTIME_LAB_IP_RE.search(text):
            errors.append(f"lab IP in runtime {rel}")

    scan_globs = (
        "*.py", "*.md", "*.html", "*.yml", "*.yaml", "*.sh",
        "*.conf", "*.template", "*.service", "*.example",
        "*.json", "*.txt", "*.csv",
    )
    for pattern in scan_globs:
        for path in root.rglob(pattern):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if _allowed(rel) or "/__pycache__/" in rel or rel.startswith(".venv/") or rel.startswith("data/"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            text_for_sock = text.replace("Do NOT mount /var/run/docker.sock", "").replace("No docker.sock", "")
            m = FORBIDDEN_CONTENT_RE.search(text_for_sock)
            if m:
                if "hard-dumped" in text.lower() or "not included" in text.lower():
                    continue
                errors.append(f"forbidden content {m.group(0)!r} in {rel}")
            if rel.endswith((".md", ".example", ".sh")) and FORBIDDEN_DEMO_LOGIN_RE.search(text):
                lowered = re.sub(r"\*+", "", text).lower()
                if "LABVAULT_DEMO_DEFAULTS" in text and "not the default" in lowered:
                    continue
                errors.append(f"published demo login in {rel}")
            if rel.endswith(".sh") and "login_password=labvault!" in text:
                errors.append(f"published demo login in {rel}")
            if any(rel == p or rel.startswith(p) for p in CUSTOMER_FACING_PREFIXES):
                if CUSTOMER_FACING_LAB_IP_RE.search(text):
                    if rel.startswith("docs/security/") or rel.startswith("docs/install/DEPLOY_MATRIX"):
                        # still fail — these must be sanitized even if other allowlists exist
                        errors.append(f"lab IP in customer-facing {rel}")
                    elif not _allowed(rel):
                        errors.append(f"lab IP in customer-facing {rel}")

    if errors:
        seen: set[str] = set()
        uniq = []
        for e in errors:
            if e not in seen:
                seen.add(e)
                uniq.append(e)
        print("FAIL")
        for e in uniq[:120]:
            print(e)
        return 1
    print("OK — public source checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
