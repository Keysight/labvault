"""
Feature flags for topology graph rollout (all default off).

Set in environment or .env:
  LABVAULT_GRAPH_FABRIC=1
  LABVAULT_GRAPH_PORT_FABRIC=1
  LABVAULT_GRAPH_DESIGNER=1
  LABVAULT_TOPOLOGY_GRAPH_V3=1
  LABVAULT_TOPOLOGY_SSE=1

Readers: GRAPH_FABRIC switches ``lab_fabric_map_api`` to ``TopologyGraphBuilder``;
TOPOLOGY_GRAPH_V3 switches ``/topology/data/`` and ``/topology/export/`` to
``build_global_graph()``. PORT_FABRIC, DESIGNER and SSE are not read by any
view in this tree. Values are read once at import time (restart to change).
"""
from __future__ import annotations

import os


def _flag(name: str) -> bool:
    return os.environ.get(name, '0').strip() == '1'


TOPOLOGY_GRAPH_FABRIC = _flag('LABVAULT_GRAPH_FABRIC')
TOPOLOGY_GRAPH_PORT_FABRIC = _flag('LABVAULT_GRAPH_PORT_FABRIC')
TOPOLOGY_GRAPH_DESIGNER = _flag('LABVAULT_GRAPH_DESIGNER')
TOPOLOGY_GRAPH_V3 = _flag('LABVAULT_TOPOLOGY_GRAPH_V3')
TOPOLOGY_SSE = _flag('LABVAULT_TOPOLOGY_SSE')
