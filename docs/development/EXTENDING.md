# Extending LabVault

Where to change code, and which doc has the step-by-step. Do not add a free-form device shell.

## Add a page

1. View function in the module that owns the area (`connect/views.py` only if
   it is generic device UI; otherwise the subsystem module).
2. `path(...)` in `connect/urls.py`.
3. Template under `connect/templates/connect/`, extending `base.html`.
4. `@login_required`, or `@staff_member_required`, or `_api_auth_required` for
   Bearer tokens.
5. A row in [MODULES.md](../MODULES.md) and a paragraph in the subsystem doc.

Pattern and middleware order: [core-web.md](subsystems/core-web.md).

## Add a vendor driver

New module under `connect/drivers/` subclassing `BaseDriver`. Register it in
`connect/drivers/__init__.py` (`get_driver`, `VENDOR_CHOICES`, `VENDOR_COMMANDS`).
Return `DriverResult`. No I/O in `__init__`. Cover `probe`, `get_system_info`,
and `get_interfaces` at minimum. Add a unittest that mocks the transport.

Full checklist (forms, manifest, tests): [drivers.md](subsystems/drivers.md).

## Add a fleet API endpoint

View in `connect/fleet_api_views.py` using `_api_auth_required`, route in
`connect/urls.py`, entry in `fleet_openapi.py` (the spec, the index, and the
URL list are three copies and drift if you update only one), and a test under
`connect/tests/`.

Steps: [fleet-api.md](subsystems/fleet-api.md).

## Add a CLI command

Decorator `@command` in `connect/labvault_cli/commands/__init__.py`. Set the
tier (`read` / `write` / `service`). Write commands that change lab gear or
restart services require a confirmation nonce. Redact secrets before they
reach `CliInvocation`. Registering the module is enough; `__init__.py` imports
`commands` at startup.

Steps: [cli.md](subsystems/cli.md).

## Add a metric

1. Collect in `connect/metric_collectors.py` inside `collect_all_topologies`
   (respect `collector_mode`; idle must not open device connections).
2. Write `LabMetricSample` or `LabResourceEvent` through `lab_metrics.py`
   (`using='np_timeseries'`).
3. If Lab Pulse should chart it, add the metric name to the bucket query and
   to the JS module under `connect/static/js/lab-graph/`.
4. Retention is automatic if the row uses the existing models.

Guide: [metrics-insights.md](subsystems/metrics-insights.md).

## Add a lab-topology node type or export format

Node rendering and JSON `extra` keys live in `lab_topology_views.py` and
`lab_topology_io.py`. Link validation is `topology_link_validate.py`. Export
formats are `topology_export.py`. Bump a schema file under `resources/` only
when the on-disk JSON shape changes, and keep `import_lab_topology` able to
read the previous shape.

Guide: [lab-topology-designer.md](subsystems/lab-topology-designer.md).

## Add a management command

`connect/management/commands/<name>.py` with a `Command` subclass. Document it
in the command table in [operations.md](subsystems/operations.md) and in
`connect/management/commands/README.md`. If it should run forever, add a unit
to `deploy/systemd/`, a service to `deploy/compose/docker-compose.yml`, and an
entry in `opsd/service_catalog.py`. Commands that write device config must
support `--dry-run`.

## Add a diagnostic check

Append a check function in `connect/diagnostics.py` and include its result in
`build_diagnostics_payload`. Say what "warn" means and whether anything
auto-recovers (most checks do not). Update
[diagnostics.md](subsystems/diagnostics.md).

## Tests and gates before you finish

```bash
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
python manage.py test connect.tests.test_hard_dump connect.tests.test_bootstrap_defaults --verbosity=1
python tools/check_public_source.py
python tools/check_docs.py
```

`check_public_source.py` fails the tree if a doc or module names an internal
lab host, a private lab address prefix, a published demo password, or a product
that this SKU does not ship. Use example addresses `192.0.2.0/24` and
`2001:db8::/32`.
