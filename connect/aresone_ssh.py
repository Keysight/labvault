"""Collect per-PCPU CPU/memory from AresONE chassis via root SSH hop.

Credentials come from Django settings (``ARESONE_ROOT_*`` env vars). Never log
passwords or key material.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Legacy busybox PCPUs on the internal 10.0.x.x network.
_INNER_SSH_OPTS = (
    '-o StrictHostKeyChecking=no '
    '-o ConnectTimeout=4 '
    '-o BatchMode=yes '
    '-o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1 '
    '-o HostKeyAlgorithms=+ssh-rsa,ssh-dss '
    '-o PubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss'
)

_PCPU_IP_RE = re.compile(r'^10\.0\.\d+\.\d+$')
_STAT_MARKER = '---LV_STAT---'
_FREE_MARKER = '---LV_FREE---'


def parse_free_m_mem_pct(output: str) -> float | None:
    """Parse ``free -m`` (busybox) and return used/total as a percentage."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].lower() == 'mem:':
            try:
                total = float(parts[1])
                used = float(parts[2])
            except (TypeError, ValueError):
                continue
            if total <= 0:
                return None
            return round(max(0.0, min(100.0, used / total * 100.0)), 2)
    return None


def parse_proc_stat_cpu_pct(output: str) -> float | None:
    """Parse two aggregate ``cpu`` lines from ``/proc/stat`` (1s apart)."""
    lines = [ln for ln in output.splitlines() if ln.startswith('cpu ')]
    if len(lines) < 2:
        return None

    def _totals(line: str) -> tuple[int, int] | None:
        parts = line.split()
        if len(parts) < 5:
            return None
        try:
            nums = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        return sum(nums), idle

    a = _totals(lines[0])
    b = _totals(lines[1])
    if not a or not b:
        return None
    total_a, idle_a = a
    total_b, idle_b = b
    dt = total_b - total_a
    if dt <= 0:
        return None
    idle_delta = idle_b - idle_a
    pct = (1.0 - idle_delta / dt) * 100.0
    return round(max(0.0, min(100.0, pct)), 2)


