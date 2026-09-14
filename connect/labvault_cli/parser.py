"""Server-side LabVault CLI line parser (shlex)."""
from __future__ import annotations

import shlex
from typing import Any

from connect.labvault_cli.registry import COMMANDS, get_command

MAX_LINE_CHARS = 4096
MAX_TOKENS = 64


class ParseError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


def _coerce(value: str) -> Any:
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        try:
            return int(value)
        except ValueError:
            pass
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return value


def parse_line(line: str) -> tuple[str, dict[str, Any]]:
    """Parse a CLI line into (command_name, args).

    Supports:
    - longest-match multi-word commands from the registry
    - positional args mapped onto handler parameter names when unambiguous
    - legacy key=value tokens
    - quoted values via shlex
    """
    raw = (line or "").strip()
    if not raw:
        raise ParseError("empty_line")
    if len(raw) > MAX_LINE_CHARS:
        raise ParseError("line_too_long", f"max {MAX_LINE_CHARS} chars")

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ParseError("malformed_line", str(exc)) from exc

    if not tokens:
        raise ParseError("empty_line")
    if len(tokens) > MAX_TOKENS:
        raise ParseError("too_many_tokens", f"max {MAX_TOKENS}")

    names = sorted(COMMANDS.keys(), key=lambda n: len(n.split()), reverse=True)
    command = ""
    rest: list[str] = tokens
    for name in names:
        n_parts = name.split()
        if tokens[: len(n_parts)] == n_parts:
            command = name
            rest = tokens[len(n_parts) :]
            break
    if not command:
        raise ParseError("unknown_command", " ".join(tokens[:3]))

    args: dict[str, Any] = {}
    positionals: list[str] = []
    for tok in rest:
        if "=" in tok and not tok.startswith("="):
            key, _, val = tok.partition("=")
            if key and key.replace("_", "").isalnum():
                args[key] = _coerce(val)
                continue
        positionals.append(tok)

    cmd = get_command(command)
    if cmd and positionals:
        import inspect

        params = [
            p.name
            for p in inspect.signature(cmd.handler).parameters.values()
            if p.name not in ("request", "ctx", "kwargs") and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
        # Skip already-filled kwargs; map remaining positionals in order.
        free = [p for p in params if p not in args]
        for i, val in enumerate(positionals):
            if i >= len(free):
                # Extra bare tokens: stash under _extra for handlers that care
                args.setdefault("_extra", [])
                if isinstance(args["_extra"], list):
                    args["_extra"].append(val)
                break
            args[free[i]] = _coerce(val)

    return command, args


def parse_invoke_body(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Accept either {"line": "..."} or {"command": "...", "args": {...}}."""
    if not isinstance(body, dict):
        raise ParseError("invalid_body")
    line = body.get("line")
    if line is not None and str(line).strip():
        return parse_line(str(line))
    command = str(body.get("command") or "").strip()
    if not command:
        raise ParseError("missing_command")
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise ParseError("args_must_be_object")
    # Allow command string that still needs longest-match resolution if caller
    # passed a full phrase without splitting args.
    if " " in command and command not in COMMANDS:
        return parse_line(command)
    if command not in COMMANDS:
        # Try treating command + args values as a line for aliases
        raise ParseError("unknown_command", command)
    return command, dict(args)
