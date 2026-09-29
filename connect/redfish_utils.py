"""
Redfish client wrapper for modern BMCs.

Provides sensor collection and availability checks via DMTF Redfish REST API.
Falls back gracefully when Redfish is unavailable — callers should try IPMI next.

Uses the optional ``redfish`` package (``HAS_REDFISH``); without it every function
returns False / ``[]``. Connects to ``https://<ip>`` with session auth and always
logs out. Credentials are passed in by the caller; the keyword defaults are
placeholders and should not be relied on. Used by the ``ipmi_discover``
management command (``redfish_is_available``).
"""
import logging

logger = logging.getLogger('labvault.redfish_utils')

try:
    import redfish
    HAS_REDFISH = True
except ImportError:
    HAS_REDFISH = False


def redfish_is_available(ip, username='admin', password='admin', timeout=5):
    """Quick check: can we connect to Redfish on this host? (login + logout, no retry)"""
    if not HAS_REDFISH:
        return False
    try:
        ctx = redfish.redfish_client(
            base_url=f'https://{ip}',
            username=username, password=password,
            default_prefix='/redfish/v1',
            timeout=timeout, max_retry=0,
        )
        ctx.login(auth='session')
        ctx.logout()
        return True
    except Exception:
        return False


def redfish_get_sensors(ip, username='admin', password='admin', timeout=10):
    """
    Fetch sensor readings from a Redfish-capable BMC.

    Returns a list of dicts:
        {'name': str, 'type': str, 'value': float, 'unit': str}

    type is one of: temperature_c, fan_rpm, power_w, voltage_v, current_a, other
    """
    if not HAS_REDFISH:
        return []

    sensors = []
    ctx = None
    try:
        ctx = redfish.redfish_client(
            base_url=f'https://{ip}',
            username=username, password=password,
            default_prefix='/redfish/v1',
            timeout=timeout, max_retry=1,
        )
        ctx.login(auth='session')

        sensors.extend(_collect_chassis_thermal(ctx))
        sensors.extend(_collect_chassis_power(ctx))
        sensors.extend(_collect_telemetry_sensors(ctx))

    except Exception as e:
        logger.debug('Redfish sensor collection failed for %s: %s', ip, e)
    finally:
        if ctx:
            try:
                ctx.logout()
            except Exception:
                pass
    return sensors


def _collect_chassis_thermal(ctx):
    """Collect from /redfish/v1/Chassis/{id}/Thermal (Redfish 1.x)."""
    sensors = []
    try:
        chassis_resp = ctx.get('/redfish/v1/Chassis')
        members = chassis_resp.dict.get('Members', [])
        for member in members:
            uri = member.get('@odata.id', '')
            thermal_uri = f'{uri}/Thermal'
            try:
                resp = ctx.get(thermal_uri)
                data = resp.dict

                for temp in data.get('Temperatures', []):
                    reading = temp.get('ReadingCelsius')
                    if reading is not None:
                        sensors.append({
                            'name': temp.get('Name', 'temperature'),
                            'type': 'temperature_c',
                            'value': float(reading),
                            'unit': '°C',
                        })

                for fan in data.get('Fans', []):
                    reading = fan.get('Reading')
                    if reading is not None:
                        unit_raw = (fan.get('ReadingUnits') or 'RPM').upper()
                        if 'PERCENT' in unit_raw:
                            sensors.append({
                                'name': fan.get('Name', 'fan'),
                                'type': 'other',
                                'value': float(reading),
                                'unit': '%',
                            })
                        else:
                            sensors.append({
                                'name': fan.get('Name', 'fan'),
                                'type': 'fan_rpm',
                                'value': float(reading),
                                'unit': 'RPM',
                            })
            except Exception:
                continue
    except Exception:
        pass
    return sensors


def _collect_chassis_power(ctx):
    """Collect from /redfish/v1/Chassis/{id}/Power (Redfish 1.x)."""
    sensors = []
    try:
        chassis_resp = ctx.get('/redfish/v1/Chassis')
        members = chassis_resp.dict.get('Members', [])
        for member in members:
            uri = member.get('@odata.id', '')
            power_uri = f'{uri}/Power'
            try:
                resp = ctx.get(power_uri)
                data = resp.dict

                for psu in data.get('PowerSupplies', []):
                    watts = psu.get('PowerInputWatts') or psu.get('PowerOutputWatts')
                    if watts is not None:
                        sensors.append({
                            'name': psu.get('Name', 'psu'),
                            'type': 'power_w',
                            'value': float(watts),
                            'unit': 'W',
                        })

                for voltage in data.get('Voltages', []):
                    reading = voltage.get('ReadingVolts')
                    if reading is not None:
                        sensors.append({
                            'name': voltage.get('Name', 'voltage'),
                            'type': 'voltage_v',
                            'value': float(reading),
                            'unit': 'V',
                        })

                pc = data.get('PowerControl', [])
                for p in pc:
                    consumed = p.get('PowerConsumedWatts')
                    if consumed is not None:
                        sensors.append({
                            'name': p.get('Name', 'system_power'),
                            'type': 'power_w',
                            'value': float(consumed),
                            'unit': 'W',
                        })
            except Exception:
                continue
    except Exception:
        pass
    return sensors


def _collect_telemetry_sensors(ctx):
    """Collect from /redfish/v1/TelemetryService/Sensors (Redfish 2022+)."""
    sensors = []
    try:
        resp = ctx.get('/redfish/v1/TelemetryService')
        if resp.status != 200:
            return sensors

        sensor_collection_uri = resp.dict.get('Sensors', {}).get('@odata.id')
        if not sensor_collection_uri:
            return sensors

        coll = ctx.get(sensor_collection_uri)
        for member in coll.dict.get('Members', []):
            uri = member.get('@odata.id', '')
            try:
                s = ctx.get(uri).dict
                reading = s.get('Reading')
                if reading is None:
                    continue
                reading_type = (s.get('ReadingType') or '').lower()
                name = s.get('Name', 'sensor')

                if 'temperature' in reading_type:
                    sensors.append({'name': name, 'type': 'temperature_c', 'value': float(reading), 'unit': '°C'})
                elif 'rotational' in reading_type:
                    sensors.append({'name': name, 'type': 'fan_rpm', 'value': float(reading), 'unit': 'RPM'})
                elif 'power' in reading_type:
                    sensors.append({'name': name, 'type': 'power_w', 'value': float(reading), 'unit': 'W'})
                elif 'voltage' in reading_type:
                    sensors.append({'name': name, 'type': 'voltage_v', 'value': float(reading), 'unit': 'V'})
                elif 'current' in reading_type:
                    sensors.append({'name': name, 'type': 'current_a', 'value': float(reading), 'unit': 'A'})
                else:
                    sensors.append({'name': name, 'type': 'other', 'value': float(reading), 'unit': s.get('ReadingUnits', '')})
            except Exception:
                continue
    except Exception:
        pass
    return sensors
