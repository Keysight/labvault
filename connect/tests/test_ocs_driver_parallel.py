"""OCS driver parallel fetch (no live Calient required)."""
import time
import unittest
from unittest.mock import MagicMock, patch

from connect.drivers.ocs import OcsDriver


class TestOcsDriverParallel(unittest.TestCase):
    def test_fetch_ocs_sources_parallel(self):
        calls = []

        def fake_rest_request(method, rel, *, params=None, json_body=None, timeout=30):
            calls.append("restversion")
            time.sleep(0.05)
            resp = MagicMock()
            resp.status_code = 200
            resp.text = '{"version":"1.0"}'
            resp.json.return_value = {"version": "1.0"}
            return resp, None

        def fake_rest_get_json(rel, params, timeout=15):
            calls.append(rel)
            time.sleep(0.05)
            if rel == "ports/":
                return [{"port": "1.1.1", "conn": "1.1.1>1.1.2"}]
            if rel == "crossconnects/":
                return [
                    {
                        "name": "1.1.1-1.1.2",
                        "half1": {"conn": "1.1.1>1.1.2"},
                        "half2": {"conn": "1.1.2>1.1.1"},
                    }
                ]
            return None

        device = MagicMock(ip_address="10.0.0.1", api_key="{}", username="u", password="p")
        driver = OcsDriver(device)

        with patch.object(driver, "_rest_request", side_effect=fake_rest_request):
            with patch.object(driver, "_rest_get_json", side_effect=fake_rest_get_json):
                t0 = time.time()
                sources = driver.fetch_ocs_sources_parallel()
                elapsed = time.time() - t0

        self.assertEqual(set(calls), {"restversion", "ports/", "crossconnects/"})
        self.assertLess(elapsed, 0.12)
        self.assertEqual(sources["restversion_status"], "ok")
        self.assertEqual(len(sources["ports_rows"]), 1)
        self.assertEqual(len(sources["xc_rows"]), 1)
        self.assertEqual(OcsDriver.probe_from_sources(sources), "ok")

    def test_probe_from_sources_auth_failed(self):
        sources = {"restversion_status": "auth_failed"}
        self.assertEqual(OcsDriver.probe_from_sources(sources), "auth_failed")

    def test_get_interfaces_uses_prefetched_ports(self):
        device = MagicMock(ip_address="10.0.0.1", api_key="{}", username="u", password="p")
        driver = OcsDriver(device)
        rows = [{"port": "2.1.1", "conn": "2.1.1>2.1.2", "connid": "x"}]
        with patch.object(driver, "_rest_get_json") as mock_get:
            result = driver.get_interfaces(ports_rows=rows)
            mock_get.assert_not_called()
        self.assertTrue(result.success)
        self.assertEqual(result.data["physical_data"][0]["name"], "2.1.1")
        self.assertEqual(result.data["physical_data"][0]["status"], "connected")

    def test_fetch_crossconnect_list_prefetched(self):
        device = MagicMock(ip_address="10.0.0.1", api_key="{}", username="u", password="p")
        driver = OcsDriver(device)
        raw = [{"name": "a-b", "half1": {"conn": "1.1.1>1.1.2"}}]
        with patch.object(driver, "_rest_get_json") as mock_get:
            out = driver.fetch_crossconnect_list(raw_rows=raw)
            mock_get.assert_not_called()
        self.assertEqual(out, raw)


if __name__ == "__main__":
    unittest.main()
