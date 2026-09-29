"""Generic host reachability probes (ICMP + TCP).

Used by live fleet heartbeat and optional livelihood checks. DNS suffix
expansion is driven by ``LABVAULT_DNS_SUFFIXES`` (comma-separated), not a
hardcoded site name.

Protocol-agnostic: no credentials, no driver. ICMP uses the system ``ping``
binary; TCP checks are plain connects to ``DEFAULT_TCP_PORTS`` (SSH, HTTPS, HTTP,
8006, WinRM 5985/5986, 623). Caller: ``connect.fleet_heartbeat`` (host-alive
signal before trying the chassis driver).
"""

from __future__ import annotations

import os
import socket
import subprocess
from typing import Iterable, Sequence

DEFAULT_TCP_PORTS: tuple[int, ...] = (22, 443, 80, 8006, 5985, 5986, 623)


def dns_suffixes() -> list[str]:
    """Suffixes from ``LABVAULT_DNS_SUFFIXES`` without leading dots."""
    raw = (os.environ.get('LABVAULT_DNS_SUFFIXES') or '').strip()
    if not raw:
        return []
    return [s.strip().lstrip('.') for s in raw.split(',') if s.strip()]


def icmp_ping(host: str, *, timeout_s: float = 2.0) -> bool:
    """Best-effort ICMP. Returns False when ping is missing or filtered."""
    host = (host or '').strip()
    if not host or ' ' in host:
        return False
    try:
        proc = subprocess.run(
            ['ping', '-c', '1', '-W', str(max(1, int(timeout_s))), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s + 1.5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def tcp_connect(host: str, port: int, *, timeout_s: float = 2.0) -> bool:
    """True if a TCP connection to ``host:port`` opens within *timeout_s*."""
    host = (host or '').strip()
    if not host or ' ' in host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def expand_probe_hosts(*candidates: str) -> list[str]:
    """Deduped probe targets: given hosts, then short-name + configured DNS suffixes."""
    out: list[str] = []
    suffixes = dns_suffixes()
    for raw in candidates:
        host = (raw or '').strip()
        if not host or host in out:
            continue
        out.append(host)
        # IPv4 literal — no suffix expansion
        if host.replace('.', '').isdigit() or ':' in host:
            continue
        if '.' not in host:
            for suffix in suffixes:
                fqdn = f'{host}.{suffix}'
                if fqdn not in out:
                    out.append(fqdn)
    return out


def probe_host(
    host: str,
    ports: Sequence[int] | None = None,
    *,
    timeout_s: float = 2.0,
    try_icmp: bool = True,
) -> dict:
    """Reachability summary. ``alive`` when ICMP or any TCP port succeeds."""
    host = (host or '').strip()
    port_list = list(ports) if ports is not None else list(DEFAULT_TCP_PORTS)
    icmp = icmp_ping(host, timeout_s=timeout_s) if try_icmp else False
    open_ports: list[int] = []
    for port in port_list:
        if tcp_connect(host, port, timeout_s=timeout_s):
            open_ports.append(int(port))
    return {
        'host': host,
        'icmp': icmp,
        'open_ports': open_ports,
        'alive': bool(icmp or open_ports),
        'probe': 'tcp+ping' if try_icmp else 'tcp',
    }


def probe_hosts(
    hosts: Iterable[str],
    ports: Sequence[int] | None = None,
    *,
    timeout_s: float = 2.0,
    try_icmp: bool = True,
) -> dict:
    """Probe candidates in order; return first alive result (or last failure)."""
    last: dict = {
        'host': '',
        'icmp': False,
        'open_ports': [],
        'alive': False,
        'probe': 'tcp+ping' if try_icmp else 'tcp',
    }
    for host in hosts:
        last = probe_host(host, ports, timeout_s=timeout_s, try_icmp=try_icmp)
        if last['alive']:
            return last
    return last
