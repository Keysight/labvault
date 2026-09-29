"""
Full LabVault dataset export/import for migration and replication.

Use management commands export_labvault_dataset / import_labvault_dataset, or the
DATA → Export / Import UI (staff only).


Appliance identity is intentionally excluded from datasets. Never import/export SSH host keys, bootstrap credential files, CLI auth-throttle rows, or deployment encryption material. Those live under /var/lib/labvault/ and named volumes and must remain unique per appliance instance.
APPLIANCE_IDENTITY_EXCLUDED = True
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    AuditLog,
    ChangeLogEvent,
    ConfigBackup,
    Device,
    DeviceStateBaseline,
    KeysightBmcEndpoint,
    KeysightChassis,
    KeysightDeploymentJob,
    KeysightReservation,
    KeysightReservationItem,
    KeysightSubnetScan,
    LabTopology,
    LabTopologyLink,
    LabTopologyNode,
    RequestLog,
    TopologyLink,
)

FORMAT_ID = 'labvault-full-export'
FORMAT_VERSION = 1


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, '_meta') and hasattr(obj, 'pk'):
        return obj.pk
    raise TypeError(type(obj))


def _coerce_dt(value: Any):
    """Turn ISO strings from JSON export into timezone-aware datetimes."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = parse_datetime(value)
        if dt is None:
            # Fallback: date-only or naive common forms
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return value
    else:
        return value
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _defaults_for_model(model, row: dict) -> dict:
    """Keep only real model fields so older full exports still import."""
    from django.db import models as dj_models

    names = {f.name for f in model._meta.fields if not f.primary_key}
    auto = {
        f.name
        for f in model._meta.fields
        if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False)
    }
    filtered = {k: v for k, v in row.items() if k in names and k not in auto}
    # JSON stores FKs as source PKs; those IDs are not valid in a fresh database.
    # Callers re-bind relations by natural keys (IP, hostname). Drop stale PKs.
    for field in model._meta.fields:
        if not isinstance(field, (dj_models.ForeignKey, dj_models.OneToOneField)):
            continue
        if field.name not in filtered:
            continue
        val = filtered.pop(field.name)
        if val is None or val == '':
            filtered[field.attname] = None
        elif hasattr(val, '_meta'):
            filtered[field.name] = val
    return _coerce_model_datetimes(model, filtered)


def _coerce_model_datetimes(model, row: dict) -> dict:
    """Coerce DateTimeField / DateField values in a row dict for model create/update."""
    from django.db import models as dj_models

    out = dict(row)
    for field in model._meta.fields:
        if field.name not in out:
            continue
        if isinstance(field, (dj_models.DateTimeField, dj_models.DateField)):
            # Skip auto_now / auto_now_add — let Django manage them
            if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                out.pop(field.name, None)
                continue
            out[field.name] = _coerce_dt(out[field.name])
    return out


def _dt(val):
    if val is None:
        return None
    return val.isoformat() if hasattr(val, 'isoformat') else val


def _field_export_value(field, value):
    if value is None:
        return None
    if field.is_relation:
        return value.pk
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _model_dict(obj, *, exclude=None):
    exclude = set(exclude or ())
    row = {}
    for f in obj._meta.fields:
        if f.name in exclude or f.primary_key:
            continue
        row[f.name] = _field_export_value(f, getattr(obj, f.name))
    return row


def _export_capex_section():
    """Optional export section. Always empty in this tree."""
    return {
        'quarters': [],
        'requests': [],
        'asset_links': [],
        'documents': [],
        'audit_logs': [],
    }


