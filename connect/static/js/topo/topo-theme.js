/**
 * Shared topology visual theme (NormalizedGraph / Fabric Map / Designer).
 * @see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §4.1
 */
export const TOPO_THEME = {
  bg_canvas: '#050d1a',
  bg_panel: '#0d1b2a',
  bg_card: '#0a1628',
  bg_hover: '#1a2d45',
  border_dim: '#1e3a5f',
  border_bright: '#2e5080',
  text_primary: '#f1f5f9',
  text_secondary: '#94a3b8',
  text_muted: '#4a6280',
  link_up: '#22c55e',
  link_stale: '#f59e0b',
  link_down: '#ef4444',
  link_planned: '#64748b',
  link_ocs: '#fb923c',
  link_conflict: '#ef4444',
  link_reserved: '#3b82f6',
  link_selected: '#ffffff',
  status_online: '#22c55e',
  status_offline: '#ef4444',
  status_maint: '#f59e0b',
  status_unknown: '#64748b',
  icons: {
    switch: 'fa-network-wired',
    chassis: 'fa-microchip',
    ocs: 'fa-project-diagram',
    server: 'fa-server',
    firewall: 'fa-shield-alt',
    generic: 'fa-cube',
  },
  team_colors: ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4'],
};

export function linkColorForHealth(health, linkType) {
  if (health === 'conflict') return TOPO_THEME.link_conflict;
  if (linkType === 'ocs_active' || linkType === 'ocs') {
    return health === 'active' || health === 'up' ? TOPO_THEME.link_ocs : TOPO_THEME.link_planned;
  }
  if (health === 'up' || health === 'active') return TOPO_THEME.link_up;
  if (health === 'stale' || health === 'lldp_only') return TOPO_THEME.link_stale;
  if (health === 'down' || health === 'alarm') return TOPO_THEME.link_down;
  return TOPO_THEME.link_planned;
}
