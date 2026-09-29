# `connect/static/` — CSS, JavaScript and images

Listed in `STATICFILES_DIRS`; `python manage.py collectstatic` copies it to `STATIC_ROOT`
(`<repo>/staticfiles`), which nginx (or the fallback `django.views.static.serve` route in
`labvault/urls.py`) serves at `/static/`. Templates reference files with
`{% static 'css/…' %}` / `{% static "js/…" %}`. There is no build step or bundler; files are
served as written.

Bootstrap 5.3, Bootstrap JS and Font Awesome 6.4 are loaded from public CDNs by
`templates/connect/base.html` and `login.html`, not from this folder.

## Directory map

| Path | Contents | Used by |
|---|---|---|
| `branding/` | `mark-{light,dark}-scheme.png` (sidebar logo), `mark-{light,dark}-favicon.png` | `base.html`; `/favicon.ico` redirects to `mark-light-favicon.png` |
| `css/chassis.css`, `js/chassis.js` | Keysight chassis detail styling and behaviour | `keysight/chassis_detail.html` |
| `css/topo-topbar.css` | Lab Topology top bar | `includes/lab_topology_topbar.html` |
| `css/topo-designer-theme.css`, `css/topo-view-theme.css` | Designer / view themes | No direct template reference found |
| `css/usage-*.css`, `css/usage-insights.js` | Usage page, usage graph views, usage insights styling | `lab_topology_usage*.html` |
| `js/topo/` | Network topology map client: `topo-map-init.js` (entry point that wires inspector, legend, map client and toolbar), `topo-theme.js`, `topo-graph-client.js`, `topo-canvas.js`, `topo-editor.js` | `topology.html` (via `topo-map-init.js`) |
| `js/topo-*.js` | Designer add-ons: `topo-import.js`, `topo-layout-modes.js`, `topo-metrics-toggle.js`, `topo-topo-name.js`, `topo-view-theme.js` | Lab topology pages and top bar (`topo-view-theme.js` has no direct reference) |
| `js/lab-graph/` | Usage / timeline graph engine: `usage-graph-shell.js`, `usage-graph-data.js`, `usage-data-mode.js`, `usage-granular-filters.js`, `usage-force-graph.js`, `timeline-graph.js`, `timeline-filters.js`, `chassis-layout.js`, `usage-insights.js`, `usage-page-theme.js`, and one file per graph view (`view-pulse-radial.js`, `view-stream-wave.js`, `view-radar-health.js`, `view-matrix-heat.js`, `view-topology-pulse.js`) selected by name in `lab_usage_graph_views.py` | `lab_topology_usage*.html`, `includes/lab_topology_topbar.html` |
| `connect/js/lab-graph/usage-force-graph.js` | Second copy of the force-graph module under a namespaced path | — |
| `css/bootstrap.min.css`, `js/jquery-3.6.0.min.js` | Vendored libraries | No template references them |
| `style.css`, `arista-style.css`, `css/style.css`, `css/styles.css`, `css/arista.css`, `js/arista.js`, `images/arista_dut.jpg`, `favicon.svg` | Older theme and vendor-specific assets | No direct reference found by filename search; likely legacy |

The "Used by" column comes from a filename search of `connect/templates`, `connect/static`
and `connect/*.py`; JavaScript that builds paths dynamically may load additional files.

## Adding assets

1. Put the file under `css/` or `js/` (use a subfolder for a multi-file module).
2. Reference it from the page template inside `{% block extra_css %}` or `{% block extra_js %}`
   with `{% load static %}` / `{% static '…' %}`.
3. Run `collectstatic` on deploy; `health/ready` reports `static_empty` if `STATIC_ROOT`
   exists but is empty in non-DEBUG mode.
4. Do not add CDN-only dependencies for new pages if the install may be air-gapped; vendor the
   file here instead.

Full description: [docs/development/subsystems/core-web.md](../../docs/development/subsystems/core-web.md).