def build_export_payload() -> dict:
    devices = []
    for d in Device.objects.all().order_by('ip_address'):
        devices.append(_model_dict(d))

    chassis = []
    for ch in KeysightChassis.objects.all().order_by('ip_address'):
        chassis.append(_model_dict(ch))

    reservations = []
    for r in KeysightReservation.objects.select_related('user').order_by('pk'):
        row = _model_dict(r, exclude={'user'})
        row['user_username'] = r.user.get_username() if r.user_id else ''
        reservations.append(row)

    res_items = []
    for item in KeysightReservationItem.objects.order_by('pk'):
        ch = item.chassis
        row = _model_dict(item, exclude={'chassis', 'reservation'})
        row['reservation_source_id'] = item.reservation_id
        row['chassis_ip'] = ch.ip_address if ch else ''
        res_items.append(row)

    bmc = []
    for x in KeysightBmcEndpoint.objects.select_related('chassis').all():
        row = _model_dict(x, exclude={'chassis'})
        row['chassis_ip'] = x.chassis.ip_address if x.chassis_id else ''
        bmc.append(row)

    subnet_scans = [_model_dict(x) for x in KeysightSubnetScan.objects.all()]
    deploy_jobs = []
    for x in KeysightDeploymentJob.objects.select_related('chassis').all():
        row = _model_dict(x, exclude={'chassis', 'started_by'})
        row['chassis_ip'] = x.chassis.ip_address if x.chassis_id else ''
        deploy_jobs.append(row)

    audit_logs = []
    for log in AuditLog.objects.select_related('user', 'device').order_by('-pk')[:50000]:
        audit_logs.append({
            'action': log.action,
            'details': log.details,
            'ip_address': str(log.ip_address) if log.ip_address else None,
            'user_agent': getattr(log, 'user_agent', '') or '',
            'username': log.user.get_username() if log.user_id else '',
            'device_ip': log.device.ip_address if log.device_id else '',
            'timestamp': _dt(log.timestamp),
        })

    request_logs = []
    for rl in RequestLog.objects.order_by('-pk')[:20000]:
        request_logs.append(_model_dict(rl, exclude={'user'}))

    config_backups = []
    for b in ConfigBackup.objects.select_related('device', 'created_by').order_by('-pk')[:5000]:
        config_backups.append({
            'device_ip': b.device.ip_address if b.device_id else '',
            'username': b.created_by.get_username() if b.created_by_id else '',
            'config_type': b.config_type,
            'content': b.content,
            'diff_from_previous': b.diff_from_previous,
            'created_at': _dt(b.created_at),
        })

    changelog = [_model_dict(e) for e in ChangeLogEvent.objects.order_by('-pk')[:50000]]
    baselines = [_model_dict(b) for b in DeviceStateBaseline.objects.all()]

    from .lab_topology_io import export_topology

    topologies = [export_topology(topo) for topo in LabTopology.objects.all()]

    legacy_links = []
    for lnk in TopologyLink.objects.select_related('device_a', 'device_b').all():
        legacy_links.append({
            'device_a_ip': lnk.device_a.ip_address if lnk.device_a_id else '',
            'device_b_ip': lnk.device_b.ip_address if lnk.device_b_id else '',
            'port_a': lnk.port_a,
            'port_b': lnk.port_b,
            'speed': lnk.speed,
            'lag': lnk.lag,
            'discovered_via': lnk.discovered_via,
            'link_status': lnk.link_status,
            'last_seen': _dt(lnk.last_seen),
        })

    counts = {
        'devices': len(devices),
        'keysight_chassis': len(chassis),
        'reservations': len(reservations),
        'reservation_items': len(res_items),
        'audit_logs': len(audit_logs),
        'changelog_events': len(changelog),
        'lab_topologies': len(topologies),
    }

    return {
        'format': FORMAT_ID,
        'version': FORMAT_VERSION,
        'exported_at': timezone.now().isoformat(),
        'media_root_note': str(settings.MEDIA_ROOT),
        'counts': counts,
        'devices': devices,
        'keysight_chassis': chassis,
        'keysight_reservations': reservations,
        'keysight_reservation_items': res_items,
        'keysight_bmc_endpoints': bmc,
        'keysight_subnet_scans': subnet_scans,
        'keysight_deployment_jobs': deploy_jobs,
        'audit_logs': audit_logs,
        'request_logs': request_logs,
        'config_backups': config_backups,
        'changelog_events': changelog,
        'device_state_baselines': baselines,
        'lab_topologies': topologies,
        'topology_links': legacy_links,
        'capex': _export_capex_section(),
    }


def write_export_file(path: str) -> dict:
    payload = build_export_payload()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, default=_json_default)
    return payload.get('counts', {})


def _import_capex(capex_payload: dict, stats: dict):
    """Optional import section. Ignored in this tree."""
    if capex_payload:
        stats['capex_skipped'] = True
        stats['capex_note'] = 'section skipped'


