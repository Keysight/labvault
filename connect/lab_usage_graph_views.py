"""Graph-based Lab Pulse companion views — radial, stream, radar, matrix, topology.

Each entry in ``GRAPH_VIEWS`` is a login-required HTML page that loads one module
from ``connect/static/js/lab-graph/`` (``view-pulse-radial.js``, ``view-stream-wave.js``,
``view-radar-health.js``, ``view-matrix-heat.js``, ``view-topology-pulse.js``).
Those scripts fetch the same timeline/insights JSON the timeline API returns; this
module does not query devices itself.
See ``docs/development/subsystems/metrics-insights.md``.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from connect.models import LabTopology

GRAPH_VIEWS = {
    'pulse_radial': {
        'active_view': 'graph_pulse_radial',
        'title': 'Pulse Radial',
        'subtitle': 'Radial gauge arcs per device and resource class',
        'icon': 'fa-circle-notch',
        'accent': '#f472b6',
        'module': 'view-pulse-radial.js',
    },
    'stream_wave': {
        'active_view': 'graph_stream_wave',
        'title': 'Stream Wave',
        'subtitle': 'Stacked traffic waveform with event scatter overlays',
        'icon': 'fa-water',
        'accent': '#38bdf8',
        'module': 'view-stream-wave.js',
    },
    'radar_health': {
        'active_view': 'graph_radar_health',
        'title': 'Radar Health',
        'subtitle': 'Spider charts comparing devices across metric dimensions',
        'icon': 'fa-spider',
        'accent': '#a78bfa',
        'module': 'view-radar-health.js',
    },
    'matrix_heat': {
        'active_view': 'graph_matrix_heat',
        'title': 'Matrix Heat',
        'subtitle': 'Device × time heatmap with range brush',
        'icon': 'fa-th',
        'accent': '#fb923c',
        'module': 'view-matrix-heat.js',
    },
    'topology_pulse': {
        'active_view': 'graph_topology_pulse',
        'title': 'Topology Pulse',
        'subtitle': 'Fabric graph with node size and color = live stress',
        'icon': 'fa-project-diagram',
        'accent': '#4ade80',
        'module': 'view-topology-pulse.js',
    },
}


def _render_graph_view(request, topo_id: int, view_key: str):
    from connect.lab_topology_split import get_sub_topology_nav

    meta = GRAPH_VIEWS[view_key]
    topo = get_object_or_404(LabTopology, pk=topo_id)
    return render(request, 'connect/lab_topology_usage_graph.html', {
        'topo': topo,
        'sub_nav': get_sub_topology_nav(topo),
        'view_key': view_key,
        'view_meta': meta,
        'active_view': meta['active_view'],
        'graph_views': GRAPH_VIEWS,
    })


@login_required
@require_GET
def lab_topology_graph_pulse_radial(request, topo_id: int):
    return _render_graph_view(request, topo_id, 'pulse_radial')


@login_required
@require_GET
def lab_topology_graph_stream_wave(request, topo_id: int):
    return _render_graph_view(request, topo_id, 'stream_wave')


@login_required
@require_GET
def lab_topology_graph_radar_health(request, topo_id: int):
    return _render_graph_view(request, topo_id, 'radar_health')


@login_required
@require_GET
def lab_topology_graph_matrix_heat(request, topo_id: int):
    return _render_graph_view(request, topo_id, 'matrix_heat')


@login_required
@require_GET
def lab_topology_graph_topology_pulse(request, topo_id: int):
    return _render_graph_view(request, topo_id, 'topology_pulse')
