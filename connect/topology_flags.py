"""
Feature flags for topology graph rollout (all default off).

Set in environment or .env:
  LABVAULT_GRAPH_FABRIC=1
  LABVAULT_GRAPH_PORT_FABRIC=1
  LABVAULT_GRAPH_DESIGNER=1
  LABVAULT_TOPOLOGY_GRAPH_V3=1
  LABVAULT_TOPOLOGY_SSE=1
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
