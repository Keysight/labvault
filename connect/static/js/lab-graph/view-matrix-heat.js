/**
 * Matrix Heat — device × time heatmap with brush range selection.
 */

export function initView({ rootEl, detailEl }) {
  rootEl.innerHTML = `
    <article class="ugv-card ugv-card--wide">
      <h2>Device × time matrix</h2>
      <p class="ugv-hint">CPU % per device over buckets — drag brush to select a range</p>
      <div class="ugv-heat-controls">
        <label>Metric
          <select id="ugv-heat-metric">
            <option value="cpu_pct">CPU %</option>
            <option value="mem_pct">Memory %</option>
            <option value="bps_total">Traffic (bps)</option>
          </select>
        </label>
        <span id="ugv-brush-label" class="ugv-hint-inline">Full window</span>
      </div>
      <svg id="ugv-matrix-svg" class="ugv-svg ugv-svg--tall"></svg>
    </article>`;

  let brushSelection = null;

  function render({ timeline, insights, dataMode }) {
    const metricSel = document.getElementById('ugv-heat-metric');
    const metric = metricSel?.value || 'cpu_pct';
    if (metricSel && !metricSel._bound) {
      metricSel._bound = true;
      metricSel.addEventListener('change', () => render({ timeline, insights, dataMode }));
    }

    const buckets = timeline?.metric_buckets || {};
    const catalog = timeline?.catalog || {};
    const deviceKeys = Object.keys(catalog).filter((k) => !catalog[k]?.parent).slice(0, 24);

    const rows = deviceKeys.map((dk) => {
      const label = catalog[dk]?.label || dk;
      const rk = dk;
      const pts = buckets[rk]?.[metric] || buckets[`${rk}__1.1`]?.[metric] || [];
      return { key: rk, label, pts };
    }).filter((r) => r.pts.length > 0);

    const svg = d3.select('#ugv-matrix-svg');
    svg.selectAll('*').remove();

    const width = Math.max(640, rootEl.clientWidth - 32);
    const rowH = 18;
    const height = Math.max(200, rows.length * rowH + 50);
    const margin = { top: 10, right: 12, bottom: 24, left: 120 };
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    if (!rows.length) {
      svg.append('text').attr('x', 40).attr('y', 40).attr('fill', '#64748b').text('No bucketed metrics for heat matrix');
      return;
    }

    const tCount = Math.max(...rows.map((r) => r.pts.length));
    const x = d3.scaleLinear().domain([0, tCount - 1]).range([margin.left, width - margin.right]);
    const y = d3.scaleBand().domain(rows.map((r) => r.label)).range([margin.top, height - margin.bottom]).padding(0.08);

    let vMax = 100;
    if (metric === 'bps_total') {
      vMax = d3.max(rows.flatMap((r) => r.pts.map((p) => Number(p[1]) || 0))) || 1;
    }
    const color = d3.scaleSequential(d3.interpolateInferno).domain([0, vMax]);

    const g = svg.append('g');
    rows.forEach((row) => {
      row.pts.forEach((p, i) => {
        const v = Number(p[1]) || 0;
        g.append('rect')
          .attr('x', x(i) - 2)
          .attr('y', y(row.label))
          .attr('width', Math.max(2, (width - margin.left - margin.right) / tCount - 1))
          .attr('height', y.bandwidth())
          .attr('fill', color(v))
          .attr('rx', 1)
          .append('title')
          .text(`${row.label}: ${v}`);
      });
      g.append('text')
        .attr('x', margin.left - 6)
        .attr('y', (y(row.label) || 0) + y.bandwidth() / 2)
        .attr('text-anchor', 'end')
        .attr('dominant-baseline', 'middle')
        .attr('fill', '#94a3b8')
        .attr('font-size', 9)
        .text(row.label.length > 14 ? `${row.label.slice(0, 12)}…` : row.label);
    });

    const brush = d3.brushX()
      .extent([[margin.left, margin.top], [width - margin.right, height - margin.bottom]])
      .on('end', (ev) => {
        brushSelection = ev.selection;
        const lbl = document.getElementById('ugv-brush-label');
        if (!brushSelection) {
          if (lbl) lbl.textContent = 'Full window';
          return;
        }
        const [x0, x1] = brushSelection;
        const i0 = Math.round(x.invert(x0));
        const i1 = Math.round(x.invert(x1));
        if (lbl) lbl.textContent = `Buckets ${i0}–${i1} (${dataMode})`;
        if (detailEl) {
          detailEl.hidden = false;
          detailEl.innerHTML = `<h3>Brush range</h3><p>Selected buckets ${i0} to ${i1} of ${tCount}.</p>
            <p class="ugv-hint">Use Usage timeline for port-level drill-down in this range.</p>`;
        }
      });

    svg.append('g').attr('class', 'ugv-brush').call(brush);

    if (detailEl && !brushSelection) {
      detailEl.hidden = false;
      detailEl.innerHTML = `<h3>Heat matrix</h3><p>${rows.length} devices × ${tCount} buckets</p>`;
    }
  }

  return { render };
}
