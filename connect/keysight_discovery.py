"""
Subnet discovery for Keysight/Ixia chassis.
Scans a given CIDR range and identifies IxOS and KCOS devices.

Each host is probed over HTTPS (TLS verification disabled): first the IxOS
``/platform/api/v1/auth/session`` login, then the KCOS Keycloak token endpoint.
A 401/403 still counts as a Keysight device (``auth_ok=False``) so the UI can
show it, but :func:`auto_add_discovered` only creates chassis whose login
succeeded.

Scan progress lives in the process-local ``_scan_state`` dict, so
``get_scan_status`` only sees scans started by the same gunicorn worker.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory scan state  (scan_id -> status dict)
# ---------------------------------------------------------------------------
_scan_state: dict[str, dict] = {}
_scan_lock = threading.Lock()
_scan_counter = 0


def _next_scan_id() -> str:
    global _scan_counter
    with _scan_lock:
        _scan_counter += 1
        return f'scan-{_scan_counter}'


def get_scan_status(scan_id: str) -> dict | None:
    """Return a copy of the in-memory scan state for ``scan_id`` (or None).

    Keys: ``state`` (running/completed/error), ``subnet``, ``progress``,
    ``total``, ``discovered`` (list of probe dicts), ``started_at``, ``error``.
    """
    with _scan_lock:
        return _scan_state.get(scan_id, {}).copy() if scan_id in _scan_state else None


# ---------------------------------------------------------------------------
# Single-host probe
# ---------------------------------------------------------------------------

def _probe_host(ip: str, username: str, password: str, timeout: int = 5) -> dict | None:
    """Probe a single IP to determine if it's an IxOS or KCOS device.
    Returns a dict with discovery info or None if not a Keysight device.

    Result keys: ``ip``, ``platform`` (``ixos``/``kcos``),
    ``chassis_type_guess`` (a ``KEYSIGHT_CHASSIS_TYPE_CHOICES`` value),
    ``reachable``, ``auth_ok``. KCOS model detection tries Helm release chart
    names, then ``/api/v2/vital/hostname``, then system/chassis/hardware info.
    """

    # Try IxOS first (faster to reject)
    try:
        resp = requests.post(
            f'https://{ip}/platform/api/v1/auth/session',
            json={
                'username': username,
                'password': password,
                'rememberMe': False,
                'resetWeakPassword': False,
            },
            headers={'Content-Type': 'application/json'},
            verify=False,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('apiKey'):
                # Try to detect chassis type from the /chassis endpoint
                chassis_type_guess = 'other'
                try:
                    api_key = data['apiKey']
                    ch_resp = requests.get(
                        f'https://{ip}/platform/api/v1/chassis',
                        headers={'X-Api-Key': api_key},
                        verify=False,
                        timeout=timeout,
                    )
                    if ch_resp.status_code == 200:
                        ch_data = ch_resp.json()
                        if isinstance(ch_data, list) and ch_data:
                            ch_data = ch_data[0]
                        raw_type = (ch_data.get('type', '') if isinstance(ch_data, dict) else '').lower().replace('-', '').replace('_', '').replace(' ', '')
                        if 'aresone' in raw_type:
                            chassis_type_guess = 'aresone'
                        elif 'xgs2' in raw_type:
                            chassis_type_guess = 'xgs2'
                        elif 'xgs12' in raw_type:
                            chassis_type_guess = 'xgs12'
                        elif 'xm' in raw_type:
                            chassis_type_guess = 'xm'
                        elif 'xg' in raw_type:
                            chassis_type_guess = 'xg'
                except Exception:
                    pass
                return {
                    'ip': ip,
                    'platform': 'ixos',
                    'chassis_type_guess': chassis_type_guess,
                    'reachable': True,
                    'auth_ok': True,
                }
        elif resp.status_code in (401, 403):
            # Server responded but auth failed - still an IxOS device
            return {
                'ip': ip,
                'platform': 'ixos',
                'chassis_type_guess': 'other',
                'reachable': True,
                'auth_ok': False,
            }
    except requests.exceptions.ConnectionError:
        pass
    except requests.exceptions.Timeout:
        pass
    except Exception:
        pass

    # Try KCOS (Keycloak)
    try:
        resp = requests.post(
            f'https://{ip}/auth/realms/keysight/protocol/openid-connect/token',
            data={
                'grant_type': 'password',
                'client_id': 'kcos-rest-api',
                'username': username,
                'password': password,
            },
            verify=False,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('access_token'):
                # Detect M1010 vs M8400 from Helm chart name or system info
                # aps-kcos-400 / kcos-eagle-merlin => M8400
                # aps-kcos     / kcos-eagle        => M1010
                chassis_type_guess = 'aps_m1010'  # default
                try:
                    token = data['access_token']
                    auth_headers = {'Authorization': f'Bearer {token}'}

                    # Primary: check Helm releases for chart name hints
                    helm_resp = requests.get(
                        f'https://{ip}/api/v2/deployment/helm/cluster/releases',
                        headers=auth_headers,
                        verify=False,
                        timeout=timeout,
                    )
                    logger.debug(
                        'KCOS %s helm releases status=%s', ip, helm_resp.status_code
                    )
                    if helm_resp.status_code == 200:
                        releases = helm_resp.json()
                        logger.debug('KCOS %s helm releases payload: %s', ip, releases)
                        if isinstance(releases, list):
                            for rel in releases:
                                cd = rel.get('chartDeployment', {})
                                cn = (cd.get('chartName', '') if cd else '').lower()
                                # Also check top-level 'name' and 'chart' fields
                                cn_alt = rel.get('name', '') + ' ' + rel.get('chart', '')
                                cn_alt = cn_alt.lower()
                                if ('htrex' in cn or 'h-trex' in cn or 'htrex' in cn_alt or 'h-trex' in cn_alt):
                                    chassis_type_guess = 'aresone_htrex'
                                    break
                                if ('trex' in cn or 't-rex' in cn or 'trex' in cn_alt or 't-rex' in cn_alt):
                                    chassis_type_guess = 'trex'
                                    break
                                if ('kcos-400' in cn or 'eagle-merlin' in cn or
                                        'm8400' in cn or '8400' in cn or
                                        'kcos-400' in cn_alt or 'eagle-merlin' in cn_alt or
                                        'm8400' in cn_alt or '8400' in cn_alt):
                                    chassis_type_guess = 'aps_m8400'
                                    break
                    else:
                        logger.warning(
                            'KCOS %s helm releases returned HTTP %s: %s',
                            ip, helm_resp.status_code, helm_resp.text[:200],
                        )

                    # Fallback: check hostname for TREX/HTREX
                    if chassis_type_guess == 'aps_m1010':
                        try:
                            hn_resp = requests.get(
                                f'https://{ip}/api/v2/vital/hostname',
                                headers=auth_headers,
                                verify=False,
                                timeout=timeout,
                            )
                            if hn_resp.status_code == 200:
                                hn_data = hn_resp.json()
                                hostname = (hn_data.get('name', '') or '').lower()
                                if 'htrex' in hostname or 'h-trex' in hostname:
                                    chassis_type_guess = 'aresone_htrex'
                                elif 'trex' in hostname or 't-rex' in hostname:
                                    chassis_type_guess = 'trex'
                        except Exception:
                            pass

                    # Fallback: check system/chassis info endpoint for model string
                    if chassis_type_guess == 'aps_m1010':
                        for info_url in [
                            f'https://{ip}/api/v2/system/info',
                            f'https://{ip}/api/v2/chassis',
                            f'https://{ip}/api/v2/hardware',
                        ]:
                            try:
                                info_resp = requests.get(
                                    info_url,
                                    headers=auth_headers,
                                    verify=False,
                                    timeout=timeout,
                                )
                                if info_resp.status_code == 200:
                                    info_text = info_resp.text.lower()
                                    logger.debug(
                                        'KCOS %s %s => %s', ip, info_url, info_text[:300]
                                    )
                                    if '8400' in info_text or 'm8400' in info_text:
                                        chassis_type_guess = 'aps_m8400'
                                        break
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning('KCOS %s model detection failed: %s', ip, exc)
                return {
                    'ip': ip,
                    'platform': 'kcos',
                    'chassis_type_guess': chassis_type_guess,
                    'reachable': True,
                    'auth_ok': True,
                }
        elif resp.status_code in (401, 403):
            return {
                'ip': ip,
                'platform': 'kcos',
                'chassis_type_guess': 'aps_m1010',
                'reachable': True,
                'auth_ok': False,
            }
    except requests.exceptions.ConnectionError:
        pass
    except requests.exceptions.Timeout:
        pass
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Subnet scanner
# ---------------------------------------------------------------------------

def scan_subnet(subnet_cidr: str, username: str = 'admin',
                password: str = 'admin', max_workers: int = 50) -> list[dict]:
    """Scan all hosts in a subnet and return discovered Keysight devices."""
    try:
        network = ipaddress.IPv4Network(subnet_cidr, strict=False)
    except (ipaddress.AddressValueError, ValueError) as e:
        logger.error('Invalid subnet %s: %s', subnet_cidr, e)
        return []

    hosts = list(network.hosts())
    discovered = []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(hosts) or 1)) as pool:
        futures = {
            pool.submit(_probe_host, str(host), username, password): str(host)
            for host in hosts
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                discovered.append(result)

    discovered.sort(key=lambda d: ipaddress.IPv4Address(d['ip']))
    return discovered


def scan_subnet_async(subnet_cidr: str, username: str = 'admin',
                      password: str = 'admin') -> str:
    """Launch a subnet scan in a background thread. Returns a scan_id for polling."""
    scan_id = _next_scan_id()
    with _scan_lock:
        _scan_state[scan_id] = {
            'state': 'running',
            'subnet': subnet_cidr,
            'progress': 0,
            'total': 0,
            'discovered': [],
            'started_at': time.time(),
        }

    def _run():
        try:
            network = ipaddress.IPv4Network(subnet_cidr, strict=False)
            hosts = list(network.hosts())
            total = len(hosts)
            with _scan_lock:
                _scan_state[scan_id]['total'] = total

            discovered = []
            completed = 0

            with ThreadPoolExecutor(max_workers=min(50, total or 1)) as pool:
                futures = {
                    pool.submit(_probe_host, str(h), username, password): str(h)
                    for h in hosts
                }
                for future in as_completed(futures):
                    completed += 1
                    result = future.result()
                    if result:
                        discovered.append(result)
                    with _scan_lock:
                        _scan_state[scan_id]['progress'] = completed
                        _scan_state[scan_id]['discovered'] = list(discovered)

            discovered.sort(key=lambda d: ipaddress.IPv4Address(d['ip']))
            with _scan_lock:
                _scan_state[scan_id]['state'] = 'completed'
                _scan_state[scan_id]['discovered'] = discovered
                _scan_state[scan_id]['progress'] = total
        except Exception as e:
            logger.error('Subnet scan error: %s', e)
            with _scan_lock:
                _scan_state[scan_id]['state'] = 'error'
                _scan_state[scan_id]['error'] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return scan_id


# ---------------------------------------------------------------------------
# Auto-create chassis from discovery results
# ---------------------------------------------------------------------------

def auto_add_discovered(discovered: list[dict], username: str = 'admin',
                        password: str = 'admin') -> list:
    """Create Chassis objects for newly discovered devices (skip existing)."""
    from .models import KeysightChassis  # deferred import to avoid circular

    added = []
    existing_ips = set(KeysightChassis.objects.values_list('ip_address', flat=True))
    for d in discovered:
        if d['ip'] in existing_ips:
            continue
        if not d.get('auth_ok', False):
            continue
        ch = KeysightChassis.objects.create(
            ip_address=d['ip'],
            username=username,
            password=password,
            chassis_type=d.get('chassis_type_guess', 'other'),
        )
        added.append(ch)
    return added
