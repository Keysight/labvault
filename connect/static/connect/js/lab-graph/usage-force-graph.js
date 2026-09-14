/**
 * D3 force-directed port utilization map (LabPortUsageGraphV1).
 */
const TIER_COLORS = {
  switch: '#4ade80',
  chassis: '#a78bfa',
  ocs: '#fb923c',
  firewall: '#f87171',
  generic: '#64748b',
};

function heatColor(duty, inUse) {
  if (inUse) return '#ef4444';
  if (duty >= 0.75) return '#f97316';
  if (duty >= 0.5) return '#f59e0b';
  if (duty >= 0.25) return '#3b82f6';
  if (duty > 0.05) return '#22c55e';
  return '#475569';
}

export function renderUsageGraph(container, graph, { onSelect } = {}) {
  const el = typeof container === 'string' ? document.querySelector(container) : container;
  if (!el || !window.d3) return null;

  const d3 = window.d3;
  el.innerHTML = '';
  const width = el.clientWidth || 960;
  const height = el.clientHeight || 640;

  const svg = d3.select(el).append('svg').attr('width', '100%').attr('height', '100%').attr('viewBox', [0, 0, width, height]);
  const gRoot = svg.append('g');
  const zoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', (e) => gRoot.attr('transform', e.transform));
  svg.call(zoom);

  const deviceById = new Map((graph.devices || []).map((d) => [d.id, d]));
  const deviceNodes = (graph.nodes || []).map((n) => ({
    id: n.id,
    kind: 'device',
    label: n.label || n.id,
    nodeType: n.kind || 'generic',
    duty: (n.utilization && n.utilization.duty_cycle_31d) || 0,
    reserved: Boolean(n.reservation),
    r: 22,
  }));

  const portNodes = (graph.port_nodes || []).map((p) => ({
    id: p.id,
    kind: 'port',
    label: p.label,
    parent: p.parent_device,
    duty: p.duty_cycle_31d || 0,
    inUse: p.in_use,
    reserved: p.reserved,
    color: p.color || heatColor(p.duty_cycle_31d || 0, p.in_use),
    r: 5 + Math.min(8, (p.duty_cycle_31d || 0) * 12),
  }));

  const nodes = deviceNodes.length ? [...deviceNodes, ...portNodes] : portNodes;
  const links = (graph.port_links || []).map((l) => ({
    source: l.source,
    target: l.target,
    duty: l.duty_cycle_31d || 0,
    color: l.color,
  }));

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id((d) => d.id).distance(48).strength(0.35))
    .force('charge', d3.forceManyBody().strength(-120))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius((d) => d.r + 4));

  const deviceCenters = new Map();
  deviceNodes.forEach((d, i) => {
    const angle = (i / Math.max(1, deviceNodes.length)) * Math.PI * 2;
    deviceCenters.set(d.id, { x: width / 2 + Math.cos(angle) * 180, y: height / 2 + Math.sin(angle) * 140 });
  });
  sim.force('deviceX', d3.forceX((d) => {
    if (d.kind === 'device') return deviceCenters.get(d.id)?.x ?? width / 2;
    const c = deviceCenters.get(d.parent);
    return c ? c.x + (Math.random() - 0.5) * 40 : width / 2;
  }).strength((d) => (d.kind === 'device' ? 0.12 : 0.08)));
  sim.force('deviceY', d3.forceY((d) => {
    if (d.kind === 'device') return deviceCenters.get(d.id)?.y ?? height / 2;
    const c = deviceCenters.get(d.parent);
    return c ? c.y + (Math.random() - 0.5) * 40 : height / 2;
  }).strength((d) => (d.kind === 'device' ? 0.12 : 0.08)));

  const link = gRoot.append('g').attr('stroke-opacity', 0.55).selectAll('line').data(links).join('line')
    .attr('stroke', (d) => d.color || heatColor(d.duty, false))
    .attr('stroke-width', (d) => 1 + d.duty * 3);

  const node = gRoot.append('g').selectAll('g').data(nodes).join('g').attr('cursor', 'pointer')
    .on('click', (_, d) => { if (onSelect) onSelect(d, deviceById.get(d.parent || d.id)); });

  node.append('circle')
    .attr('r', (d) => d.r)
    .attr('fill', (d) => {
      if (d.kind === 'port') return d.color;
      if (d.reserved) return '#1d4ed8';
      return heatColor(d.duty, false);
    })
    .attr('stroke', (d) => (d.kind === 'device' ? TIER_COLORS[d.nodeType] || TIER_COLORS.generic : '#0f172a'))
    .attr('stroke-width', (d) => (d.kind === 'device' ? 2.5 : 1));

  node.append('text')
    .text((d) => (d.kind === 'device' ? d.label : d.label))
    .attr('x', (d) => (d.kind === 'device' ? 0 : 8))
    .attr('y', (d) => (d.kind === 'device' ? d.r + 12 : 3))
    .attr('text-anchor', (d) => (d.kind === 'device' ? 'middle' : 'start'))
    .attr('fill', '#e2e8f0')
    .attr('font-size', (d) => (d.kind === 'device' ? 11 : 9))
    .attr('pointer-events', 'none');

  sim.on('tick', () => {
    link
      .attr('x1', (d) => d.source.x)
      .attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x)
      .attr('y2', (d) => d.target.y);
    node.attr('transform', (d) => `translate(${d.x},${d.y})`);
  });

  return { svg, sim, destroy: () => { sim.stop(); el.innerHTML = ''; } };
}

export function formatDuty(pct) {
  return `${Math.round((pct || 0) * 100)}%`;
}
