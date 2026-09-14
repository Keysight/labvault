"""CLI catalog: guided D1/D2 verbs present; dumped surfaces absent."""
from django.test import SimpleTestCase

from connect.labvault_cli.registry import COMMANDS


class CliCatalogTests(SimpleTestCase):
    def test_guided_commands_registered(self):
        for name in (
            "device add",
            "device show",
            "topo import",
            "reserve create",
            "lldp refresh",
            "settings get",
            "device command",
        ):
            self.assertIn(name, COMMANDS)
            self.assertTrue(COMMANDS[name].guided)

    def test_dumped_verbs_absent(self):
        names = " ".join(COMMANDS)
        for banned in ("laas", "capex", "hyperview", "bfshell", "ucli", "uhd"):
            self.assertNotIn(banned, names)

    def test_device_command_rejects_freeform(self):
        from connect.labvault_cli.commands import device_command

        out = device_command(None, id_or_ip="1", verb="rm -rf /")
        self.assertEqual(out["state"], "denied")
        self.assertEqual(out["error"], "verb_not_allowlisted")
