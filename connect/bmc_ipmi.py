"""
Direct IPMI/ipmitool interface for querying BMC endpoints.

Runs ipmitool as a subprocess to fetch info from BMCs even when
the host OS / KCOS API is down.  Only requires network reachability
to the BMC management interface.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

IPMITOOL_BIN = shutil.which('ipmitool') or 'ipmitool'
DEFAULT_TIMEOUT = 8  # seconds per ipmitool call


@dataclass
class BmcInfo:
    hostname: str = ''
    ip: str = ''
    reachable: bool = False
    error: str = ''

    # primary_os_name fields (kcos-eagle-node-pxeboot >= v0.97.0)
    primary_os_name_raw: str = ''
    primary_os_version: str = ''
    master_host: str = ''       # mh=
    mgmt_primary_system: str = ''  # mps=

    # FRU
    fru_board_mfg: str = ''
    fru_board_product: str = ''
    fru_board_serial: str = ''
    fru_board_part: str = ''
    fru_product_name: str = ''
    fru_product_serial: str = ''

    # MC info
    bmc_firmware: str = ''
    device_id: str = ''
    manufacturer_id: str = ''

    # Power
    power_status: str = ''

    # Raw outputs for debugging
    raw: dict = field(default_factory=dict)


def _run_ipmitool(ip: str, user: str, password: str, *args: str,
                  timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """Run a single ipmitool command, return (stdout, stderr)."""
    cmd = [
        IPMITOOL_BIN, '-I', 'lanplus',
        '-H', ip, '-U', user, '-P', password,
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return '', f'Timeout after {timeout}s'
    except FileNotFoundError:
        return '', 'ipmitool not found on this system'
    except Exception as exc:
        return '', str(exc)


def _parse_primary_os_name(raw: str) -> dict:
    """Parse 'v=1 mh=eagle-ma006 mps=APS-M1-TW20230110' into components."""
    result = {'raw': raw, 'v': '', 'mh': '', 'mps': ''}
    if not raw:
        return result
    for token in raw.split():
        if '=' in token:
            k, _, v = token.partition('=')
            if k in result:
                result[k] = v
    return result


def _parse_fru(raw: str) -> dict:
    """Extract key fields from `ipmitool fru print` output."""
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip().lower()
        val = val.strip()
        if 'board mfg' in key and 'date' not in key:
            data['board_mfg'] = val
        elif 'board product' in key:
            data['board_product'] = val
        elif 'board serial' in key:
            data['board_serial'] = val
        elif 'board part' in key:
            data['board_part'] = val
        elif 'product name' in key:
            data['product_name'] = val
        elif 'product serial' in key:
            data['product_serial'] = val
    return data


def _parse_mc_info(raw: str) -> dict:
    """Extract key fields from `ipmitool mc info`."""
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip().lower()
        val = val.strip()
        if 'firmware revision' in key:
            data['firmware'] = val
        elif 'device id' in key:
            data['device_id'] = val
        elif 'manufacturer id' in key:
            data['manufacturer_id'] = val
    return data


def _parse_power(raw: str) -> str:
    """Parse `chassis power status` => 'on' or 'off'."""
    low = raw.lower()
    if 'is on' in low:
        return 'on'
    elif 'is off' in low:
        return 'off'
    return raw


def fetch_bmc_info(hostname: str, ip: str, user: str, password: str,
                   timeout: int = DEFAULT_TIMEOUT) -> BmcInfo:
    """Query a single BMC for all available information."""
    info = BmcInfo(hostname=hostname, ip=ip)

    if not ip:
        info.error = 'No IP address'
        return info

    # 1. primary_os_name (most important per requirements)
    out, err = _run_ipmitool(ip, user, password,
                             'bmc', 'getsysinfo', 'primary_os_name',
                             timeout=timeout)
    if err and 'not found' in err.lower():
        info.error = 'ipmitool not installed'
        return info

    info.raw['primary_os_name'] = out or err
    if out and 'error' not in out.lower() and 'not supported' not in out.lower():
        info.primary_os_name_raw = out
        parsed = _parse_primary_os_name(out)
        info.primary_os_version = parsed['v']
        info.master_host = parsed['mh']
        info.mgmt_primary_system = parsed['mps']
        info.reachable = True

    # 2. FRU print
    out, err = _run_ipmitool(ip, user, password, 'fru', 'print', timeout=timeout)
    info.raw['fru'] = out or err
    if out:
        info.reachable = True
        fru = _parse_fru(out)
        info.fru_board_mfg = fru.get('board_mfg', '')
        info.fru_board_product = fru.get('board_product', '')
        info.fru_board_serial = fru.get('board_serial', '')
        info.fru_board_part = fru.get('board_part', '')
        info.fru_product_name = fru.get('product_name', '')
        info.fru_product_serial = fru.get('product_serial', '')

    # 3. MC info
    out, err = _run_ipmitool(ip, user, password, 'mc', 'info', timeout=timeout)
    info.raw['mc_info'] = out or err
    if out:
        info.reachable = True
        mc = _parse_mc_info(out)
        info.bmc_firmware = mc.get('firmware', '')
        info.device_id = mc.get('device_id', '')
        info.manufacturer_id = mc.get('manufacturer_id', '')

    # 4. Power status
    out, err = _run_ipmitool(ip, user, password,
                             'chassis', 'power', 'status', timeout=timeout)
    info.raw['power'] = out or err
    if out:
        info.reachable = True
        info.power_status = _parse_power(out)

    if not info.reachable:
        info.error = err or 'BMC unreachable'

    return info


def fetch_all_bmcs(
    targets: list[dict],
    max_workers: int = 12,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[BmcInfo]:
    """
    Query multiple BMCs in parallel.

    Each target dict must have: hostname, ip, user, password.
    Returns a list of BmcInfo in the same order as targets.
    """
    results: dict[int, BmcInfo] = {}

    def _fetch(idx: int, t: dict) -> tuple[int, BmcInfo]:
        return idx, fetch_bmc_info(
            hostname=t['hostname'],
            ip=t['ip'],
            user=t['user'],
            password=t['password'],
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets) or 1)) as pool:
        futures = {
            pool.submit(_fetch, i, t): i
            for i, t in enumerate(targets)
        }
        for future in as_completed(futures):
            try:
                idx, info = future.result()
                results[idx] = info
            except Exception as exc:
                idx = futures[future]
                t = targets[idx]
                results[idx] = BmcInfo(
                    hostname=t['hostname'], ip=t['ip'],
                    error=str(exc))

    return [results[i] for i in range(len(targets))]
