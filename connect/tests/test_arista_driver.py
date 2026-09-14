"""Unit tests for Arista driver helpers (no live switch required)."""
import unittest

from connect.drivers.arista import (
    _cpu_idle_from_top_json,
    _normalize_lldp_neighbors_summary,
    extract_input_discards_from_interface_info,
    is_physical_ethernet_iface,
    parse_show_version_text,
)


class TestParseShowVersionText(unittest.TestCase):
    def test_license_style_platform(self):
        text = """
Customer name:
System Serial number:
System MAC address:
Domain name:           Unknown
Platform:              DCS-7060X6-64PE-B
"""
        d = parse_show_version_text(text)
        self.assertEqual(d.get("modelName"), "DCS-7060X6-64PE-B")

    def test_serial_and_mac(self):
        text = """Hostname: HBG252901MU
Serial number: X123
System MAC address: 00:11:22:33:44:55
Arista EOS version 4.32.0F running on an Arista DCS-7060X6-64PE-B
"""
        d = parse_show_version_text(text)
        self.assertEqual(d.get("hostname"), "HBG252901MU")
        self.assertEqual(d.get("serialNumber"), "X123")
        self.assertEqual(d.get("systemMacAddress"), "00:11:22:33:44:55")
        self.assertIn("4.32.0F", d.get("version", ""))

    def test_empty_input(self):
        self.assertEqual(parse_show_version_text(""), {})
        self.assertEqual(parse_show_version_text(None), {})

    def test_hardware_model_fallback(self):
        text = "Hardware model:  DCS-7050TX-64\nArista EOS version 4.28.1F"
        d = parse_show_version_text(text)
        self.assertEqual(d.get("modelName"), "DCS-7050TX-64")


class TestCpuIdle(unittest.TestCase):
    def test_classic_cpu_info(self):
        top = {"cpuInfo": {"%Cpu(s)": {"idle": 85.0}}}
        idle = _cpu_idle_from_top_json(top["cpuInfo"])
        self.assertEqual(idle, 85.0)

    def test_flat_idle(self):
        self.assertEqual(_cpu_idle_from_top_json({"idle": 90.0}), 90.0)

    def test_pct_cpu_key(self):
        cpu = {"%Cpu(s)": {"idle": 72.5}}
        self.assertEqual(_cpu_idle_from_top_json(cpu), 72.5)

    def test_non_dict(self):
        self.assertEqual(_cpu_idle_from_top_json(None), 100.0)
        self.assertEqual(_cpu_idle_from_top_json("bad"), 100.0)


class TestLldpNormalize(unittest.TestCase):
    def test_list_form(self):
        raw = {
            "lldpNeighbors": [
                {"port": "Ethernet1", "neighborDevice": "a", "neighborPort": "b"},
            ]
        }
        self.assertEqual(len(_normalize_lldp_neighbors_summary(raw)), 1)

    def test_dict_per_interface(self):
        raw = {
            "lldpNeighbors": {
                "Ethernet1/1": [{"neighborDevice": "x", "neighborPort": "y"}],
            }
        }
        out = _normalize_lldp_neighbors_summary(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("port"), "Ethernet1/1")

    def test_empty(self):
        self.assertEqual(_normalize_lldp_neighbors_summary({}), [])
        self.assertEqual(_normalize_lldp_neighbors_summary(None), [])


class TestInputDiscardsHelpers(unittest.TestCase):
    def test_is_physical_ethernet_iface(self):
        self.assertTrue(is_physical_ethernet_iface('Ethernet6/1'))
        self.assertTrue(is_physical_ethernet_iface('ethernet1'))
        self.assertFalse(is_physical_ethernet_iface('Vlan100'))
        self.assertFalse(is_physical_ethernet_iface('Management1'))
        self.assertFalse(is_physical_ethernet_iface('Port-Channel10'))

    def test_extract_input_discards_from_interface_info(self):
        info = {'interfaceCounters': {'inDiscards': 42}}
        self.assertEqual(extract_input_discards_from_interface_info(info), 42)
        self.assertEqual(
            extract_input_discards_from_interface_info({'interfaceCounters': {'inputDiscards': 7}}),
            7,
        )
        self.assertEqual(extract_input_discards_from_interface_info({}), 0)


if __name__ == "__main__":
    unittest.main()
