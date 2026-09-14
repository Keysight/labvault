/**
 * Radar Health — spider charts comparing devices across metric dimensions.
 */

import { deviceRowsFromInsights } from './usage-graph-data.js';

const AXES = [
  { key: 'cpu', label: 'CPU' },
  { key: 'mem', label: 'Mem' },
  { key: 'owned', label: 'Owned' },
  { key: 'traffic', label: 'Traffic' },
];

const PALETTE = ['#f472b6', '#38bdf8', '#a78bfa', '#4ade80', '#fb923c', '#fbbf24'];

export function initView({ rootEl, detailEl }) {
  rootEl.innerHTML = `
    <article class="ugv-card ugv-card--wide">
      <h2>Device health radar</h2>
      <p class="ugv-hint">Compare up to 6 devices across four utilization dimensions</p>
      <svg id="ugv-radar-svg" class="ugv-svg ugv-svg--tall"></svg>
      <div class="ugv-legend" id="ugv-radar-legend"></div>
    </article>`;

  function render({ insights, dataMode }) {
    const devices = deviceRowsFromInsights(insights).slice(0, 6);
    const svg = d3.select('#ugv-radar-svg');
    svg.selectAll('*').remove();

    const width = Math.max(520, rootEl.clientWidth - 32);
    const height = 400;
    const cx = width / 2;
    const cy = height / 2 + 10;
    const maxR = Math.min(width, height) * 0.32;
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    const angleSlice = (Math.PI * 2) / AXES.length;
    const rScale = d3.scaleLinear().domain([0, 100]).range([0, maxR]);

    const g = svg.append('g').attr('transform', `translate(${cx},${cy})`);

    [25, 50, 75, 100].forEach((pct) => {
      const r = rScale(pct);
      g.append('circle').attr('r', r).attr('fill', 'none').attr('stroke', '#334155').attr('stroke-dasharray', '2,3');
    });

    AXES.forEach((ax, i) => {
      const a = angleSlice * i - Math.PI / 2;
      const x = Math.cos(a) * maxR;
      const y = Math.sin(a) * maxR;
      g.append('line').attr('x1', 0).attr('y1', 0).attr('x2', x).attr('y2', y).attr('stroke', '#475569');
      g.append('text')
        .attr('x', Math.cos(a) * (maxR + 18))
        .attr('y', Math.sin(a) * (maxR + 18))
        .attr('text-anchor', 'middle').attr('dominant-baseline', 'middle')
        .attr('fill', '#94a3b8').attr('font-size', 10)
        .text(ax.label);
    });

    const lineRadial = d3.lineRadial()
      .angle((_, i) => i * angleSlice)
      .radius((d) => rScale(Math.min(100, d)))
      .curve(d3.curveLinearClosed);

    devices.forEach((dev, di) => {
      const vals = AXES.map((ax) => dev[ax.key] || 0);
      const color = PALETTE[di % PALETTE.length];
      g.append('path')
        .attr('d', lineRadial(vals))
        .attr('fill', color)
        .attr('fill-opacity', 0.15)
        .attr('stroke', color)
        .attr('stroke-width', 2);
    });

    const leg = document.getElementById('ugv-radar-legend');
    if (leg) {
      leg.innerHTML = devices.map((d, i) => (
        `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${d.label}</span>`
      )).join('') + `<span class="ugv-hint-inline">${dataMode} mode</span>`;
    }

    if (detailEl) {
      detailEl.hidden = false;
      detailEl.innerHTML = `<h3>Radar compare</h3><p>Showing ${devices.length} of ${(insights.devices || []).length} devices.</p>
        <p class="ugv-hint">Spikes toward an axis = hot dimension for that chassis.</p>`;
    }
  }

  return { render };
}