@transaction.atomic
def import_from_payload(payload: dict, *, import_capex: bool = True, skip_logs: bool = False) -> dict:
    if payload.get('format') != FORMAT_ID:
        raise ValueError(f'Expected format {FORMAT_ID!r}, got {payload.get("format")!r}')

    stats = {
        'devices_created': 0,
        'devices_updated': 0,
        'chassis_created': 0,
        'chassis_updated': 0,
        'reservations': 0,
        'audit_logs': 0,
        'changelog_events': 0,
    }

    for row in payload.get('devices', []):
        ip = (row.get('ip_address') or '').strip()
        if not ip:
            continue
        defaults = _defaults_for_model(
            Device, {k: v for k, v in row.items() if k != 'ip_address'},
        )
        _, created = Device.objects.update_or_create(ip_address=ip, defaults=defaults)
        stats['devices_created' if created else 'devices_updated'] += 1

    chassis_by_ip = {}
    for row in payload.get('keysight_chassis', []):
        ip = (row.get('ip_address') or '').strip()
        if not ip:
            continue
        defaults = _defaults_for_model(
            KeysightChassis, {k: v for k, v in row.items() if k != 'ip_address'},
        )
        ch, created = KeysightChassis.objects.update_or_create(ip_address=ip, defaults=defaults)
        chassis_by_ip[ip] = ch
        stats['chassis_created' if created else 'chassis_updated'] += 1

    from django.contrib.auth import get_user_model
    User = get_user_model()

    res_id_map = {}
    fallback_user = (
        User.objects.filter(is_superuser=True).order_by('pk').first()
        or User.objects.filter(is_staff=True).order_by('pk').first()
        or User.objects.order_by('pk').first()
    )
    for raw in payload.get('keysight_reservations', []):
        row = dict(raw)
        uname = row.pop('user_username', '')
        source_id = row.pop('id', None)
        user = User.objects.filter(username=uname).first() if uname else None
        if user is None:
            user = fallback_user
        if user is None:
            continue
        row = _defaults_for_model(KeysightReservation, row)
        title = row.get('title') or f'import-{source_id}'
        start_time = row.get('start_time')
        end_time = row.get('end_time')
        if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
            continue
        obj, _ = KeysightReservation.objects.update_or_create(
            title=title,
            defaults={**row, 'user': user},
        )
        if source_id:
            res_id_map[source_id] = obj.pk
        stats['reservations'] += 1

    for raw in payload.get('keysight_reservation_items', []):
        row = dict(raw)
        rip = row.pop('chassis_ip', '')
        rsrc = row.pop('reservation_source_id', None)
        row.pop('reservation', None)
        row.pop('chassis', None)
        ch = chassis_by_ip.get(rip)
        res_pk = res_id_map.get(rsrc) if rsrc else None
        if not ch or not res_pk:
            continue
        item_defaults = _defaults_for_model(KeysightReservationItem, row)
        KeysightReservationItem.objects.update_or_create(
            reservation_id=res_pk,
            chassis=ch,
            slot_number=row.get('slot_number'),
            port_number=row.get('port_number'),
            defaults=item_defaults,
        )

    if not skip_logs:
        for row in payload.get('audit_logs', []):
            dip = row.pop('device_ip', '')
            device = Device.objects.filter(ip_address=dip).first() if dip else None
            from django.contrib.auth import get_user_model
            User = get_user_model()
            uname = row.pop('username', '')
            user = User.objects.filter(username=uname).first() if uname else None
            AuditLog.objects.create(
                user=user,
                device=device,
                action=row.get('action', ''),
                details=row.get('details', ''),
                ip_address=row.get('ip_address'),
                user_agent=row.get('user_agent', ''),
            )
            stats['audit_logs'] += 1

        for row in payload.get('changelog_events', []):
            cleaned = _defaults_for_model(ChangeLogEvent, dict(row))
            try:
                ChangeLogEvent.objects.create(**cleaned)
                stats['changelog_events'] += 1
            except Exception:
                continue
    else:
        stats['logs_skipped'] = True

    stats['bmc_endpoints'] = 0
    for raw in payload.get('keysight_bmc_endpoints', []):
        row = dict(raw)
        cip = (row.pop('chassis_ip', '') or '').strip()
        row.pop('chassis', None)
        host = (row.get('hostname') or '').strip()
        if not host:
            continue
        defaults = _defaults_for_model(KeysightBmcEndpoint, row)
        if cip:
            defaults['chassis'] = chassis_by_ip.get(cip)
        KeysightBmcEndpoint.objects.update_or_create(hostname=host, defaults=defaults)
        stats['bmc_endpoints'] += 1

    if import_capex and payload.get('capex'):
        _import_capex(payload['capex'], stats)

    stats.update(_import_lab_topologies(payload))
    _enable_pulse_if_lab_imported(stats)

    return stats


def _enable_pulse_if_lab_imported(stats: dict) -> None:
    """Uploading chassis/topology is the customer 'go live' signal — turn Pulse on."""
    chassis = int(stats.get("chassis_created") or 0) + int(stats.get("chassis_updated") or 0)
    topos = int(stats.get("topologies_imported") or 0)
    if not chassis and not topos:
        return
    from connect.runtime_settings import set_setting

    set_setting("collector_mode", "live")
    set_setting("heartbeat_mode", "live")
    stats["pulse"] = "live"


def _import_lab_topologies(payload: dict) -> dict:
    """Import embedded lab topologies (v3 export_topology shape)."""
    from .lab_topology_io import import_topology

    out = {'topologies_imported': 0, 'topology_names': []}
    for block in payload.get('lab_topologies', []):
        if not isinstance(block, dict):
            continue
        meta = block.get('topology') or {}
        name = (meta.get('name') or block.get('name') or '').strip()
        if not name:
            continue
        topo = LabTopology.objects.filter(name=name).first()
        if topo is None:
            topo = LabTopology.objects.create(
                name=name,
                description=meta.get('description') or block.get('description') or '',
                source=meta.get('source') or block.get('source') or 'dataset-import',
                tags=meta.get('tags') or block.get('tags') or '',
            )
        import_topology(topo, block)
        topo.metrics_collection_enabled = True
        topo.save(update_fields=['metrics_collection_enabled', 'updated_at'])
        out['topologies_imported'] += 1
        out['topology_names'].append(name)
    return out


def import_from_file(path: str, **kwargs) -> dict:
    with open(path, encoding='utf-8') as f:
        payload = json.load(f)
    return import_from_payload(payload, **kwargs)
