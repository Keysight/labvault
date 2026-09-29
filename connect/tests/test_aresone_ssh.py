"""Unit tests for AresONE PCPU SSH parsing (no live SSH)."""

from django.test import SimpleTestCase

from connect.aresone_ssh import (
    _normalize_mgmt_ips,
    _parse_pcpu_blob,
    parse_free_m_mem_pct,
    parse_proc_stat_cpu_pct,
)


class AresOneSshParserTests(SimpleTestCase):
    def test_parse_free_m(self):
        out = """
              total        used        free      shared  buff/cache   available
Mem:           8192        2048        4096          64        2048        6000
Swap:             0           0           0
"""
        self.assertAlmostEqual(parse_free_m_mem_pct(out), 25.0)

    def test_parse_proc_stat_cpu(self):
        a = 'cpu  1000 0 500 8000 0 0 0 0 0 0'
        b = 'cpu  1100 0 550 8500 0 0 0 0 0 0'
        pct = parse_proc_stat_cpu_pct(f'{a}\n{b}')
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 0.0)
        self.assertLessEqual(pct, 100.0)

    def test_normalize_mgmt_ips(self):
        ips = _normalize_mgmt_ips(['10.0.1.1', '10.0.1.1', '192.0.2.35', '', '10.0.2.3'])
        self.assertEqual(ips, ['10.0.1.1', '10.0.2.3'])

    def test_parse_pcpu_blob_with_markers(self):
        blob = """---LV_FREE---
              total        used        free
Mem:           4096        1024        3072
---LV_STAT---
cpu  291491 0 231262 266040030 60 0 1465 0 0 0
cpu  291500 0 231270 266041000 60 0 1465 0 0 0
"""
        parsed = _parse_pcpu_blob(blob)
        self.assertAlmostEqual(parsed['mem_pct'], 25.0)
        self.assertIsNotNone(parsed['cpu_pct'])