def _normalize_mgmt_ips(ips: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in ips:
        ip = (raw or '').strip()
        if not ip or not _PCPU_IP_RE.match(ip) or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return sorted(out)


def _pcpu_remote_script() -> str:
    return (
        f'echo {_FREE_MARKER}; free -m; '
        f'echo {_STAT_MARKER}; head -1 /proc/stat; sleep 1; head -1 /proc/stat'
    )


def _parse_pcpu_blob(output: str) -> dict[str, float | None]:
    free_part = ''
    stat_part = ''
    if _FREE_MARKER in output:
        after_free = output.split(_FREE_MARKER, 1)[1]
        if _STAT_MARKER in after_free:
            free_part, stat_part = after_free.split(_STAT_MARKER, 1)
        else:
            free_part = after_free
    elif _STAT_MARKER in output:
        stat_part = output.split(_STAT_MARKER, 1)[1]
    else:
        free_part = output
    return {
        'mem_pct': parse_free_m_mem_pct(free_part),
        'cpu_pct': parse_proc_stat_cpu_pct(stat_part or output),
    }


def collect_pcpu_health(
    chassis_hosts: list[str],
    management_ips: Iterable[str],
    *,
    username: str = 'root',
    password: str = '',
    key_path: str = '',
    connect_timeout: int = 15,
    command_timeout: int = 25,
) -> dict[str, dict[str, float | None]]:
    """SSH to chassis root, hop to each PCPU ``managementIp``, return health map."""
    ips = _normalize_mgmt_ips(management_ips)
    if not ips or not chassis_hosts:
        return {}

    if not (password or (key_path and Path(key_path).is_file())):
        logger.debug('AresONE PCPU SSH skipped: no root password or key configured')
        return {}

    try:
        import paramiko
    except ImportError:
        logger.warning('paramiko not installed; AresONE PCPU collection disabled')
        return {}

    base_kwargs: dict = {
        'username': username or 'root',
        'timeout': connect_timeout,
        'look_for_keys': False,
        'allow_agent': False,
    }
    key_file = key_path if key_path and Path(key_path).is_file() else ''

    def _connect_chassis(host: str) -> bool:
        """Password-first (fleet-shared root), then optional key — matches lab validation."""
        if password:
            try:
                client.connect(host, password=password, **base_kwargs)
                return True
            except Exception as exc:
                logger.debug('AresONE chassis SSH %s password auth failed: %s', host, exc)
        if key_file:
            try:
                client.connect(host, key_filename=key_file, **base_kwargs)
                return True
            except Exception as exc:
                logger.debug('AresONE chassis SSH %s key auth failed: %s', host, exc)
        return False

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connected_host = ''
    for host in chassis_hosts:
        host = (host or '').strip()
        if not host:
            continue
        try:
            if _connect_chassis(host):
                connected_host = host
                break
        except Exception as exc:
            logger.debug('AresONE chassis SSH %s failed: %s', host, exc)

    if not connected_host:
        logger.info('AresONE PCPU SSH: could not connect to chassis (%d hosts tried)', len(chassis_hosts))
        return {}

    result: dict[str, dict[str, float | None]] = {}
    try:
        for ip in ips:
            inner = (
                f'ssh {_INNER_SSH_OPTS} root@{ip} '
                f"'{_pcpu_remote_script()}'"
            )
            try:
                _, stdout, stderr = client.exec_command(inner, timeout=command_timeout)
                out = stdout.read().decode('utf-8', errors='replace')
                err = stderr.read().decode('utf-8', errors='replace')
                if not out.strip():
                    logger.debug('AresONE PCPU %s empty output (host=%s): %s', ip, connected_host, err[:200])
                    continue
                parsed = _parse_pcpu_blob(out)
                if parsed.get('cpu_pct') is None and parsed.get('mem_pct') is None:
                    logger.debug('AresONE PCPU %s unparsable output (host=%s)', ip, connected_host)
                    continue
                result[ip] = {k: v for k, v in parsed.items() if v is not None}
            except Exception as exc:
                logger.debug('AresONE PCPU hop to %s failed: %s', ip, exc)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if result:
        logger.info(
            'AresONE PCPU health: %d/%d IPs from %s',
            len(result), len(ips), connected_host,
        )
    return result


def _shell_single_quote(value: str) -> str:
    return (value or '').replace("'", "'\"'\"'")


def _pcpu_ixos_apps_script(ixos_user: str, ixos_pass: str) -> str:
    """Remote busybox script: auth to local IxOS REST and print /chassis JSON."""
    user = _shell_single_quote(ixos_user or 'admin')
    pwd = _shell_single_quote(ixos_pass or 'admin')
    return (
        'echo ---LV_IXOS---; '
        "if command -v curl >/dev/null 2>&1; then "
        f"AUTH=$(curl -sk -X POST https://127.0.0.1/platform/api/v1/auth/session "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"username\":\"{user}\",\"password\":\"{pwd}\",\"rememberMe\":false}}'); "
        "KEY=$(echo \"$AUTH\" | sed -n 's/.*\"apiKey\":\"\\([^\"]*\\)\".*/\\1/p'); "
        'if [ -n "$KEY" ]; then '
        'curl -sk -H "x-api-key: $KEY" https://127.0.0.1/chassis/api/v2/ixos/chassis; '
        'fi; '
        'fi'
    )


def collect_pcpu_app_versions(
    chassis_hosts: list[str],
    management_ips: Iterable[str],
    *,
    username: str = 'root',
    password: str = '',
    key_path: str = '',
    ixos_username: str = 'admin',
    ixos_password: str = 'admin',
    connect_timeout: int = 15,
    command_timeout: int = 30,
) -> dict[str, dict]:
    """SSH hop to each PCPU and read local IxOS ``/chassis`` application versions."""
    from connect.pcpu_versions import parse_pcpu_ixos_blob

    ips = _normalize_mgmt_ips(management_ips)
    if not ips or not chassis_hosts:
        return {}

    if not (password or (key_path and Path(key_path).is_file())):
        logger.debug('AresONE PCPU app versions skipped: no root password or key configured')
        return {}

    try:
        import paramiko
    except ImportError:
        logger.warning('paramiko not installed; AresONE PCPU app version collection disabled')
        return {}

    base_kwargs: dict = {
        'username': username or 'root',
        'timeout': connect_timeout,
        'look_for_keys': False,
        'allow_agent': False,
    }
    key_file = key_path if key_path and Path(key_path).is_file() else ''
    remote = _pcpu_ixos_apps_script(ixos_username, ixos_password)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connected_host = ''

    def _connect_chassis(host: str) -> bool:
        if password:
            try:
                client.connect(host, password=password, **base_kwargs)
                return True
            except Exception as exc:
                logger.debug('AresONE chassis SSH %s password auth failed: %s', host, exc)
        if key_file:
            try:
                client.connect(host, key_filename=key_file, **base_kwargs)
                return True
            except Exception as exc:
                logger.debug('AresONE chassis SSH %s key auth failed: %s', host, exc)
        return False

    for host in chassis_hosts:
        host = (host or '').strip()
        if not host:
            continue
        try:
            if _connect_chassis(host):
                connected_host = host
                break
        except Exception as exc:
            logger.debug('AresONE chassis SSH %s failed: %s', host, exc)

    if not connected_host:
        return {}

    result: dict[str, dict] = {}
    try:
        for ip in ips:
            inner = f'ssh {_INNER_SSH_OPTS} root@{ip} \'{remote}\''
            try:
                _, stdout, _stderr = client.exec_command(inner, timeout=command_timeout)
                out = stdout.read().decode('utf-8', errors='replace')
                parsed = parse_pcpu_ixos_blob(out)
                if parsed.get('applications') or parsed.get('ixos_version') or parsed.get('ixnetwork_version'):
                    result[ip] = parsed
            except Exception as exc:
                logger.debug('AresONE PCPU app versions hop to %s failed: %s', ip, exc)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if result:
        logger.info(
            'AresONE PCPU app versions: %d/%d IPs from %s',
            len(result), len(ips), connected_host,
        )
    return result
