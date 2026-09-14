/**
 * Pulse Radial — radial gauge arcs per device/resource class.
 */

import { deviceRowsFromInsights } from './usage-graph-data.js';

function pctColor(v) {
  if (v >= 80) return '#ef4444';
  if (v >= 55) return '#f59e0b';
  if (v >= 30) return '#38bdf8';
  return '#22c55e';
}

function drawRadialGauge(svg, cx, cy, r, value, color, label) {
  const arc = d3.arc().innerRadius(r * 0.62).outerRadius(r).startAngle(0);
  const g = svg.append('g').attr('transform', `translate(${cx},${cy})`);
  g.append('path')
    .attr('d', arc({ endAngle: Math.PI * 2 }))
    .attr('fill', '#1e293b');
  g.append('path')
    .attr('d', arc({ endAngle: (Math.min(100, value) / 100) * Math.PI * 2 }))
    .attr('fill', color)
    .attr('opacity', 0.9);
  g.append('text').attr('text-anchor', 'middle').attr('y', 4)
    .attr('fill', '#f8fafc').attr('font-size', 11).attr('font-weight', 700)
    .text(`${Math.round(value)}%`);
  g.append('text').attr('text-anchor', 'middle').attr('y', r + 14)
    .attr('fill', '#94a3b8').attr('font-size', 8)
    .text(label.length > 16 ? `${label.slice(0, 14)}…` : label);
}

export function initView({ rootEl, detailEl, insights, dataMode }) {
  rootEl.innerHTML = `
    <div class="ugv-radial-wrap">
      <article class="ugv-card ugv-card--fleet">
        <h2>Fleet pulse</h2>
        <p class="ugv-hint">Mode: ${dataMode} — aggregate chassis stress</p>
        <svg id="ugv-radial-fleet" class="ugv-svg"></svg>
      </article>
      <article class="ugv-card ugv-card--devices">
        <h2>Per-device arcs</h2>
        <svg id="ugv-radial-devices" class="ugv-svg ugv-svg--grid"></svg>
      </article>
    </div>`;

  function render({ insights: ins, dataMode: mode }) {
    const summary = ins.summary || {};
    const devices = deviceRowsFromInsights(ins);

    const fleetSvg = d3.select('#ugv-radial-fleet');
    fleetSvg.selectAll('*').remove();
    const fw = rootEl.clientWidth > 600 ? 480 : 320;
    fleetSvg.attr('viewBox', `0 0 ${fw} 160`);
    const metrics = [
      { v: summary.avg_chassis_cpu_pct || 0, label: 'CPU', c: '#f472b6' },
      { v: summary.avg_chassis_mem_pct || 0, label: 'Memory', c: '#a78bfa' },
      { v: summary.ports_owned_pct || 0, label: 'Owned', c: '#38bdf8' },
      { v: summary.ports_traffic_pct || 0, label: 'Traffic', c: '#4ade80' },
    ];
    metrics.forEach((m, i) => drawRadialGauge(fleetSvg, 60 + i * (fw / 4), 72, 42, m.v, m.c, m.label));

    const devSvg = d3.select('#ugv-radial-devices');
    devSvg.selectAll('*').remove();
    const cols = Math.max(3, Math.min(6, Math.ceil(Math.sqrt(devices.length || 1))));
    const cell = 100;
    const rows = Math.ceil(Math.max(1, devices.length) / cols);
    devSvg.attr('viewBox', `0 0 ${cols * cell} ${rows * cell + 20}`);
    devices.forEach((d, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const stress = (d.cpu + d.mem) / 2;
      drawRadialGauge(devSvg, col * cell + 50, row * cell + 50, 36, stress, pctColor(stress), d.label);
    });

    if (detailEl) {
      detailEl.hidden = false;
      detailEl.innerHTML = `<h3>Radial summary</h3>
        <p>${devices.length} devices · ${summary.ports_total || 0} ports tracked</p>
        <p class="ugv-hint">Arc fill = blended CPU+memory stress. Click a device arc in future builds for drill-down.</p>`;
    }
  }

  return { render };
}
