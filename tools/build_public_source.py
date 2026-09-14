#!/usr/bin/env python3
"""Build an allowlisted clean-room LabVault public-source tree.

Usage:
  python tools/build_public_source.py --source /opt/LabVault --target /root/Apps/labvault-public
  python tools/check_public_source.py --target /root/Apps/labvault-public
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".cache",
    "staticfiles",
    "media",
    "var",
    "secrets",
    "node_modules",
    "agent-research",
    "graphify-out",
}

EXCLUDE_FILE_GLOBS = {
    ".env",
    "db.sqlite3",
    "hyperview_db.sqlite3",
    "np_timeseries.sqlite3",
    "np_timeseries.sqlite3-shm",
    "np_timeseries.sqlite3-wal",
    "gunicorn.ctl",
}

# Paths removed after copy (hard-dump / internal-only). Relative to target root.
HARD_DUMP_PATHS = [
    # Capex
    "connect/capex_views.py",
    "connect/capex_ui_prefs.py",
    "connect/capex_list_filters.py",
    "connect/capex_priority.py",
    "connect/capex_split.py",
    "connect/capex_audit.py",
    "connect/templatetags/capex_tags.py",
    "connect/templates/connect/capex",
    "connect/static/js/capex_column_profiles.classic.js",
    "connect/static/js/capex_column_profiles.js",
    "connect/static/js/capex_columns_config.js",
    "docs/capex",
    "docs/CAPEX_TBD_PRIORITY_INLINE_PLAN.md",
    # Hyperview / SNMP / BACnet
    "connect/hyperview_views.py",
    "connect/hyperviewhq_client.py",
    "connect/hyperview_associations.py",
    "connect/hyperview_discovery_utils.py",
    "connect/snmp_utils.py",
    "connect/snmp_service.py",
    "connect/bacnet_utils.py",
    "connect/templates/connect/hyperview",
    "docs/hyperview_external_sync.md",
    # LAAS
    "connect/views_laas_reserve.py",
    "connect/hw_assignment_reserve.py",
    "connect/views_hw_resolve.py",
    "connect/views_hw_release.py",
    "connect/views_ocs_xconnect.py",
    "connect/templates/connect/laas_reserve_wizard.html",
    "docs/LABVAULT_LAAS_CONNECTIVITY.md",
    "docs/B2B_LaaS_PYTHAR_INTEGRATION_PLAN.md",
    "docs/ARISTA3_OCS_ARESONE05_LAAS_PILOT.md",
    # AI / Snappi / Demo
    "connect/snappi_regression_views.py",
    "connect/snappi_regression_proxy.py",
    "connect/presentation_views.py",
    "connect/demo_nav.py",
    "connect/demo_mode.py",
    "connect/templates/connect/ai_nexus.html",
    "connect/templates/connect/snappi_regression",
    "connect/templates/connect/includes/demo_launchpad.html",
    "connect/templates/connect/includes/demo_sidebar_links.html",
    "docs/LabVault-Presentation.html",
    "docs/LabVault-Presentation-v2.html",
    "docs/DEMO-RUNBOOK.html",
    "docs/GOOGLE_DEMO_PLAN.md",
    "docs/google-demo",
    "deploy/google-demo",
    # UHD shell / nginx-eagle launchpad
    "connect/keysight_drivers/uhd_bfshell.py",
    "connect/keysight_drivers/uhd_bfshell_eagle.py",
    "connect/keysight_drivers/uhd_fanout_ucli.py",
    "connect/uhd_launchpad.py",
    "deploy/nginx-eagle",
    "docs/nginx-eagle",
    "docs/uhd/UCLI_USER_GUIDE.md",
    "docs/uhd/capability-matrix-chassis74.md",
    "scripts/nginx-eagle-ssh-bootstrap.sh",
    # Internal deploy / identity
    "DEPLOY.md",
    "README_lab_requirements.md",
    "watchdog.py",
]

# Management commands to drop (prefix match under connect/management/commands/)
HARD_DUMP_CMD_PREFIXES = (
    "capex_",
    "import_capex",
    "export_capex",
    "ensure_capex",
    "hyperview",
    "hyperviewhq",
    "snmp_",
    "bacnet_",
    "seed_google_demo",
)

HARD_DUMP_NAME_RE = re.compile(
    r"(capex|hyperview|laas|snappi|ai.?nexus|presentation|seed_google|"
    r"uhd_bfshell|uhd_fanout_ucli|nginx-eagle|demo_mode|demo_nav)",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _should_skip(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    name = rel.name
    if name in EXCLUDE_FILE_GLOBS:
        return True
    if name.endswith((".pyc", ".pyo", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")):
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return False


def copy_tree(source: Path, target: Path) -> list[str]:
    copied: list[str] = []
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for root, dirs, files in os.walk(source):
        root_p = Path(root)
        rel_root = root_p.relative_to(source)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES and not _should_skip(rel_root / d)]
        for name in files:
            rel = rel_root / name
            if _should_skip(rel):
                continue
            src = root_p / name
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(str(rel).replace("\\", "/"))
    return copied


def hard_dump(target: Path) -> list[str]:
    removed: list[str] = []
    for rel in HARD_DUMP_PATHS:
        path = target / rel
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(rel + "/")
        elif path.is_file():
            path.unlink()
            removed.append(rel)

    cmd_dir = target / "connect" / "management" / "commands"
    if cmd_dir.is_dir():
        for fp in list(cmd_dir.glob("*.py")):
            if fp.name == "__init__.py":
                continue
            if fp.name.startswith(HARD_DUMP_CMD_PREFIXES) or HARD_DUMP_NAME_RE.search(fp.name):
                fp.unlink()
                removed.append(str(fp.relative_to(target)).replace("\\", "/"))

    # Drop excluded tests
    for pattern in ("**/test*capex*", "**/test*hyperview*", "**/test*snappi*", "**/test*laas*", "**/test*uhd_fanout*"):
        for fp in target.glob(pattern):
            if fp.is_file():
                fp.unlink()
                removed.append(str(fp.relative_to(target)).replace("\\", "/"))
    return removed


def write_manifest(target: Path, source: Path, copied: list[str], removed: list[str]) -> Path:
    tools = target / "tools"
    tools.mkdir(exist_ok=True)
    manifest = {
        "generated_at": _now(),
        "source": str(source),
        "target": str(target),
        "copied_count": len(copied),
        "removed_count": len(removed),
        "removed": sorted(removed),
    }
    path = target / "public-source-manifest.txt"
    lines = [
        f"# LabVault public-source manifest",
        f"# generated_at={manifest['generated_at']}",
        f"# source={source}",
        f"# copied={len(copied)} removed={len(removed)}",
        "",
        "## removed",
        *sorted(removed),
        "",
        "## note",
        "Internal SOURCE_PROVENANCE.md stays in the private release-evidence bundle.",
        "Public RELEASE_PROVENANCE.md is generated separately at release time.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tools / "public_source_build.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def write_private_provenance(source: Path, target: Path, evidence_dir: Path) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        branch = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain"], text=True
        )
    except Exception as exc:  # noqa: BLE001
        commit, branch, status = "unknown", "unknown", str(exc)

    text = f"""# SOURCE_PROVENANCE (internal — do not publish)

