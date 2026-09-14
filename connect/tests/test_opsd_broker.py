"""Ops broker unit tests (no root/systemctl required)."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


def _load_opsd():
    root = Path(__file__).resolve().parents[2] / "opsd"
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("opsd_mod", root / "opsd.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class OpsdBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.opsd = _load_opsd()

    def test_rejects_extra_fields(self):
        resp = self.opsd.handle({"action": "list", "shell": "bash"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("state"), "denied")

    def test_rejects_unknown_action(self):
        resp = self.opsd.handle({"action": "exec"})
        self.assertFalse(resp.get("ok"))

    def test_restart_all_requires_reason(self):
        resp = self.opsd.handle({"action": "restart", "name": "all", "source": "ssh"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error"), "reason_required")

    def test_db_restart_denied(self):
        with mock.patch.object(self.opsd, "_detect_adapter", return_value="systemd"):
            resp = self.opsd.handle(
                {
                    "action": "restart",
                    "name": "db",
                    "source": "ssh",
                    "reason": "nope",
                }
            )
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("state"), "denied")

    def test_web_restart_denied_from_web(self):
        with mock.patch.object(self.opsd, "_detect_adapter", return_value="systemd"):
            resp = self.opsd.handle(
                {
                    "action": "restart",
                    "name": "web",
                    "source": "web",
                    "reason": "test",
                }
            )
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("state"), "denied")

    def test_list_uses_status_helper(self):
        with mock.patch.object(self.opsd, "_detect_adapter", return_value="systemd"):
            with mock.patch.object(
                self.opsd,
                "_status_one",
                return_value={"name": "web", "ok": True, "state": "running"},
            ):
                resp = self.opsd.handle({"action": "list", "source": "ssh"})
        self.assertTrue(resp.get("ok"))
        self.assertIn("services", resp)


if __name__ == "__main__":
    unittest.main()
