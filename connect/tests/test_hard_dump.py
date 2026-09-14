"""Customer SKU must not expose dumped surfaces."""
from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse


class HardDumpRouteTests(SimpleTestCase):
    def test_freeform_device_shell_urls_absent(self):
        for name in ("device_terminal", "api_execute_command"):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[1])

    def test_laas_manifest_url_absent(self):
        with self.assertRaises(NoReverseMatch):
            reverse("lab_topology_laas_manifest", args=[1])

    def test_b2b_export_module_absent(self):
        with self.assertRaises(ModuleNotFoundError):
            __import__("connect.topology_b2b_export")

    def test_uhd_modules_absent(self):
        for name in (
            "connect.uhd_fetch",
            "connect.uhd_views",
            "connect.keysight_uhd_api",
            "connect.keysight_drivers.uhd_connect",
        ):
            with self.assertRaises(ModuleNotFoundError):
                __import__(name)

    def test_uhd_url_names_absent(self):
        for name in ("uhd_api_bfshell", "uhd_api_config", "uhd_l1_restore"):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[1])


class HardDumpContentTests(TestCase):
    def test_terminal_path_404(self):
        r = self.client.get("/device/1/terminal/")
        self.assertEqual(r.status_code, 404)

    def test_execute_command_path_404(self):
        r = self.client.post("/api/device/1/command/", {}, content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_port_fabric_summary_404(self):
        r = self.client.get("/lab-topology/1/port-fabric/summary.json")
        self.assertEqual(r.status_code, 404)
