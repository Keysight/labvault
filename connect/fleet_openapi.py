"""Full OpenAPI 3.0 document for the LabVault fleet Bearer APIs."""
from __future__ import annotations


def fleet_openapi_document(*, server_url: str = '/', absolute_server_url: str | None = None) -> dict:
    bearer = {'bearerAuth': []}

    def get_op(summary: str, description: str = '', *, params=None, story: str = '', response_schema=None):
        op = {
            'summary': summary,
            'description': description or summary,
            'security': [bearer],
            'tags': [story] if story else ['fleet'],
            'responses': {
                '200': {'description': 'OK'},
                '401': {'description': 'Unauthorized'},
                '403': {'description': 'Forbidden (auth or mutation gate)'},
                '409': {'description': 'Conflict / preflight failed'},
            },
        }
        if params:
            op['parameters'] = params
        if response_schema:
            op['responses']['200'] = {
                'description': 'OK',
                'content': {
                    'application/json': {'schema': response_schema},
                },
            }
        return {'get': op}

    def post_op(
        summary: str,
        description: str = '',
        *,
        story: str = '',
        body_props=None,
        required_body=None,
        response_schema=None,
        extra_responses=None,
    ):
        op = {
            'summary': summary,
            'description': description or summary,
            'security': [bearer],
            'tags': [story] if story else ['fleet'],
            'responses': {
                '200': {'description': 'OK'},
                '201': {'description': 'Created'},
                '401': {'description': 'Unauthorized'},
                '403': {'description': 'Blocked when LABVAULT_DEMO_MODE=true and ALLOW_MUTATE=false'},
                '400': {'description': 'Bad request'},
                '502': {'description': 'Upstream device error'},
            },
        }
        if extra_responses:
            op['responses'].update(extra_responses)
        if body_props:
            schema = {'type': 'object', 'properties': body_props}
            if required_body:
                schema['required'] = list(required_body)
            op['requestBody'] = {
                'required': True,
                'content': {
                    'application/json': {
                        'schema': schema,
                    }
                },
            }
        if response_schema:
            op['responses']['200'] = {
                'description': 'OK',
                'content': {
                    'application/json': {'schema': response_schema},
                },
            }
        return {'post': op}

    chassis_id = {
        'name': 'chassis_id', 'in': 'path', 'required': True,
        'schema': {'type': 'integer'}, 'description': 'KeysightChassis primary key',
    }
    device_ip = {
        'name': 'device_ip', 'in': 'path', 'required': True,
        'schema': {'type': 'string', 'example': '192.0.2.10'},
        'description': 'OCS management IPv4/IPv6 (Device.ip_address)',
    }

    ocs_xconnect_item = {
        'type': 'object',
        'properties': {
            'port_a': {'type': 'string', 'example': '1.1.1'},
            'port_b': {'type': 'string', 'example': '2.1.1'},
            'name': {'type': 'string', 'example': '1.1.1-2.1.1'},
            'state': {
                'type': 'string',
                'enum': ['OK', 'FAIL'],
                'description': 'FAIL if either optical half failed',
            },
            'raw': {'type': 'object', 'additionalProperties': True},
        },
    }
    ocs_list_schema = {
        'type': 'object',
        'properties': {
            'ok': {'type': 'boolean', 'example': True},
            'device_ip': {'type': 'string', 'example': '192.0.2.10'},
            'device_hostname': {'type': 'string', 'example': 'ocs-controller.lab.example'},
            'count': {'type': 'integer', 'example': 50},
            'crossconnects': {'type': 'array', 'items': ocs_xconnect_item},
        },
    }
    ocs_mutate_schema = {
        'type': 'object',
        'properties': {
            'ok': {'type': 'boolean'},
            'action': {'type': 'string', 'enum': ['xconnect_add', 'xconnect_delete']},
            'port_a': {'type': 'string'},
            'port_b': {'type': 'string'},
            'name': {'type': 'string'},
            'result': {'type': 'object', 'additionalProperties': True},
            'error': {'type': 'string'},
        },
    }

    paths = {
        '/api/fleet/': get_op('Fleet API index', 'Lists all fleet endpoints and auth note.', story='meta'),
        '/api/fleet/openapi.json': get_op(
            'OpenAPI 3.0 document',
            'Machine-readable OpenAPI for this API surface.',
            story='meta',
        ),
        '/api/docs/': get_op('Swagger UI', 'Interactive Swagger UI for the fleet APIs.', story='meta'),
        '/api/fleet/health.json': get_op(
            'Fleet health',
            'Per-chassis status, last_seen, CPU/mem, HW-error flags, recovery_actions[].',
            story='oncaller',
        ),
        '/api/fleet/heartbeat.json': get_op(
            'Fleet heartbeat snapshot',
            'heartbeat_ok, heartbeat_age_s, halt_suspect, halt_reason, probe latency.',
            story='oncaller',
        ),
        '/api/fleet/heartbeat/stream': get_op(
            'Fleet heartbeat SSE stream',
            'Server-Sent Events push each heartbeat interval (fallback: poll heartbeat.json every 2s).',
            story='oncaller',
        ),
        '/api/fleet/chassis/{chassis_id}/health.json': get_op(
            'Single-chassis deep health',
            'Chassis + PCPU telemetry + recovery hints.',
            story='oncaller',
            params=[chassis_id],
        ),
        '/api/fleet/chassis/{chassis_id}/recover': {
            **post_op(
                'Trigger recovery action',
                'reboot_chassis | power_cycle_node | restart_node | port_reboot.',
                story='oncaller',
                body_props={
                    'action': {'type': 'string', 'example': 'reboot_chassis'},
                    'node_name': {'type': 'string'},
                    'port_id': {'type': 'integer'},
                },
            ),
            'parameters': [chassis_id],
        },
        '/api/fleet/ports/telemetry.json': get_op(
            'Port telemetry',
            'Per-port link, owner, reserved, bps_in/out, CPU/mem, sample age.',
            story='test_user',
        ),
        '/api/fleet/ports/preflight.json': get_op(
            'Preflight gate',
            'Pass/fail blockers: reserved, link-down, owned-by-other, HW-error, chassis_api_ok, '
            'halt_suspect. HTTP 409 when blockers present.',
            story='test_user',
            params=[{
                'name': 'topology_id', 'in': 'query', 'required': False,
                'schema': {'type': 'integer'},
            }],
        ),
        '/api/fleet/chassis/{chassis_id}/ports.json': get_op(
            'Ports for one chassis',
            story='test_user',
            params=[chassis_id],
        ),
        '/api/fleet/sla.json': get_op(
            'Fleet SLA / uptime',
            'Aggregated uptime %, availability windows, breach counts.',
            story='em_director',
        ),
        '/api/fleet/summary.json': get_op(
            'Fleet summary rollup',
            'Chassis online, ports used/free, aggregate bps.',
            story='em_director',
        ),
        '/api/fleet/metrics/transmission.json': get_op(
            'Transmission metrics',
            'Port-level transmission timeseries (bps).',
            story='em_director',
            params=[{
                'name': 'window', 'in': 'query', 'required': False,
                'schema': {'type': 'string', 'example': '1h'},
            }],
        ),
        '/api/fleet/inventory.json': get_op(
            'Fleet inventory JSON',
            'Chassis + cards + ports + team tags + reservation state.',
            story='lab_admin',
        ),
        '/api/fleet/inventory.csv': get_op(
            'Fleet inventory CSV',
            'Same inventory as CSV for audits.',
            story='lab_admin',
        ),
        '/api/fleet/reservations.json': get_op(
            'List reservations',
            'Current + upcoming Keysight reservations.',
            story='lab_admin',
        ),
        '/api/fleet/reservations': post_op(
            'Create reservation',
            'Create a reservation.',
            story='lab_admin',
            body_props={
                'title': {'type': 'string'},
                'chassis_id': {'type': 'integer'},
                'duration_hours': {'type': 'integer', 'example': 4},
                'slot': {'type': 'integer'},
                'port': {'type': 'integer'},
                'notes': {'type': 'string'},
            },
        ),
        '/api/fleet/chassis/{chassis_id}/team-tags': {
            **post_op(
                'Set team tags',
                'Replace ownership team tags on a chassis.',
                story='lab_admin',
                body_props={'team_tags': {'type': 'string', 'example': 'lab-a,team-a'}},
            ),
            'parameters': [chassis_id],
        },
        '/api/fleet/ownership.json': get_op(
            'Port ownership',
            'IxOS owner + LabVault reservations + team tags across fleet.',
            story='infra_sre',
        ),
        '/api/fleet/conflicts.json': get_op(
            'Allocation conflicts',
            'Overlapping / double-booked reservation report.',
            story='infra_sre',
        ),
        '/api/ocs/{device_ip}/crossconnects/': get_op(
            'List OCS crossconnects',
            'Live crossconnect list from Calient/OCS REST (credentials stored on Device). '
            'Example device_ip: 192.0.2.10',
            story='infra_sre',
            params=[device_ip],
            response_schema=ocs_list_schema,
        ),
        '/api/ocs/{device_ip}/crossconnect/': {
            **post_op(
                'Mutate OCS crossconnect',
                'action=xconnect_add|xconnect_delete. Requires free ports for add. '
                'Returns 502 when OCS rejects (port in use / invalid).',
                story='infra_sre',
                body_props={
                    'action': {
                        'type': 'string',
                        'enum': ['xconnect_add', 'xconnect_delete'],
                        'example': 'xconnect_add',
                    },
                    'port_a': {'type': 'string', 'example': '1.3.3'},
                    'port_b': {'type': 'string', 'example': '1.3.4'},
                    'name': {'type': 'string', 'example': 'LV_TEST_133_134'},
                },
                required_body=['action'],
                response_schema=ocs_mutate_schema,
                extra_responses={
                    '404': {'description': 'OCS device_ip not found in LabVault Device table'},
                },
            ),
            'parameters': [device_ip],
        },
    }

    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'LabVault Fleet API',
            'version': '1.2.0',
            'description': (
                'Bearer-authenticated fleet APIs for LabVault. '
                'KENG maps to AresONE / IxOS chassis. Auth: '
                '`Authorization: Bearer <token>` or Django session cookie.'
            ),
        },
        'servers': (
            [
                {'url': server_url or '/', 'description': 'Same origin as this page (recommended)'},
            ]
            + (
                [{'url': absolute_server_url, 'description': 'This LabVault instance'}]
                if absolute_server_url and absolute_server_url.rstrip('/') not in ('', server_url)
                else []
            )
        ),
        'tags': [
            {'name': 'meta', 'description': 'Discovery / OpenAPI'},
            {'name': 'oncaller', 'description': 'Health, heartbeat, recovery'},
            {'name': 'test_user', 'description': 'Port telemetry and preflight'},
            {'name': 'em_director', 'description': 'SLA and transmission metrics'},
            {'name': 'lab_admin', 'description': 'Inventory and reservations'},
            {'name': 'infra_sre', 'description': 'Ownership, conflicts, OCS'},
        ],
        'paths': paths,
        'components': {
            'securitySchemes': {
                'bearerAuth': {
                    'type': 'http',
                    'scheme': 'bearer',
                    'bearerFormat': 'API token',
                    'description': 'Create under Settings → API Tokens, or use seeded demo-api token.',
                },
            },
            'schemas': {
                'OcsCrossconnect': ocs_xconnect_item,
                'OcsCrossconnectList': ocs_list_schema,
                'OcsCrossconnectMutate': ocs_mutate_schema,
            },
        },
        'security': [bearer],
    }
