"""Unit tests for live fleet heartbeat helpers (no hardware required)."""

from django.test import SimpleTestCase, override_settings

from connect.fleet_heartbeat import _classify, seeded_mode
from connect.reachability import expand_probe_hosts, dns_suffixes


class ReachabilityHelpersTests(SimpleTestCase):
    def test_expand_probe_hosts_with_suffix(self):
        with override_settings():
            import os
            os.environ['LABVAULT_DNS_SUFFIXES'] = 'lbj.is.keysight.com'
            hosts = expand_probe_hosts('ares1', '10.1.2.3')
            self.assertIn('ares1', hosts)
            self.assertIn('ares1.lbj.is.keysight.com', hosts)
            self.assertIn('10.1.2.3', hosts)
            # IP should not get a suffix
            self.assertFalse(any(h.startswith('10.1.2.3.') for h in hosts))

    def test_dns_suffixes_parse(self):
        import os
        os.environ['LABVAULT_DNS_SUFFIXES'] = 'a.example, .b.example'
        self.assertEqual(dns_suffixes(), ['a.example', 'b.example'])


class HeartbeatClassifyTests(SimpleTestCase):
    def test_probe_ok_recent_is_healthy(self):
        from django.utils import timezone
        now = timezone.now()
        row = _classify(
            {
                'last_probe_ok': True,
                'last_heartbeat_at': now.isoformat(),
            },
            interval=30,
            now=now,
        )
        self.assertTrue(row['heartbeat_ok'])
        self.assertFalse(row['halt_suspect'])

    def test_probe_fail_is_halt(self):
        from django.utils import timezone
        now = timezone.now()
        row = _classify(
            {
                'last_probe_ok': False,
                'last_heartbeat_at': now.isoformat(),
                'halt_reason': 'probe_auth_failed',
            },
            interval=30,
            now=now,
        )
        self.assertFalse(row['heartbeat_ok'])
        self.assertTrue(row['halt_suspect'])
        self.assertEqual(row['halt_reason'], 'probe_auth_failed')

    def test_heartbeat_mode_is_callable(self):
        from connect.fleet_heartbeat import heartbeat_mode
        self.assertTrue(callable(heartbeat_mode))
        import os
        os.environ['LABVAULT_HEARTBEAT_MODE'] = 'live'
        self.assertFalse(seeded_mode())
        os.environ['LABVAULT_HEARTBEAT_MODE'] = 'seeded'
        self.assertTrue(seeded_mode())
        os.environ.pop('LABVAULT_HEARTBEAT_MODE', None)
        self.assertFalse(seeded_mode())  # default is live
