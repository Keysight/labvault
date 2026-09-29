"""Collect LLDP neighbors from KCOS compute nodes via root SSH hop.

Flow: SSH to the KCOS management (merlin) node as root using the key from
``KCOS_ROOT_SSH_KEY`` (read-only secrets dir), then hop to each compute node's
internal IP and run ``lldpcli``/``lldpctl``. Before collecting we best-effort
*enable* LLDP (start lldpd, widen the interface pattern) so neighbors appear
on test/compute interfaces.

Credentials come from Django settings (``KCOS_ROOT_*`` env vars). Never log
passwords or key material.

Called from :meth:`connect.keysight_drivers.kcos.KCOSDriver.get_lldp_ssh`. Side
effects on the chassis are best-effort LLDP enablement: starting ``lldpd`` and
setting ``lldpcli`` interface patterns on compute nodes; the optional
``merlin_use_k8s_lldpd`` path creates and deletes a short-lived ``lv-lldp-*`` pod
in the ``default`` namespace (off in the in-tree caller). Host keys are
auto-accepted (Paramiko ``AutoAddPolicy``; ``StrictHostKeyChecking=no`` on hops).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Hop options: tolerate older sshd on compute nodes; never prompt.
_INNER_SSH_OPTS = (
    '-o StrictHostKeyChecking=no '
    '-o ConnectTimeout=5 '
    '-o BatchMode=yes '
    '-o KexAlgorithms=+diffie-hellman-group14-sha1 '
    '-o HostKeyAlgorithms=+ssh-rsa '
    '-o PubkeyAcceptedAlgorithms=+ssh-rsa'
)

_LLDP_MARKER = '---LV_LLDP---'
_LLDPD_K8S_IMAGE = 'ghcr.io/lldpd/lldpd:1.0.20'


def resolve_kcos_ssh_key(key_path: str) -> str:
    """Return an OpenSSH private-key path paramiko can load.

    Accepts OpenSSH ``.key`` / PEM paths directly. For PuTTY ``.ppk``, uses a
    sibling ``.key`` file (same basename) that the operator converted locally.
    Do not ship or commit private keys.
    """
    path = Path((key_path or '').strip())
    if not path.is_file():
        return ''
    if path.suffix.lower() == '.ppk':
        openssh_path = path.with_suffix('.key')
        if openssh_path.is_file():
            return str(openssh_path)
        logger.warning(
            'KCOS PuTTY key %s has no OpenSSH sibling %s — '
            'convert the PuTTY key to OpenSSH at that path (mode 0600)',
            path.name, openssh_path.name,
        )
        return ''
    return str(path)


def _lldp_remote_script(interfaces: list[str] | None = None) -> str:
    """Enable lldpd (best-effort) and dump neighbors as JSON.

    When *interfaces* is provided, LLDP send/receive is enabled only on those
    interfaces (M8400 QDD fanout legs, APS100 front-panel NICs). Otherwise all
    interfaces are enabled via pattern ``*``.
    """
    if interfaces:
        ifs = sorted({i.strip() for i in interfaces if (i or '').strip()})
        if ifs:
            iface_setup = ' '.join(
                f"lldpcli configure system interface pattern '{iface}' 2>/dev/null || true;"
                for iface in ifs
            )
        else:
            iface_setup = "lldpcli configure system interface pattern '*' 2>/dev/null || true; "
    else:
        iface_setup = "lldpcli configure system interface pattern '*' 2>/dev/null || true; "
    return (
        'systemctl start lldpd 2>/dev/null || service lldpd start 2>/dev/null || '
        'lldpd 2>/dev/null || true; '
        f'{iface_setup} '
        'sleep 3; '
        f'echo {_LLDP_MARKER}; '
        'lldpcli show neighbors details -f json 2>/dev/null || '
        'lldpctl -f json 2>/dev/null || '
        'lldpcli show neighbors 2>/dev/null || true'
    )


def _parse_lldp_command_output(out: str) -> list[dict]:
    """Strip marker prefix (if present) and parse LLDP JSON or text rows."""
    if _LLDP_MARKER in out:
        out = out.split(_LLDP_MARKER)[-1]
    rows = parse_lldpcli_json(out)
    if rows:
        return rows
    return parse_lldpcli_text(out)


def parse_lldpcli_text(raw: str) -> list[dict]:
    """Parse ``lldpcli show neighbors details`` plain-text output → neighbor rows.

    Used when ``lldpcli -f json`` is unavailable (KCOS producer image) but text
    output is still produced.
    """
    raw = (raw or '').strip()
    if not raw or 'Interface:' not in raw:
        return []

    rows: list[dict] = []
    blocks = re.split(r'^Interface:\s+', raw, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        header, _, body = block.partition('\n')
        iface_part = header.split(',', 1)[0].strip()
        iface_name = iface_part.strip()
        if not iface_name:
            continue

        remote_device = ''
        chassis_id = ''
        mgmt_ip = ''
        remote_port = ''
        port_descr = ''

        section = ''
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith('Chassis:'):
                section = 'chassis'
                continue
            if stripped.startswith('Port:'):
                section = 'port'
                continue
            if not stripped or stripped.startswith('---'):
                continue
            if ':' not in stripped:
                continue
            key, _, val = stripped.partition(':')
            key = key.strip()
            val = val.strip()
            if section == 'chassis':
                if key == 'SysName':
                    remote_device = val
                elif key == 'ChassisID':
                    chassis_id = val.replace('mac ', '').replace('local ', '').strip()
                elif key == 'MgmtIP' and not mgmt_ip:
                    mgmt_ip = val
            elif section == 'port':
                if key == 'PortID':
                    remote_port = val.replace('ifname ', '').replace('mac ', '').strip()
                elif key == 'PortDescr':
                    port_descr = val

        if not remote_port:
            remote_port = port_descr
        if not (remote_device or chassis_id or remote_port):
            continue
        rows.append({
            'interface': iface_name,
            'remote_device': remote_device or remote_port or chassis_id or 'unknown',
            'remote_port': remote_port,
            'port_descr': port_descr,
            'chassis_id': chassis_id,
            'mgmt_ip': mgmt_ip,
        })
    return rows


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _first_value(obj) -> str:
    """lldpd JSON wraps scalars as {'value': x} or [{'value': x}]."""
    for item in _as_list(obj):
        if isinstance(item, dict) and item.get('value') is not None:
            return str(item['value'])
        if isinstance(item, (str, int, float)):
            return str(item)
    return ''


def parse_lldpcli_json(raw: str) -> list[dict]:
    """Parse ``lldpcli show neighbors -f json`` output → neighbor rows.

    Handles both lldpd JSON shapes:
    - dict-keyed: ``{"lldp": {"interface": {"eth0": {...}}}}``
    - list form:  ``{"lldp": [{"interface": [{"name": "eth0", ...}]}]}``

    Returns rows of ``{interface, remote_device, remote_port, chassis_id, mgmt_ip}``.
    """
    raw = (raw or '').strip()
    if not raw or not raw.startswith('{'):
        return []
    try:
        doc = json.loads(raw)
    except ValueError:
        return []

    lldp = doc.get('lldp')
    interfaces: list[tuple[str, dict]] = []
    for lldp_item in _as_list(lldp):
        if not isinstance(lldp_item, dict):
            continue
        iface_obj = lldp_item.get('interface')
        if isinstance(iface_obj, dict):
            interfaces.extend((name, data) for name, data in iface_obj.items() if isinstance(data, dict))
        else:
            for entry in _as_list(iface_obj):
                if not isinstance(entry, dict):
                    continue
                if entry.get('name'):
                    interfaces.append((str(entry['name']), entry))
                    continue
                # lldpd list form: [{"eaglefp0": {...}}, {"eaglefp0": {...}}]
                for name, data in entry.items():
                    if isinstance(data, dict):
                        interfaces.append((str(name), data))

    rows: list[dict] = []
    for iface_name, data in interfaces:
        chassis = data.get('chassis')
        remote_device = ''
        chassis_id = ''
        mgmt_ip = ''
        for ch_item in _as_list(chassis):
            if not isinstance(ch_item, dict):
                continue
            if 'id' in ch_item or 'name' in ch_item or 'mgmt-ip' in ch_item:
                # list form: fields inline
                remote_device = remote_device or _first_value(ch_item.get('name'))
                chassis_id = chassis_id or _first_value(ch_item.get('id'))
                mgmt_ip = mgmt_ip or _first_value(ch_item.get('mgmt-ip'))
            else:
                # dict-keyed form: sysname is the key
                for sysname, body in ch_item.items():
                    if not isinstance(body, dict):
                        continue
                    remote_device = remote_device or str(sysname)
                    chassis_id = chassis_id or _first_value(body.get('id'))
                    mgmt_ip = mgmt_ip or _first_value(body.get('mgmt-ip'))

        port = data.get('port')
        remote_port = ''
        port_descr = ''
        for p_item in _as_list(port):
            if isinstance(p_item, dict):
                remote_port = remote_port or _first_value(p_item.get('id'))
                port_descr = port_descr or _first_value(p_item.get('descr'))
        if not remote_port:
            remote_port = port_descr

        if not (remote_device or chassis_id or remote_port):
            continue
        rows.append({
            'interface': iface_name,
            'remote_device': remote_device or remote_port or chassis_id or 'unknown',
            'remote_port': remote_port,
            'port_descr': port_descr,
            'chassis_id': chassis_id,
            'mgmt_ip': mgmt_ip,
        })
    return rows


def _collect_m8400_merlin_lldp_via_k8s(
    client,
    *,
    k8s_node: str = 'mgmt',
    iface_pattern: str = 'eaglefp*',
    command_timeout: int = 45,
) -> str:
    """Run a short-lived hostNetwork lldpd pod on the KCOS mgmt node.

    KCOS ships lldpd only in k8s DaemonSets on compute nodes; the mgmt/merlin
    host has no ``lldpcli`` binary. Front-panel QDD LLDP is collected here.
    """
    pod = f'lv-lldp-{uuid.uuid4().hex[:10]}'
    probe_cmd = (
        'lldpd -d -ddd -k -l & '
        'sleep 3; '
        f'lldpcli configure system interface pattern "{iface_pattern}" 2>/dev/null || true; '
        'OUT=""; '
        'for r in $(seq 1 15); do '
        '  sleep 2; '
        '  OUT=$(lldpcli show neighbors details -f json 2>/dev/null || true); '
        '  echo "$OUT" | grep -qi chassis && break; '
        'done; '
        f'echo {_LLDP_MARKER}; '
        'printf "%s\\n" "$OUT"; '
        'exit 0'
    )
    manifest = {
        'apiVersion': 'v1',
        'kind': 'Pod',
        'metadata': {'name': pod, 'namespace': 'default'},
        'spec': {
            'nodeName': k8s_node or 'mgmt',
            'hostNetwork': True,
            'restartPolicy': 'Never',
            'containers': [{
                'name': 'lldpd',
                'image': _LLDPD_K8S_IMAGE,
                'command': ['sh', '-ce', probe_cmd],
                'securityContext': {'capabilities': {'add': ['NET_RAW', 'NET_ADMIN']}},
            }],
        },
    }
    manifest_b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    wait_secs = min(90, max(30, int(command_timeout)))
    remote_script = f'''set -e
POD={pod}
MAN=/tmp/${{POD}}.json
echo {manifest_b64} | base64 -d > "$MAN"
kubectl delete pod "$POD" -n default --ignore-not-found >/dev/null 2>&1 || true
kubectl apply -f "$MAN" >/dev/null
for i in $(seq 1 {wait_secs}); do
  phase=$(kubectl get pod "$POD" -n default -o jsonpath='{{.status.phase}}' 2>/dev/null || true)
  if [ "$phase" = "Succeeded" ] || [ "$phase" = "Failed" ]; then break; fi
  sleep 1
done
kubectl logs "$POD" -n default 2>/dev/null || true
kubectl delete pod "$POD" -n default --ignore-not-found >/dev/null 2>&1 || true
rm -f "$MAN"
'''
    _, stdout, _stderr = client.exec_command(remote_script.strip(), timeout=wait_secs + 30)
    return stdout.read().decode('utf-8', errors='replace')


_KCOS_LLDP_NAMESPACE = 'kcos-lldp-cablemap'
_FRONT_PANEL_IFACE_RE = re.compile(r'^eaglefp\d+(?:fo\d+)?$', re.I)


def _filter_front_panel_lldp_rows(rows: list[dict]) -> list[dict]:
    """Keep only front-panel ``eaglefp*`` neighbor rows (drop internal ``eaglecp0``)."""
    out: list[dict] = []
    for row in rows:
        iface = (row.get('interface') or '').strip()
        if _FRONT_PANEL_IFACE_RE.match(iface):
            out.append(row)
    return out


def _collect_lldp_from_kcos_producer_pods(
    client,
    *,
    namespace: str = _KCOS_LLDP_NAMESPACE,
    command_timeout: int = 45,
    front_panel_only: bool = True,
) -> dict[str, list[dict]]:
    """Read LLDP from KCOS ``lldp-producer`` DaemonSet pods (one per compute node).

    M8400 front-panel ``eaglefp*fo*`` interfaces are owned by lldpd in these pods,
    not on the mgmt/merlin host. Uses ``lldpctl -f json`` when available, else text.
    """
    list_cmd = (
        f"kubectl get pods -n {namespace} -o json 2>/dev/null"
    )
    _, stdout, _stderr = client.exec_command(list_cmd, timeout=command_timeout)
    raw = stdout.read().decode('utf-8', errors='replace')
    if not raw.strip():
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        logger.debug('KCOS LLDP producer: kubectl json parse failed')
        return {}

    result: dict[str, list[dict]] = {}
    items = doc.get('items') or []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get('metadata') or {}
        pod_name = (meta.get('name') or '').strip()
        if 'producer' not in pod_name.lower():
            continue
        status = item.get('status') or {}
        if (status.get('phase') or '').strip() != 'Running':
            continue
        spec = item.get('spec') or {}
        node_name = (spec.get('nodeName') or '').strip()
        if not node_name:
            continue

        exec_cmd = (
            f"kubectl exec -n {namespace} {pod_name} -- sh -c "
            f"'echo {_LLDP_MARKER}; "
            f"lldpctl -f json 2>/dev/null || "
            f"lldpcli show neighbors details 2>/dev/null || true'"
        )
        try:
            _, pod_out, _pod_err = client.exec_command(exec_cmd, timeout=command_timeout)
            out = pod_out.read().decode('utf-8', errors='replace')
        except Exception as exc:  # noqa: BLE001
            logger.debug('KCOS LLDP producer exec %s failed: %s', pod_name, exc)
            continue

        rows = _parse_lldp_command_output(out)
        if front_panel_only:
            rows = _filter_front_panel_lldp_rows(rows)
        if rows:
            result[node_name] = rows
            logger.debug(
                'KCOS LLDP producer %s on %s: %d front-panel neighbor(s)',
                pod_name, node_name, len(rows),
            )
    return result


def collect_kcos_lldp(
    mgmt_hosts: list[str],
    compute_nodes: list[dict],
    *,
    merlin_node: dict | None = None,
    node_interfaces: dict[str, list[str]] | None = None,
    probe_merlin: bool = True,
    probe_compute: bool = True,
    probe_producer_pods: bool = False,
    merlin_k8s_node: str = 'mgmt',
    merlin_use_k8s_lldpd: bool = False,
    username: str = 'root',
    password: str = '',
    key_path: str = '',
    port: int = 9022,
    connect_timeout: int = 15,
    command_timeout: int = 45,
) -> dict[str, list[dict]]:
    """SSH to the KCOS mgmt node, collect LLDP locally + on compute nodes.

    ``compute_nodes``: ``[{'name': ..., 'internal_ip': ...}, ...]``
    ``merlin_node``: optional ``{'name': ...}`` — run LLDP script locally on the
    mgmt node (M8400 front-panel QDD ports live on merlin, not compute nodes).

    ``probe_merlin`` / ``probe_compute``: M8400 uses producer pods + optional mgmt
    ephemeral lldpd; APS100/M1010 uses compute hops only.
    ``probe_producer_pods``: kubectl exec into ``kcos-lldp-cablemap`` producer
    DaemonSet pods (M8400 front-panel ``eaglefp*`` TX/RX lives on compute nodes).

    Returns ``{node_name: [{interface, remote_device, remote_port, port_descr,
    chassis_id, mgmt_ip}, ...]}``; ``{}`` when no credentials, no paramiko, or the
    management node is unreachable. Compute hops run in up to 8 threads.
    """
    nodes = [
        n for n in (compute_nodes or [])
        if isinstance(n, dict) and (
            (n.get('internal_ip') or '').strip() or (n.get('name') or '').strip()
        )
    ]
    merlin_name = (merlin_node or {}).get('name', '').strip() if merlin_node else ''
    if not mgmt_hosts:
        return {}
    if not probe_compute and not (probe_merlin and merlin_name) and not probe_producer_pods:
        return {}

    resolved_key = resolve_kcos_ssh_key(key_path) if key_path else ''
    if not (password or resolved_key):
        logger.debug('KCOS LLDP SSH skipped: no root password or key configured')
        return {}

    try:
        import paramiko
    except ImportError:
        logger.warning('paramiko not installed; KCOS LLDP collection disabled')
        return {}

    connect_kwargs: dict = {
        'username': username or 'root',
        'port': int(port or 9022),
        'timeout': connect_timeout,
        'look_for_keys': False,
        'allow_agent': False,
    }
    if password:
        connect_kwargs['password'] = password
    if resolved_key:
        connect_kwargs['key_filename'] = resolved_key

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connected_host = ''
    for host in mgmt_hosts:
        host = (host or '').strip()
        if not host:
            continue
        try:
            client.connect(host, **connect_kwargs)
            connected_host = host
            break
        except Exception as exc:
            logger.debug('KCOS mgmt SSH %s failed: %s', host, exc)

    if not connected_host:
        logger.info('KCOS LLDP SSH: could not connect to mgmt node (%d hosts tried)', len(mgmt_hosts))
        return {}

    result: dict[str, list[dict]] = {}
    iface_map = node_interfaces or {}

    if probe_producer_pods:
        try:
            producer_rows = _collect_lldp_from_kcos_producer_pods(
                client, command_timeout=command_timeout,
            )
            for node_name, rows in producer_rows.items():
                if rows:
                    result[node_name] = rows
        except Exception as exc:  # noqa: BLE001
            logger.debug('KCOS LLDP producer pod collection failed: %s', exc)

    # M8400 legacy/ephemeral path: short-lived lldpd on mgmt (usually no eaglefp*fo*).
    if probe_merlin and merlin_name:
        merlin_ifaces = (iface_map.get(merlin_name) if merlin_name in iface_map else None)
        if merlin_ifaces is not None and not merlin_ifaces:
            pass  # explicit empty list — skip merlin LLDP
        else:
            try:
                if merlin_use_k8s_lldpd:
                    out = _collect_m8400_merlin_lldp_via_k8s(
                        client,
                        k8s_node=merlin_k8s_node or 'mgmt',
                        iface_pattern='eaglefp*',
                        command_timeout=command_timeout,
                    )
                else:
                    _, stdout, _stderr = client.exec_command(
                        _lldp_remote_script(merlin_ifaces), timeout=command_timeout,
                    )
                    out = stdout.read().decode('utf-8', errors='replace')
                rows = _parse_lldp_command_output(out)
                if not rows and merlin_use_k8s_lldpd:
                    logger.debug('KCOS LLDP k8s pod on %s returned no neighbors', merlin_k8s_node)
                elif rows:
                    result[merlin_name] = rows
                    logger.debug('KCOS LLDP: %d neighbors on merlin (%s)', len(rows), merlin_k8s_node)
            except Exception as exc:  # noqa: BLE001
                logger.debug('KCOS LLDP merlin probe failed: %s', exc)

    def _probe(node: dict):
        name = node.get('name') or node.get('internal_ip')
        ip = (node.get('internal_ip') or '').strip()
        host = (node.get('name') or '').strip()
        targets = []
        for t in (ip, host):
            if t and t not in targets:
                targets.append(t)
        rows: list[dict] = []
        script = _lldp_remote_script(iface_map.get(str(name)))
        for target in targets:
            inner = f"ssh {_INNER_SSH_OPTS} root@{target} '{script}'"
            try:
                _, stdout, _stderr = client.exec_command(inner, timeout=command_timeout)
                out = stdout.read().decode('utf-8', errors='replace')
                rows = _parse_lldp_command_output(out)
                if rows:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug('KCOS LLDP hop to %s via %s failed: %s', name, target, exc)
        return name, rows

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not nodes:
            pass
        elif not probe_compute:
            pass
        else:
            max_workers = max(1, min(8, len(nodes)))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_probe, n): n for n in nodes}
                for fut in as_completed(futures):
                    name, rows = fut.result()
                    if rows:
                        result[str(name)] = rows
    finally:
        try:
            client.close()
        except Exception:
            pass

    if result:
        total = sum(len(v) for v in result.values())
        logger.info(
            'KCOS LLDP: %d neighbors on %d node(s) from %s',
            total, len(result), connected_host,
        )
    return result
