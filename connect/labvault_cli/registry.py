from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Command:
    name: str
    help: str
    handler: Callable[..., Any]
    mutating: bool = False
    requires_confirm: bool = False
    tier: str = "read"  # read | privileged | service_control
    fields: tuple[str, ...] = ()
    guided: bool = False

    def meta(self) -> dict:
        return {
            "name": self.name,
            "help": self.help,
            "mutating": self.mutating,
            "requires_confirm": self.requires_confirm,
            "tier": self.tier,
            "fields": list(self.fields),
            "guided": self.guided,
        }


COMMANDS: dict[str, Command] = {}
ALIASES: dict[str, str] = {}


def command(
    name: str,
    help: str,
    *,
    mutating: bool = False,
    requires_confirm: bool = False,
    tier: str = "read",
    aliases: tuple[str, ...] = (),
    fields: tuple[str, ...] = (),
    guided: bool = False,
):
    def deco(fn):
        COMMANDS[name] = Command(
            name=name,
            help=help,
            handler=fn,
            mutating=mutating,
            requires_confirm=requires_confirm,
            tier=tier,
            fields=fields,
            guided=guided,
        )
        for alias in aliases:
            ALIASES[alias] = name
            # Expose alias as a first-class command name pointing at same handler
            COMMANDS[alias] = Command(
                name=alias,
                help=f"Alias for `{name}`",
                handler=fn,
                mutating=mutating,
                requires_confirm=requires_confirm,
                tier=tier,
                fields=fields,
                guided=guided,
            )
        return fn

    return deco


def get_command(name: str) -> Command | None:
    if name in COMMANDS:
        return COMMANDS[name]
    canon = ALIASES.get(name)
    if canon:
        return COMMANDS.get(canon)
    return None
