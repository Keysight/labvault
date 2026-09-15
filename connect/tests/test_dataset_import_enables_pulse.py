"""Dataset import with chassis/topology turns Lab Pulse live."""
from __future__ import annotations

from django.test import TestCase, override_settings

from connect.labvault_dataset import import_from_payload
from connect.metric_collectors import collector_mode, node_is_collectable, resolve_topology_chassis
from connect.models import KeysightChassis, LabTopology
from connect.runtime_settings import get_setting, set_setting


def _empty_payload(**overrides):
    payload = {
        "format": "labvault-full-export",
        "version": 1,
        "devices": [],
        "keysight_chassis": [],
        "keysight_reservations": [],
        "keysight_reservation_items": [],
        "keysight_bmc_endpoints": [],
        "keysight_subnet_scans": [],
        "keysight_deployment_jobs": [],
        "audit_logs": [],
        "request_logs": [],
        "config_backups": [],
        "changelog_events": [],
        "device_state_baselines": [],
        "lab_topologies": [],
        "topology_links": [],
        "capex": {},
    }
    payload.update(overrides)
    return payload


class DatasetImportEnablesPulseTests(TestCase):
    @override_settings(WORKER_MODE_ENV="idle")
    def test_empty_import_stays_idle(self):
        stats = import_from_payload(_empty_payload())
        self.assertNotEqual(stats.get("pulse"), "live")
        self.assertEqual(collector_mode(), "idle")

    @override_settings(WORKER_MODE_ENV="idle")
    def test_chassis_import_sets_live_despite_idle_env(self):
        stats = import_from_payload(
            _empty_payload(
                keysight_chassis=[
                    {
                        "ip_address": "192.0.2.31",
                        "hostname": "AresONE-01",
                        "username": "admin",
                        "password": "admin",
                        "chassis_type": "aresone",
                    }
                ]
            )
        )
        self.assertEqual(stats.get("pulse"), "live")
        self.assertEqual(get_setting("collector_mode"), "live")
        self.assertEqual(get_setting("heartbeat_mode"), "live")
        self.assertEqual(collector_mode(), "live")

    @override_settings(WORKER_MODE_ENV="idle")
    def test_db_live_wins_over_env_idle(self):
        set_setting("collector_mode", "live")
        set_setting("heartbeat_mode", "live")
        self.assertEqual(get_setting("collector_mode"), "live")
        self.assertEqual(collector_mode(), "live")

    @override_settings(WORKER_MODE_ENV="idle")
    def test_pickup_import_binds_chassis_and_enables_collection(self):
        stats = import_from_payload(
            _empty_payload(
                devices=[
                    {
                        "ip_address": "192.0.2.31",
                        "hostname": "AresONE-01",
                        "username": "admin",
                        "password": "admin",
                        "vendor_type": "keysight",
                    }
                ],
                keysight_chassis=[
                    {
                        "ip_address": "192.0.2.31",
                        "hostname": "AresONE-01",
                        "username": "admin",
                        "password": "admin",
                        "chassis_type": "aresone",
                    }
                ],
                lab_topologies=[
                    {
                        "format": "labvault.lab_topology",
                        "version": 3,
                        "topology": {"name": "DC Lab Topology"},
                        "nodes": [
                            {
                                "id": "aresone01",
                                "node_key": "aresone01",
                                "label": "AresONE-01",
                                "node_type": "chassis",
                                "device_ip": "192.0.2.31",
                                "extra": {},
                            }
                        ],
                        "links": [],
                    }
                ],
            )
        )
        self.assertEqual(stats.get("pulse"), "live")
        self.assertEqual(stats.get("topologies_imported"), 1)
        topo = LabTopology.objects.get(name="DC Lab Topology")
        self.assertTrue(topo.metrics_collection_enabled)
        node = topo.nodes.get(node_key="aresone01")
        chassis = KeysightChassis.objects.get(ip_address="192.0.2.31")
        self.assertEqual(node.extra.get("chassis_id"), chassis.pk)
        self.assertTrue(node_is_collectable(node))
        self.assertEqual(resolve_topology_chassis(node).pk, chassis.pk)
