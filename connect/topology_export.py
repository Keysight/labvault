"""
Export filtered network topology for draw.io / diagrams.net and JSON tools.

Produces:
  - drawio: mxGraphModel XML (.drawio) importable via File → Import
  - json: LabVault topology subgraph with tier layout hints
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Align with connect/templates/connect/topology.html vendorTier
TIER_ORDER = {
    'fortigate': 0,
    'paloalto': 0,
    'arista': 1,
    'sonic': 2,
    'keysight': 3,
    'unknown': 2,
}
TIER_LABELS = [
    'Routers / firewalls',
    'Switches (core / Arista)',
    'Switches (edge / SONiC / other)',
    'Keysight chassis',
]


def _norm_tags(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def node_matches_tags(node: dict, selected: Set[str]) -> bool:
    if not selected:
        return True
    tags = _norm_tags(node.get('tags'))
    return any(t in selected for t in tags)


def filter_topology_subgraph(
    nodes: List[dict],
    links: List[dict],
    tags: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Same rule as the Topology Map UI: OR-match on tags, then keep full LLDP
    connected components so neighbors stay linked.
    """
    selected = {str(t).strip() for t in (tags or []) if str(t).strip()}
    if not selected:
        return list(nodes), list(links)

    by_id = {str(n.get('id')): n for n in nodes if n.get('id') is not None}
    seeds = [nid for nid, n in by_id.items() if node_matches_tags(n, selected)]
    if not seeds:
        return [], []

    adj: Dict[str, List[str]] = defaultdict(list)
    for lk in links:
        a, b = str(lk.get('source', '')), str(lk.get('target', ''))
        if a in by_id and b in by_id:
            adj[a].append(b)
            adj[b].append(a)

    visible: Set[str] = set()
    stack = list(seeds)
    while stack:
        nid = stack.pop()
        if nid in visible:
            continue
        visible.add(nid)
        for nb in adj.get(nid, []):
            if nb not in visible:
                stack.append(nb)

    fnodes = [dict(by_id[i]) for i in sorted(visible, key=lambda x: (by_id[x].get('name') or x))]
    flinks = [
        dict(lk)
        for lk in links
        if str(lk.get('source')) in visible and str(lk.get('target')) in visible
    ]
    return fnodes, flinks


def _is_chassis(node: dict) -> bool:
    nid = str(node.get('id', ''))
    if nid.startswith('chassis-'):
        return True
    return (node.get('vendor') or '').lower() == 'keysight'


def _tier_index(node: dict) -> int:
    if _is_chassis(node):
        return 3
    v = (node.get('vendor') or 'unknown').lower()
    return TIER_ORDER.get(v, 2)


def assign_tier_positions(nodes: List[dict]) -> Dict[str, Tuple[float, float]]:
    """Simple swimlane layout similar to Tier view on /topology/."""
    by_tier: Dict[int, List[dict]] = defaultdict(list)
    for n in nodes:
        by_tier[_tier_index(n)].append(n)

    positions: Dict[str, Tuple[float, float]] = {}
    lane_h = 140.0
    card_w = 150.0
    x_gap = 24.0
    y0 = 40.0

    for tier in sorted(by_tier.keys()):
        row = sorted(by_tier[tier], key=lambda n: (n.get('name') or n.get('id') or ''))
        y = y0 + tier * lane_h
        span = max(len(row) - 1, 0) * (card_w + x_gap)
        x_start = max(40.0, (900.0 - span) / 2.0)
        for i, n in enumerate(row):
            nid = str(n.get('id'))
            positions[nid] = (x_start + i * (card_w + x_gap), y)
    return positions


def topology_export_json(
    nodes: List[dict],
    links: List[dict],
    *,
    title: str = 'LabVault topology',
    tags: Optional[List[str]] = None,
) -> dict:
    positions = assign_tier_positions(nodes)
    out_nodes = []
    for n in nodes:
        nid = str(n.get('id'))
        x, y = positions.get(nid, (0.0, 0.0))
        out_nodes.append({
            'id': nid,
            'label': n.get('name') or n.get('label') or nid,
            'ip': n.get('ip') or '',
            'vendor': n.get('vendor') or '',
            'status': n.get('status') or 'unknown',
            'model': n.get('model') or '',
            'tags': _norm_tags(n.get('tags')),
            'tier': _tier_index(n),
            'tier_label': TIER_LABELS[_tier_index(n)] if _tier_index(n) < len(TIER_LABELS) else '',
            'x': round(x, 1),
            'y': round(y, 1),
        })
    out_links = []
    for lk in links:
        out_links.append({
            'source': str(lk.get('source')),
            'target': str(lk.get('target')),
            'local_port': lk.get('local_port') or '',
            'remote_port': lk.get('remote_port') or '',
            'status': lk.get('status') or 'up',
            'link_count': lk.get('link_count', 1),
        })
    return {
        'format': 'labvault-topology-export-v1',
        'title': title,
        'filter_tags': list(tags or []),
        'tier_labels': TIER_LABELS,
        'nodes': out_nodes,
        'links': out_links,
    }


