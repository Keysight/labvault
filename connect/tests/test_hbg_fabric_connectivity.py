"""Site fabric-path helpers use an injected map (no baked-in lab IPs)."""
from connect.hbg_fabric_connectivity import (
    DAC_DIRECT_CHASSIS_IPS,
    OCS_CHASSIS_IPS,
    aggregate_fabric_path_ports,
    build_site_fabric_summary,
    build_topology_fabric_summary,
    lookup_aresone_fabric,
    ocs_fixed_mapping_is_active,
    set_fabric_map,
)

# Fixture IPs only — not shipped as runtime defaults.
_FIXTURE = {
    "10.36.84.31": {
        "chassis_id": "M01",
        "path": "dac_direct",
        "ocs_connected": False,
        "peer_label": "Arista_3",
    },
    "10.36.84.32": {
        "chassis_id": "M02",
        "path": "dac_direct",
        "ocs_connected": False,
        "peer_label": "Arista_4",
    },
    "10.36.84.33": {
        "chassis_id": "M03",
        "path": "dac_direct",
        "ocs_connected": False,
        "peer_label": "Arista_3",
    },
    "10.36.84.34": {
        "chassis_id": "M04",
        "path": "dac_direct",
        "ocs_connected": False,
        "peer_label": "Arista_4",
    },
    "10.36.84.35": {
        "chassis_id": "M05",
        "path": "ocs",
        "ocs_connected": True,
        "ocs_ip": "10.36.84.39",
        "ocs_label": "OCS-S320",
    },
    "10.36.84.36": {
        "chassis_id": "M06",
        "path": "ocs",
        "ocs_connected": True,
        "ocs_ip": "10.36.84.39",
    },
    "10.36.84.37": {
        "chassis_id": "M07",
        "path": "ocs",
        "ocs_connected": True,
        "ocs_ip": "10.36.84.39",
    },
    "10.36.84.38": {
        "chassis_id": "M08",
        "path": "ocs",
        "ocs_connected": True,
        "ocs_ip": "10.36.84.39",
    },
}


def setup_module():
    set_fabric_map(_FIXTURE, ocs_ip="10.36.84.39", ocs_label="OCS-S320")


def teardown_module():
    set_fabric_map({})


def test_empty_map_is_the_shipped_default():
    set_fabric_map({})
    assert lookup_aresone_fabric("10.36.84.35") is None
    assert build_site_fabric_summary()["ocs_chassis_count"] == 0
    set_fabric_map(_FIXTURE, ocs_ip="10.36.84.39", ocs_label="OCS-S320")


def test_ares_m05_m08_are_ocs_only():
    for ip in ("10.36.84.35", "10.36.84.36", "10.36.84.37", "10.36.84.38"):
        meta = lookup_aresone_fabric(ip)
        assert meta is not None
        assert meta["path"] == "ocs"
        assert meta["ocs_connected"] is True
        assert ip in OCS_CHASSIS_IPS


def test_ares_m01_m04_are_dac_direct():
    peers = {
        "10.36.84.31": "Arista_3",
        "10.36.84.32": "Arista_4",
        "10.36.84.33": "Arista_3",
        "10.36.84.34": "Arista_4",
    }
    for ip, peer in peers.items():
        meta = lookup_aresone_fabric(ip)
        assert meta["path"] == "dac_direct"
        assert meta["ocs_connected"] is False
        assert meta["peer_label"] == peer
        assert ip in DAC_DIRECT_CHASSIS_IPS


def test_ocs_fixed_mapping_is_active():
    assert ocs_fixed_mapping_is_active({"port_to_ocs_triplets": {"port_1": ["1.1.1", "1.1.2"]}})
    assert not ocs_fixed_mapping_is_active({"dac_direct": True, "_path": "dac_to_arista"})
    assert not ocs_fixed_mapping_is_active(
        {"_patch_status": "pending_physical_patch", "port_to_ocs_triplets": {"port_1": ["4.1.1"]}}
    )


def test_build_site_fabric_summary_counts():
    summary = build_site_fabric_summary()
    assert summary["ocs_chassis_count"] == 4
    assert summary["dac_direct_chassis_count"] == 4


def test_topology_fabric_summary_scopes_to_present_chassis():
    pilot = build_topology_fabric_summary(
        chassis_mgmt_ips=["10.36.84.35"],
        has_ocs_node=True,
    )
    assert pilot["scope"] == "topology"
    assert len(pilot["ocs_chassis"]) == 1
    assert pilot["ocs_chassis"][0]["chassis_id"] == "M05"
    assert pilot["dac_direct_chassis"] == []
    assert "M05" in pilot["description"]

    dac_only = build_topology_fabric_summary(
        chassis_mgmt_ips=["10.36.84.31", "10.36.84.32"],
        has_ocs_node=False,
    )
    assert len(dac_only["dac_direct_chassis"]) == 2
    assert dac_only["ocs_chassis"] == []


def test_aggregate_fabric_path_ports():
    rows = [
        {"fabric_path": "ocs", "slots": [{"ports_up": 2, "ports_total": 8}]},
        {"fabric_path": "dac_direct", "slots": [{"ports_up": 1, "ports_total": 4}]},
    ]
    buckets = aggregate_fabric_path_ports(rows)
    assert buckets["ocs"]["up"] == 2
    assert buckets["dac_direct"]["total"] == 4
