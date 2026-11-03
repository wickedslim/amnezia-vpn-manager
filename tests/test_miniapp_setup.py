import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('miniapp_setup',Path(__file__).resolve().parents[1]/'scripts/miniapp-setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

class MiniAppSetupTests(unittest.TestCase):
    def test_existing_enabled_panel_skips_wizard(self):
        self.assertTrue(setup.configured('AWG_WEB_URL=https://vpn.example.com AWG_WEB_DISABLED=0'))
        self.assertFalse(setup.configured('AWG_WEB_URL=https://vpn.example.com AWG_WEB_DISABLED=1'))
        self.assertFalse(setup.configured(''))

    def test_domain_validation(self):
        self.assertTrue(setup.valid_domain('my-vpn.duckdns.org',duck=True))
        for name in ['https://my.duckdns.org','my.duckdns.org.evil.com','x.y.duckdns.org','-bad.duckdns.org','bad..org']:
            self.assertFalse(setup.valid_domain(name,duck=True))
        self.assertTrue(setup.valid_domain('vpn.example.com'))

    def test_dns_rejects_mismatched_aaaa_and_resolution_failure(self):
        records=[(None,None,None,None,('203.0.113.1',443)),(None,None,None,None,('2001:db8::1',443))]
        with patch.object(setup.socket,'getaddrinfo',return_value=records):
            self.assertFalse(setup.dns_matches('vpn.example.com',{'203.0.113.1'}))
            self.assertTrue(setup.dns_matches('vpn.example.com',{'203.0.113.1','2001:db8::1'}))
        with patch.object(setup.socket,'getaddrinfo',side_effect=OSError):
            self.assertFalse(setup.dns_matches('vpn.example.com',set()))

    def test_duckdns_retry_then_continue(self):
        with patch.object(setup,'ask',side_effect=['2','203.0.113.1','my-vpn.duckdns.org','']),patch.object(setup,'local_addresses',return_value=set()),patch.object(setup,'dns_matches',side_effect=[False,True]):
            self.assertEqual(setup.plan(),{'domain':'my-vpn.duckdns.org','ip':'203.0.113.1'})

    def test_skip_failed_dns_keeps_bot(self):
        with patch.object(setup,'ask',side_effect=['1','','vpn.example.com','0']),patch.object(setup,'local_addresses',return_value={'203.0.113.1'}),patch.object(setup,'dns_matches',return_value=False):
            self.assertEqual(setup.plan(),{})
        with patch.object(setup,'ask',return_value='3'):
            self.assertEqual(setup.plan(),{})
