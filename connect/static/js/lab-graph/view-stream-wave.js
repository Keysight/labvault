/**
 * Stream Wave — multi-layer streamgraph with event scatter overlays.
 */

import { flattenEvents } from './usage-graph-data.js';

const LAYERS = [
  { key: 'cpu_pct', label: 'CPU', color: '#f472b6' },
  { key: 'mem_pct', label: 'Memory', color: '#a78bfa' },
  { key: 'ports_owned_pct', label: 'Owned', color: '#38bdf8' },
];

export function initView({ rootEl, detailEl }) {
  rootEl.innerHTML = `
    <article class="ugv-card ugv-card--wide">
      <h2>Aggregate waveform</h2>
      <p class="ugv-hint">Stacked stream layers — dots mark ownership & traffic events</p>
      <svg id="ugv-stream-svg" class="ugv-svg ugv-svg--tall"></svg>
      <div class="ugv-legend" id="ugv-stream-legend"></div>
    </article>`;

  function render({ insights, timeline, dataMode }) {
    const fleet = insights.time_series?.fleet || {};
    const svg = d3.select('#ugv-stream-svg');
    svg.selectAll('*').remove();

    const width = Math.max(640, rootEl.clientWidth - 32);
    const height = 340;
    const margin = { top: 20, right: 16, bottom: 28, left: 44 };
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    const seriesData = LAYERS.map((l) => ({
      key: l.key,
      label: l.label,
      color: l.color,
      values: (fleet[l.key] || []).map((p) => ({ date: new Date(p[0]), value: Number(p[1]) || 0 })),
    })).filter((s) => s.values.length > 1);

    if (!seriesData.length) {
      svg.append('text').attr('x', 40).attr('y', height / 2).attr('fill', '#64748b')
        .text('No fleet time-series in this window');
      return;
    }

    const allDates = seriesData[0].values.map((d) => d.date);
    const x = d3.scaleTime().domain(d3.extent(allDates)).range([margin.left, width - margin.right]);

    const stack = d3.stack().keys(seriesData.map((s) => s.key))
      .value((d, key) => d[key] || 0);
    const merged = allDates.map((date, i) => {
      const row = { date };
      seriesData.forEach((s) => { row[s.key] = s.values[i]?.value || 0; });
      return row;
    });
    const stacked = stack(merged);
    const yMax = d3.max(stacked, (layer) => d3.max(layer, (d) => d[1])) || 100;
    const y = d3.scaleLinear().domain([0, yMax * 1.1]).range([height - margin.bottom, margin.top]);

    const area = d3.area()
      .x((d) => x(d.data.date))
      .y0((d) => y(d[0]))
      .y1((d) => y(d[1]))
      .curve(d3.curveBasis);

    const g = svg.append('g');
    stacked.forEach((layer, i) => {
      g.append('path').attr('d', area(layer)).attr('fill', LAYERS[i].color).attr('opacity', 0.75);
    });

    const events = flattenEvents(timeline);
    const yBase = height - margin.bottom;
    events.forEach((ev, idx) => {
      const xPos = x(new Date(ev.ts));
      if (xPos < margin.left || xPos > width - margin.right) return;
      g.append('circle')
        .attr('cx', xPos)
        .attr('cy', yBase - 8 - (idx % 4) * 6)
        .attr('r', 3)
        .attr('fill', ev.type?.includes('owned') ? '#fbbf24' : '#94a3b8')
        .attr('opacity', 0.85)
        .append('title')
        .text(`${ev.type} @ ${ev.resource_key}`);
    });

    svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`)
      .call(d3.axisBottom(x).ticks(6).tickSizeOuter(0))
      .selectAll('text').attr('fill', '#64748b').attr('font-size', 9);

    const leg = document.getElementById('ugv-stream-legend');
    if (leg) {
      leg.innerHTML = LAYERS.map((l) => `<span><i style="background:${l.color}"></i>${l.label}</span>`).join('')
        + `<span class="ugv-hint-inline">${events.length} events · ${dataMode} mode</span>`;
    }

    if (detailEl) {
      detailEl.hidden = false;
      detailEl.innerHTML = `<h3>Stream layers</h3><p>Peak stack height ≈ ${Math.round(yMax)}% combined.</p>
        <p>${events.length} events overlaid on timeline.</p>`;
    }
  }

  return { render };
}