def _xml_esc(text: str) -> str:
    return html.escape(text or '', quote=True)


def topology_to_drawio_xml(
    nodes: List[dict],
    links: List[dict],
    *,
    diagram_name: str = 'LabVault',
) -> str:
    """
    mxGraphModel XML wrapped in mxfile — open in draw.io via File → Import from device.
    """
    positions = assign_tier_positions(nodes)
    id_map: Dict[str, str] = {}
    cell_id = 2
    root_children: List[ET.Element] = []

    # Swimlane backgrounds
    lane_h = 140
    for tier, label in enumerate(TIER_LABELS):
        band = ET.Element(
            'mxCell',
            {
                'id': str(cell_id),
                'value': label,
                'style': (
                    'swimlane;horizontal=0;startSize=24;fillColor=#1e293b;'
                    'fontColor=#94a3b8;fontSize=11;'
                ),
                'vertex': '1',
                'parent': '1',
            },
        )
        geo = ET.SubElement(band, 'mxGeometry', {'x': '20', 'y': str(30 + tier * lane_h), 'width': '1100', 'height': str(lane_h - 10), 'as': 'geometry'})
        root_children.append(band)
        cell_id += 1

    vendor_fill = {
        'arista': '#2196F3',
        'sonic': '#4CAF50',
        'fortigate': '#FF5722',
        'paloalto': '#FF9800',
        'keysight': '#9C27B0',
        'unknown': '#607D8B',
    }

    for n in nodes:
        nid = str(n.get('id'))
        mx_id = str(cell_id)
        id_map[nid] = mx_id
        cell_id += 1
        x, y = positions.get(nid, (40.0, 40.0))
        label = n.get('name') or n.get('label') or nid
        ip = n.get('ip') or ''
        vendor = (n.get('vendor') or 'unknown').lower()
        fill = vendor_fill.get(vendor, '#607D8B')
        status = (n.get('status') or 'unknown').lower()
        stroke = '#22c55e' if status == 'online' else '#ef4444'
        value = f'<b>{_xml_esc(label)}</b><br><font style="font-size:10px;">{_xml_esc(ip)}</font>'
        style = (
            f'rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};'
            f'fontColor=#ffffff;verticalAlign=middle;align=center;'
        )
        cell = ET.Element(
            'mxCell',
            {
                'id': mx_id,
                'value': value,
                'style': style,
                'vertex': '1',
                'parent': '1',
            },
        )
        w, h = (108.0, 44.0) if _is_chassis(n) else (128.0, 52.0)
        ET.SubElement(
            cell,
            'mxGeometry',
            {
                'x': str(int(x)),
                'y': str(int(y)),
                'width': str(int(w)),
                'height': str(int(h)),
                'as': 'geometry',
            },
        )
        root_children.append(cell)

    for lk in links:
        src = str(lk.get('source'))
        tgt = str(lk.get('target'))
        if src not in id_map or tgt not in id_map:
            continue
        st = (lk.get('status') or 'up').lower()
        stroke = '#22c55e' if st == 'up' else '#ef4444'
        lp = (lk.get('local_port') or '').split(',')[0].strip()
        rp = (lk.get('remote_port') or '').split(',')[0].strip()
        lbl = f'{lp} ↔ {rp}' if lp or rp else ''
        if (lk.get('link_count') or 1) > 1:
            lbl = f'×{lk["link_count"]} {lbl}'.strip()
        edge = ET.Element(
            'mxCell',
            {
                'id': str(cell_id),
                'value': _xml_esc(lbl),
                'style': (
                    f'edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;'
                    f'jettySize=auto;html=1;strokeColor={stroke};'
                ),
                'edge': '1',
                'parent': '1',
                'source': id_map[src],
                'target': id_map[tgt],
            },
        )
        ET.SubElement(edge, 'mxGeometry', {'relative': '1', 'as': 'geometry'})
        root_children.append(edge)
        cell_id += 1

    mxfile = ET.Element('mxfile', {'host': 'app.diagrams.net', 'agent': 'LabVault'})
    diagram = ET.SubElement(mxfile, 'diagram', {'name': _xml_esc(diagram_name), 'id': 'labvault-topology'})
    model = ET.SubElement(
        diagram,
        'mxGraphModel',
        {
            'dx': '1200',
            'dy': '800',
            'grid': '1',
            'gridSize': '10',
            'guides': '1',
            'tooltips': '1',
            'connect': '1',
            'arrows': '1',
            'fold': '1',
            'page': '1',
            'pageScale': '1',
            'pageWidth': '1169',
            'pageHeight': '827',
            'math': '0',
            'shadow': '0',
        },
    )
    root = ET.SubElement(model, 'root')
    ET.SubElement(root, 'mxCell', {'id': '0'})
    ET.SubElement(root, 'mxCell', {'id': '1', 'parent': '0'})
    for child in root_children:
        root.append(child)

    xml_body = ET.tostring(mxfile, encoding='unicode', xml_declaration=True)
    return xml_body


def parse_tags_param(raw: str) -> List[str]:
    if not raw:
        return []
    return [p.strip() for p in re.split(r'[,;]+', raw) if p.strip()]
