from connect.labvault_cli import commands as _commands  # noqa: F401
from connect.labvault_cli.registry import COMMANDS, command, get_command

__all__ = ["COMMANDS", "command", "get_command"]
