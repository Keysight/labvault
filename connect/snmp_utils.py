"""Minimal SNMP stubs — Hyperview SNMP stack is hard-dumped on customer SKU."""


def snmp_get(*args, **kwargs):
    raise RuntimeError("SNMP helpers are not shipped on the customer SKU")


def snmp_walk(*args, **kwargs):
    return []