Generated: {_now()}
Source tree: `{source}`
Public staging: `{target}`
Source branch: `{branch}`
Source commit: `{commit}`

## Working tree status at export

```
{status or '(clean)'}
```

## Policy

- Public history is a new root commit from the staging tree only.
- Never publish this file, internal remotes, or Capex/Hyperview/LAAS/demo branches.
"""
    out = evidence_dir / "SOURCE_PROVENANCE.md"
    out.write_text(text, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=Path("/opt/LabVault"))
    ap.add_argument("--target", type=Path, default=Path("/root/Apps/labvault-public"))
    ap.add_argument(
        "--evidence",
        type=Path,
        default=Path("/var/lib/labvault-release-evidence"),
    )
    args = ap.parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    if not source.is_dir():
        print(f"source missing: {source}", file=sys.stderr)
        return 2

    copied = copy_tree(source, target)
    removed = hard_dump(target)
    write_manifest(target, source, copied, removed)
    write_private_provenance(source, target, args.evidence)

    # Ensure tools scripts are present in target (copied if under source/tools)
    tools_src = Path(__file__).resolve().parent
    tools_dst = target / "tools"
    tools_dst.mkdir(exist_ok=True)
    for name in ("build_public_source.py", "check_public_source.py"):
        src = tools_src / name
        if src.is_file():
            shutil.copy2(src, tools_dst / name)

    print(
        json.dumps(
            {
                "ok": True,
                "target": str(target),
                "copied": len(copied),
                "removed": len(removed),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
