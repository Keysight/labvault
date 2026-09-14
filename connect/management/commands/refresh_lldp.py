"""
refresh_lldp — fetch live LLDP from Arista/SONiC switches.

Probes each dual-OS switch to detect the active OS, connects via SSH with
the appropriate credentials, runs 'show lldp table' (SONiC) or
'show lldp neighbors' (EOS), and stores parsed neighbors in the LabVault
topology cache so the Port Fabric Map shows live LLDP.

Usage:
    python manage.py refresh_lldp                  # switches from inventory
    python manage.py refresh_lldp --ip 192.0.2.20  # single device
    python manage.py refresh_lldp --dry-run        # probe only, no file write
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

LLDP_DIR = Path(os.environ.get('LABVAULT_LLDP_RAW_DIR') or '/var/lib/labvault/lldp_raw')
CACHE_FILE = Path('/tmp/labvault_lldp_cache.json')

# Empty default: discover switch IPs from inventory unless --ip is given.
DEFAULT_IPS: list[str] = []


def _ssh_connect(ip, username, password, port=22, timeout=12):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, port=port, username=username, password=password,
               timeout=timeout, look_for_keys=False, allow_agent=False,
               banner_timeout=20)
    return c


def _ssh_run(client, cmd, timeout=20):
    _, out, _ = client.exec_command(cmd, timeout=timeout)
    return (out.read() or b'').decode('utf-8', errors='replace')


def _detect_os_and_connect(ip, eos_user, eos_pass, sonic_user, sonic_pass, port=22, timeout=12):
    """Return (client, active_os) or raise on total failure."""
    for (u, p, label) in [(eos_user, eos_pass, 'eos'), (sonic_user, sonic_pass, 'sonic')]:
        try:
            c = _ssh_connect(ip, u, p, port=port, timeout=timeout)
            raw = _ssh_run(c, 'show version', timeout=12)
            rl = raw.lower()
            if 'sonic' in rl or 'buildcommit' in rl or 'debian' in rl or 'linux' in rl:
                return c, 'sonic'
            if 'arista' in rl or 'eos' in rl or 'dcs-' in rl or 'fastcli' in rl:
                return c, 'eos'
            # Connected but version output unclear — assume label for these creds
            return c, label
        except Exception as e:
            logger.debug('%s: SSH with %s/%s failed: %s', ip, u, p[:3]+'*', e)
            continue
    raise OSError(f'{ip}: could not connect with any credentials')


def _fetch_lldp_ssh(client, active_os) -> list:
    """Fetch LLDP neighbors and return list of dicts."""
    neighbors = []
    if active_os == 'sonic':
        raw = _ssh_run(client, 'show lldp table')
        neighbors = _parse_lldp_table_text(raw)
        if not neighbors:
            raw = _ssh_run(client, 'show lldp neighbors')
            neighbors = _parse_lldp_verbose(raw)
    else:
        # EOS — try JSON first
        raw = _ssh_run(client, "FastCli -c 'show lldp neighbors | json'")
        if raw and '{' in raw:
            try:
                i, j = raw.index('{'), raw.rindex('}')
                data = json.loads(raw[i:j+1])
                ln = data.get('lldpNeighbors', [])
                if isinstance(ln, list):
                    for n in ln:
                        neighbors.append({
                            'local_port': n.get('port', ''),
                            'remote_device': n.get('neighborDevice', ''),
                            'remote_port': n.get('neighborPort', ''),
                        })
                elif isinstance(ln, dict):
                    for loc_port, v in ln.items():
                        nl = v if isinstance(v, list) else v.get('lldpNeighborInfo', [])
                        for n in nl:
                            neighbors.append({
                                'local_port': loc_port,
                                'remote_device': n.get('systemName', ''),
                                'remote_port': (n.get('neighborInterfaceInfo', {}).get('interfaceId','')
                                                or n.get('portId','')).strip('"\''),
                            })
            except Exception as e:
                logger.debug('EOS JSON LLDP parse failed: %s', e)
        if not neighbors:
            raw = _ssh_run(client, "FastCli -c 'show lldp neighbors'")
            neighbors = _parse_eos_lldp_text(raw)
    return neighbors


def _parse_lldp_table_text(text):
    """Parse SONiC 'show lldp table' output."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('-','C','L','N','W','Last')):
            continue
        if 'LocalPort' in stripped or 'Local' in stripped[:12]:
            continue
        parts = re.split(r'\s{2,}', stripped)
        if len(parts) >= 3 and parts[0].startswith('Ethernet'):
            out.append({
                'local_port': parts[0],
                'remote_device': parts[1].split('.')[0],  # short hostname
                'remote_port': parts[2],
            })
    return out


