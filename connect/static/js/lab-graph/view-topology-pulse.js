/**
 * Topology Pulse — force graph with node size/color = usage stress.
 */

function pctColor(v) {
  if (v >= 80) return '#ef4444';
  if (v >= 55) return '#f59e0b';
  if (v >= 30) return '#38bdf8';
  return '#22c55e';
}

export function initView({ rootEl, detailEl }) {
  rootEl.innerHTML = `
    <article class="ugv-card ugv-card--wide">
      <h2>Topology stress pulse</h2>
      <p class="ugv-hint">Node radius and color encode blended stress — drag to explore fabric</p>
      <div class="ugv-topo-wrap">
        <svg id="ugv-topo-svg" class="ugv-svg ugv-svg--graph"></svg>
      </div>
    </article>`;

  let simulation = null;

  function render({ insights, fabric, dataMode }) {
    const graph = insights.stress_graph || {};
    let nodes = (graph.nodes || []).map((n) => ({ ...n }));
    let links = (graph.edges || []).map((e) => ({ ...e }));

    if (!nodes.length && fabric?.nodes?.length) {
      nodes = fabric.nodes.slice(0, 40).map((n) => ({
        id: n.id || n.label,
        label: n.label || n.id,
        type: 'device',
        stress: n.utilization_pct || 20,
      }));
      links = (fabric.links || fabric.port_links || []).slice(0, 60).map((l) => ({
        source: l.source || l.from,
        target: l.target || l.to,
        type: 'fabric',
      }));
    }

    const svgEl = document.getElementById('ugv-topo-svg');
    const svg = d3.select(svgEl);
    svg.selectAll('*').remove();

    const width = svgEl.clientWidth || 900;
    const height = 460;
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    if (!nodes.length) {
      svg.append('text').attr('x', 24).attr('y', 40).attr('fill', '#64748b')
        .text('No topology nodes — add devices and fabric links first.');
      return;
    }

    if (simulation) simulation.stop();

    const g = svg.append('g');
    svg.call(d3.zoom().scaleExtent([0.25, 3]).on('zoom', (ev) => g.attr('transform', ev.transform)));

    const link = g.append('g').attr('stroke-opacity', 0.5).selectAll('line')
      .data(links).join('line')
      .attr('stroke', '#64748b')
      .attr('stroke-width', (d) => Math.max(1, (d.weight || 8) / 20));

    const node = g.append('g').selectAll('g')
      .data(nodes).join('g')
      .attr('cursor', 'grab')
      .call(d3.drag()
        .on('start', (ev, d) => { if (!ev.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on('end', (ev, d) => { if (!ev.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

    node.append('circle')
      .attr('r', (d) => (d.type === 'device' ? 12 + (d.stress || 0) / 6 : 6 + (d.stress || 0) / 25))
      .attr('fill', (d) => pctColor(d.stress || 0))
      .attr('stroke', (d) => (d.role === 'bridge' ? '#fbbf24' : '#0f172a'))
      .attr('stroke-width', (d) => (d.role === 'bridge' ? 2.5 : 1))
      .attr('opacity', 0.92);

    node.append('text')
      .text((d) => String(d.label || '').slice(0, 12))
      .attr('x', 14)
      .attr('y', 4)
      .attr('fill', '#cbd5e1')
      .attr('font-size', 9)
      .attr('pointer-events', 'none');

    node.on('click', (_, d) => {
      if (!detailEl) return;
      detailEl.hidden = false;
      detailEl.innerHTML = `
        <h3>${d.label || d.id}</h3>
        <p>Stress: <strong>${d.stress ?? '—'}%</strong></p>
        <p>Type: ${d.type || 'node'}${d.role ? ` · ${d.role}` : ''}</p>
        ${d.ports_hot != null ? `<p>Hot ports: ${d.ports_hot}</p>` : ''}
        <p class="ugv-hint">${dataMode} mode · gold ring = bridge node</p>`;
    });

    simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id((d) => d.id).distance(80).strength(0.4))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius((d) => 16 + (d.stress || 0) / 10))
      .on('tick', () => {
        link
          .attr('x1', (d) => d.source.x)
          .attr('y1', (d) => d.source.y)
          .attr('x2', (d) => d.target.x)
          .attr('y2', (d) => d.target.y);
        node.attr('transform', (d) => `translate(${d.x},${d.y})`);
      });

    if (detailEl) {
      detailEl.hidden = false;
      detailEl.innerHTML = `<h3>Topology pulse</h3><p>${nodes.length} nodes · ${links.length} links</p>
        <p class="ugv-hint">Click a node for details. ${graph.meta?.graph_thinking || ''}</p>`;
    }
  }

  return { render };
}
