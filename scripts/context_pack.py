#!/usr/bin/env python3
"""Harvest Cursor chat/plan/research context into a target repo's .context/ pack.

Usage:
  python3 scripts/context_pack.py --target /root/Apps/labvault-public --graphify
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CURSOR_PROJECT = "opt-labvault"
DEFAULT_TRANSCRIPT = "bcd4a53b-643e-40be-8e74-f0fe31a43a40"

# Every LabVault-related Cursor plan on this host, including historical UHD work.
PLAN_GLOBS = (
    "customer_github_deploy*.plan.md",
    "github_distribution_strip*.plan.md",
    "google_demo_labvault_fork*.plan.md",
    "pre-deploy_hardening*.plan.md",
    "pre-deploy_platform_hardening*.plan.md",
    "first-deploy_insights*.plan.md",
    "labvault*.plan.md",
    "uhd_*.plan.md",
    "ocs_*.plan.md",
    "ccu_*.plan.md",
    "web_cli_*.plan.md",
    "install_cursor_loop*.plan.md",
)

# Extra Cursor projects that also hold LabVault SKU chats.
EXTRA_PROJECTS = ("root-Apps-labvault-public",)

# In-repo / production docs that are durable knowledge (not runtime trees).
REFERENCE_FILES = (
    ("/opt/labvault/docs/DEVELOPMENT_CONTEXT.md", "DEVELOPMENT_CONTEXT.md"),
    ("/opt/labvault/docs/GOOGLE_DEMO_PLAN.md", "GOOGLE_DEMO_PLAN.md"),
    (
        "/opt/labvault/docs/plans/2026-07-28-india-ccu-labvault-power-control.md",
        "CCU_POWER_CONTROL.md",
    ),
    ("/opt/labvault/docs/uhd/UCLI_USER_GUIDE.md", "uhd/UCLI_USER_GUIDE.md"),
)

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S | re.I)
TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.S | re.I)
IMAGE_FILES_RE = re.compile(r"<image_files>.*?</image_files>", re.S | re.I)
IMAGE_TOKEN_RE = re.compile(r"\[Image\]", re.I)
ANGLE_TAG_RE = re.compile(r"</?[^>]+>")
SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b\s*[:=]\s*\S+"
)
PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: str) -> str:
    text = PEM_RE.sub("[REDACTED_PEM]", text)
    text = SECRET_RE.sub(r"\1=[REDACTED]", text)
    return text


def _clean_text(text: str) -> str:
    text = IMAGE_FILES_RE.sub(" ", text)
    text = IMAGE_TOKEN_RE.sub(" ", text)
    text = TIMESTAMP_RE.sub(" ", text)
    queries = USER_QUERY_RE.findall(text)
    if queries:
        text = "\n\n".join(q.strip() for q in queries if q.strip())
    text = ANGLE_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _redact(text)


def _message_text(obj: dict[str, Any]) -> str:
    msg = obj.get("message") or {}
    parts: list[str] = []
    if isinstance(msg, dict):
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                raw = str(block.get("text") or "").strip()
                if raw:
                    parts.append(raw)
    elif isinstance(msg, str) and msg.strip():
        parts.append(msg.strip())
    return _clean_text("\n".join(parts))


def _read_jsonl_messages(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.is_file():
        return rows
    prev = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role") or ""
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(obj)
        if not text:
            continue
        if text == prev:
            continue
        prev = text
        rows.append({"role": role, "text": text})
    return rows


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _digest_one_session(
    *,
    project: str,
    transcript_dir: Path,
    out_path: Path,
    user_limit: int | None = None,
    assistant_limit: int = 30,
    max_chars: int = 900,
) -> dict[str, Any]:
    main = transcript_dir / f"{transcript_dir.name}.jsonl"
    messages = _read_jsonl_messages(main)
    users = [m["text"] for m in messages if m["role"] == "user"]
    assistants = [m["text"] for m in messages if m["role"] == "assistant"]
    if user_limit is not None:
        users_out = users[-user_limit:]
    else:
        users_out = users
    assistants_out = assistants[-assistant_limit:]

    size = main.stat().st_size if main.is_file() else 0
    lines = [
        f"# Chat digest — `{transcript_dir.name}`",
        "",
        f"Generated: {_now()}",
        f"Cursor project: `{project}`",
        f"Source: `{main}`",
        f"Bytes: {size}",
        f"Extracted messages: {len(messages)} (user={len(users)}, assistant={len(assistants)})",
        "",
        "## User intents (deduped, chronological)",
        "",
    ]
    if user_limit is not None and len(users) > len(users_out):
        lines.append(f"_Showing last {len(users_out)} of {len(users)} unique user turns._")
        lines.append("")
    for i, q in enumerate(users_out, 1):
        lines.append(f"{i}. {_truncate(q, max_chars)}")
    lines.extend(["", "## Recent assistant outcomes", ""])
    for i, a in enumerate(assistants_out, 1):
        lines.append(f"{i}. {_truncate(a, max_chars)}")
    _write_lines(out_path, lines)
    first = users[0] if users else ""
    return {
        "id": transcript_dir.name,
        "project": project,
        "path": str(main),
        "bytes": size,
        "messages": len(messages),
        "user_turns": len(users),
        "assistant_turns": len(assistants),
        "first_user": _truncate(first, 180),
        "digest": str(out_path),
    }


def _iter_transcript_dirs(project_dir: Path) -> list[Path]:
    root = project_dir / "agent-transcripts"
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / f"{d.name}.jsonl").is_file())


def _digest_all_sessions(
    *,
    projects: list[tuple[str, Path]],
    primary_id: str,
    chats_dir: Path,
    other_overview: Path,
    primary_digest: Path,
) -> dict[str, Any]:
    chats_dir.mkdir(parents=True, exist_ok=True)
    catalog: list[dict[str, Any]] = []
    for project, project_dir in projects:
        for d in _iter_transcript_dirs(project_dir):
            is_primary = d.name == primary_id
            digest_name = f"{project}__{d.name}.md"
            info = _digest_one_session(
                project=project,
                transcript_dir=d,
                out_path=chats_dir / digest_name,
                user_limit=None,
                assistant_limit=40 if is_primary else 20,
                max_chars=1000 if is_primary else 800,
            )
            info["primary"] = is_primary
            catalog.append(info)
            if is_primary:
                shutil.copy2(chats_dir / digest_name, primary_digest)

    index_lines = [
        "# Chat inventory",
        "",
        f"Generated: {_now()}",
        "",
        "Full digests live beside this file. Raw JSONL is **not** copied (size + secrets).",
        "",
        "| Primary | Project | Session | User turns | Bytes | First intent | Digest |",
        "|---|---|---|---:|---:|---|---|",
    ]
    other_lines = [
        "# Other sessions",
        "",
        f"Generated: {_now()}",
        "",
    ]
    for row in catalog:
        mark = "yes" if row["primary"] else ""
        digest_rel = f"chats/{Path(row['digest']).name}"
        first = (row["first_user"] or "").replace("|", "/")
        index_lines.append(
            f"| {mark} | `{row['project']}` | `{row['id']}` | {row['user_turns']} | "
            f"{row['bytes']} | {first} | [{Path(row['digest']).name}]({Path(row['digest']).name}) |"
        )
        if not row["primary"]:
            other_lines.append(f"## `{row['project']}` / `{row['id']}`")
            other_lines.append("")
            other_lines.append(f"- Digest: [{digest_rel}]({digest_rel})")
            other_lines.append(f"- User turns: {row['user_turns']}")
            other_lines.append(f"- First: {row['first_user']}")
            other_lines.append("")

    _write_lines(chats_dir / "INDEX.md", index_lines)
    _write_lines(other_overview, other_lines)
    return {"sessions": catalog, "count": len(catalog)}


def _digest_subagents(transcript_dir: Path, out_path: Path) -> dict[str, Any]:
    sub_dir = transcript_dir / "subagents"
    summaries: list[dict[str, str]] = []
    if sub_dir.is_dir():
        for fp in sorted(sub_dir.glob("*.jsonl")):
            msgs = _read_jsonl_messages(fp)
            assistants = [m["text"] for m in msgs if m["role"] == "assistant"]
            users = [m["text"] for m in msgs if m["role"] == "user"]
            if not assistants and not users:
                continue
            blob = assistants[-1] if assistants else users[-1]
            summaries.append(
                {
                    "id": fp.stem,
                    "summary": _truncate(blob, 700),
                    "prompt": _truncate(users[0], 240) if users else "",
                }
            )

    lines = [
        "# Subagent research digest",
        "",
        f"Generated: {_now()}",
        f"Source: `{sub_dir}`",
        f"Count: {len(summaries)}",
        "",
    ]
    for row in summaries:
        lines.append(f"## {row['id']}")
        lines.append("")
        if row["prompt"]:
            lines.append(f"_Task:_ {row['prompt']}")
            lines.append("")
        lines.append(row["summary"])
        lines.append("")
    _write_lines(out_path, lines)
    return {"subagents": len(summaries)}


def _copy_plans(cursor_plans: Path, out_dir: Path) -> list[str]:
    copied: list[str] = []
    seen: set[str] = set()
    out_dir.mkdir(parents=True, exist_ok=True)
    for pattern in PLAN_GLOBS:
        for src in sorted(cursor_plans.glob(pattern)):
            if src.name in seen:
                continue
            seen.add(src.name)
            shutil.copy2(src, out_dir / src.name)
            copied.append(src.name)

    index = [
        "# Packed Cursor plans",
        "",
        f"Generated: {_now()}",
        "",
    ]
    for name in copied:
        src = out_dir / name
        title = name
        overview = ""
        text = src.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^name:\s*(.+)$", text, re.M)
        if m:
            title = m.group(1).strip().strip('"')
        m = re.search(r"^overview:\s*(.+)$", text, re.M)
        if m:
            overview = m.group(1).strip().strip('"')
        index.append(f"- [{name}]({name}) — **{title}**")
        if overview:
            index.append(f"  - {overview[:300]}")
    _write_lines(out_dir / "INDEX.md", index)
    return copied


def _copy_references(out_dir: Path) -> list[str]:
    copied: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for src_s, rel in REFERENCE_FILES:
        src = Path(src_s)
        if not src.is_file():
            continue
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _run_graphify(target: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["graphify", "update", str(target), "--wiki"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-1000:],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "graphify not installed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "graphify timeout"}


def pack(
    *,
    target: Path,
    source_project: str,
    transcript_id: str,
    run_graphify: bool,
) -> dict[str, Any]:
    cursor_root = Path.home() / ".cursor"
    project_dir = cursor_root / "projects" / source_project
    transcript_dir = project_dir / "agent-transcripts" / transcript_id
    plans_src = cursor_root / "plans"
    ctx = target / ".context"

    projects: list[tuple[str, Path]] = [(source_project, project_dir)]
    for extra in EXTRA_PROJECTS:
        extra_dir = cursor_root / "projects" / extra
        if extra_dir.is_dir() and extra != source_project:
            projects.append((extra, extra_dir))

    manifest: dict[str, Any] = {
        "generated_at": _now(),
        "target_repo": str(target),
        "source_project": source_project,
        "transcript_id": transcript_id,
        "transcript_path": str(transcript_dir),
        "extra_projects": [p for p, _ in projects[1:]],
    }

    sessions = _digest_all_sessions(
        projects=projects,
        primary_id=transcript_id,
        chats_dir=ctx / "session" / "chats",
        other_overview=ctx / "session" / "OTHER_SESSIONS.md",
        primary_digest=ctx / "session" / "TRANSCRIPT_DIGEST.md",
    )
    sd = _digest_subagents(transcript_dir, ctx / "research" / "SUBAGENT_DIGEST.md")
    copied = _copy_plans(plans_src, ctx / "plans")
    refs = _copy_references(ctx / "reference")
    manifest["sessions"] = {
        "count": sessions["count"],
        "ids": [row["id"] for row in sessions["sessions"]],
        "user_turns_total": sum(row["user_turns"] for row in sessions["sessions"]),
    }
    manifest["subagents"] = sd
    manifest["plans_copied"] = copied
    manifest["references_copied"] = refs

    if run_graphify:
        manifest["graphify"] = _run_graphify(target)

    (ctx / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack portable agent context into a repo")
    parser.add_argument("--target", type=Path, default=Path("/root/Apps/labvault-public"))
    parser.add_argument("--source-project", default=DEFAULT_CURSOR_PROJECT)
    parser.add_argument("--transcript-id", default=DEFAULT_TRANSCRIPT)
    parser.add_argument("--graphify", action="store_true")
    parser.add_argument("--remember", action="store_true")
    args = parser.parse_args()

    target = args.target.resolve()
    if not target.is_dir():
        print(f"target not found: {target}", file=sys.stderr)
        return 2

    manifest = pack(
        target=target,
        source_project=args.source_project,
        transcript_id=args.transcript_id,
        run_graphify=args.graphify,
    )
    print(json.dumps({"ok": True, "context_dir": str(target / ".context"), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