def _parse_eos_lldp_text(text):
    """Parse EOS 'show lldp neighbors' text output."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= {'-', ' '}:
            continue
        parts = re.split(r'\s{2,}|\t', stripped)
        if len(parts) >= 3 and (parts[0].startswith('Et') or parts[0].startswith('Ethernet')):
            out.append({
                'local_port': parts[0],
                'remote_device': parts[1],
                'remote_port': parts[2],
            })
    return out


def _parse_lldp_verbose(text):
    """Parse SONiC/lldpctl verbose output."""
    out = []
    blocks = re.split(r'\n(?=Interface:\s+)', text)
    for block in blocks:
        local = remote = rport = ''
        for line in block.splitlines():
            if 'Interface:' in line:
                m = re.search(r'Interface:\s+(\S+)', line)
                if m: local = m.group(1).rstrip(',')
            elif 'SysName:' in line:
                remote = line.split('SysName:', 1)[-1].strip()
            elif 'PortID:' in line:
                val = line.split('PortID:', 1)[-1].strip()
                if val.lower().startswith('ifname '):
                    val = val[7:]
                rport = val
        if local and (remote or rport):
            out.append({'local_port': local, 'remote_device': remote, 'remote_port': rport})
    return out


def _save_raw(ip, hostname, raw_text, active_os):
    """Save raw LLDP text to file."""
    LLDP_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{hostname.replace(' ', '_')}.txt"
    fpath = LLDP_DIR / fname
    fpath.write_text(raw_text, encoding='utf-8')
    return fpath


def _get_raw_lldp_text(client, active_os):
    """Get full raw LLDP table text for saving to file."""
    if active_os == 'sonic':
        return _ssh_run(client, 'show lldp table')
    return _ssh_run(client, "FastCli -c 'show lldp neighbors'")


class Command(BaseCommand):
    help = 'Fetch live LLDP from Arista/SONiC switches and save to files + topology cache'

    def add_arguments(self, parser):
        parser.add_argument('--ip', nargs='+', default=None,
                            help='Specific IP(s) to refresh (default: all 4 switches)')
        parser.add_argument('--topo-id', type=int, default=None,
                            help='Refresh only switches in this topology (overrides default IP list)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Probe only, print results, do not write files')
        parser.add_argument('--timeout', type=int, default=15,
                            help='SSH timeout per device (seconds)')

    def handle(self, *args, **options):
        from connect.models import Device
        from connect.topology_graph import switch_ips_for_topology

        topo_id = options.get('topo_id')
        if topo_id and not options['ip']:
            target_ips = switch_ips_for_topology(topo_id)
            if not target_ips:
                self.stdout.write(f'No switch nodes in topology {topo_id}')
                return
        else:
            target_ips = options['ip'] or DEFAULT_IPS
            if not target_ips:
                target_ips = list(
                    Device.objects.filter(vendor_type__in=('arista', 'sonic'))
                    .exclude(ip_address='')
                    .values_list('ip_address', flat=True)
                )
        dry_run = options['dry_run']
        timeout_sec = options['timeout']
        scoped_refresh = bool(topo_id or options['ip'])

        results = {}
        all_lldp = {}  # ip → [neighbors]

        for ip in target_ips:
            dev = Device.objects.filter(ip_address=ip).first()
            if not dev:
                self.stderr.write(f'  SKIP {ip}: not in DB')
                continue

            eos_u = getattr(dev, 'arista_username', '') or dev.username or 'admin'
            eos_p = getattr(dev, 'arista_password', '') or 'admin'
            sonic_u = getattr(dev, 'sonic_username', '') or 'admin'
            sonic_p = getattr(dev, 'sonic_password', '') or 'password'
            hostname = (dev.hostname or ip).split('.')[0]

            self.stdout.write(f'\n[{ip}] {hostname}  eos={eos_u}/{eos_p[:3]}*  sonic={sonic_u}/{sonic_p[:3]}*')

            try:
                client, active_os = _detect_os_and_connect(
                    ip, eos_u, eos_p, sonic_u, sonic_p, timeout=timeout_sec)
                self.stdout.write(f'  Connected → active_os={active_os}')

                # Fetch and parse LLDP
                t0 = time.time()
                neighbors = _fetch_lldp_ssh(client, active_os)
                raw_text = _get_raw_lldp_text(client, active_os)
                elapsed = time.time() - t0
                client.close()

                self.stdout.write(f'  LLDP: {len(neighbors)} neighbors  ({elapsed:.1f}s)')
                for n in neighbors[:6]:
                    self.stdout.write(f'    {n["local_port"]:14} → {n["remote_device"][:24]:24} {n["remote_port"]}')
                if len(neighbors) > 6:
                    self.stdout.write(f'    ... ({len(neighbors)-6} more)')

                # Update Device active OS
                if not dry_run:
                    try:
                        # Never change vendor_type — dual-OS boxes must stay 'arista'
                        # Store active_os in api_key JSON
                        api = {}
                        try:
                            api = json.loads(dev.api_key or '{}')
                        except Exception:
                            pass
                        api['active_os_detected'] = active_os
                        api['active_os_ts'] = int(time.time())
                        dev.api_key = json.dumps(api)
                        dev.save(update_fields=['api_key'])
                    except Exception as e:
                        logger.warning('refresh_lldp: DB update %s: %s', ip, e)

                    # Save raw file
                    fpath = _save_raw(ip, hostname, raw_text, active_os)
                    self.stdout.write(f'  Saved → {fpath}')

                all_lldp[ip] = {
                    'active_os': active_os,
                    'neighbors': neighbors,
                    'hostname': hostname,
                    'count': len(neighbors),
                }
                results[ip] = 'ok'

            except Exception as e:
                self.stderr.write(f'  ERROR: {e}')
                results[ip] = f'error: {e}'

        # Save aggregated LLDP to cache file.
        # For any switch that FAILED this run, keep the data from the existing raw file
        # so the port fabric always has the most recent known LLDP state.
        if not dry_run:
            from connect.lab_topology_views import _ARISTA_IP_TO_LLDP, _parse_lldp_txt

            cache_data = {
                'fetched_at': time.time(),
                'devices': {},
            }
            if scoped_refresh and CACHE_FILE.is_file():
                try:
                    prev = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
                    cache_data['devices'] = dict(prev.get('devices') or {})
                except Exception:
                    pass

            ips_for_cache = set(target_ips) if scoped_refresh else set(_ARISTA_IP_TO_LLDP.keys())
            for ip in ips_for_cache:
                fname = _ARISTA_IP_TO_LLDP.get(ip, '')
                if ip in all_lldp:
                    cache_data['devices'][ip] = all_lldp[ip]
                elif fname:
                    # SSH failed this run — parse from saved raw file as fallback
                    fpath = LLDP_DIR / fname
                    if fpath.is_file():
                        try:
                            nbrs = _parse_lldp_txt(fpath)
                            nbr_list = [{'local_port': lp, 'remote_device': v.get('remote_device',''), 'remote_port': v.get('remote_port','')}
                                        for lp, v in nbrs.items()]
                            cache_data['devices'][ip] = {
                                'active_os': 'unknown',
                                'neighbors': nbr_list,
                                'count': len(nbr_list),
                                'hostname': fname.replace('.txt',''),
                                'from_file_fallback': True,
                            }
                            self.stdout.write(f'  Fallback: loaded {len(nbr_list)} neighbors from {fpath.name}')
                        except Exception as e:
                            pass
            try:
                CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding='utf-8')
                self.stdout.write(f'\nCache saved: {CACHE_FILE} ({len(cache_data["devices"])} switches)')
            except Exception as e:
                self.stderr.write(f'Cache write failed: {e}')

            try:
                from connect.lldp_persistence import persist_switch_lldp_cache
                persist_switch_lldp_cache(cache_data.get('devices') or {})
                self.stdout.write('Persistent LLDP cache updated (24h retention)')
            except Exception as e:
                self.stderr.write(f'Persistent LLDP cache failed: {e}')

            try:
                from connect.topology_graph import (
                    invalidate_cache,
                    topology_ids_for_switch_ips,
                )
                if topo_id:
                    invalidate_cache(topo_id)
                    self.stdout.write(f'Topology graph cache invalidated for topo {topo_id}')
                elif options['ip']:
                    tids = topology_ids_for_switch_ips(target_ips)
                    for tid in tids:
                        invalidate_cache(tid)
                    self.stdout.write(
                        f'Topology graph cache invalidated for topologies: {tids or "none"}'
                    )
                else:
                    invalidate_cache(None)
                    self.stdout.write('Topology graph cache invalidated (all topologies)')
            except Exception as e:
                self.stderr.write(f'Topology graph cache invalidate failed: {e}')

        # Summary
        self.stdout.write('\n=== Summary ===')
        for ip, status in results.items():
            neighbor_count = all_lldp.get(ip, {}).get('count', 0)
            active_os = all_lldp.get(ip, {}).get('active_os', '?')
            self.stdout.write(f'  {ip}  os={active_os:6}  lldp={neighbor_count:3}  {status}')

        ok_count = sum(1 for s in results.values() if s == 'ok')
        self.stdout.write(f'\n{ok_count}/{len(target_ips)} switches refreshed successfully')

        if dry_run:
            self.stdout.write('\n[DRY RUN] No files written.')
