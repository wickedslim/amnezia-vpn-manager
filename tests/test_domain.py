import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from manager.domain import ManagerError, allocate_address, client_config, parse_config, parse_dump, validate_name
from manager.ui import connection
from manager.views import client_text, progress, rates, sparkline
from helpers import fixture


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture(self.root)
        self.config = parse_config((self.root / "wg0.conf").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_adjacent_peers_and_duplicate_names_keep_distinct_keys(self):
        self.assertEqual(len(self.config.peers), 2)
        self.assertNotEqual(self.config.peers[0].public_key, self.config.peers[1].public_key)
        self.assertEqual([p.name for p in self.config.peers], ["office401"] * 2)

    def test_allocate_from_actual_subnet(self):
        self.assertEqual(allocate_address(self.config), "10.30.0.4/32")

    def test_roundtrip_preserves_obfuscation_and_all_peers(self):
        parsed = parse_config(self.config.render())
        self.assertEqual(parsed.interface, self.config.interface)
        self.assertEqual([p.public_key for p in parsed.peers], [p.public_key for p in self.config.peers])

    def test_client_config_keeps_new_amnezia_parameters(self):
        text = client_config(self.config, "10.30.0.4/32", "private", "public", "psk", "2001:db8::1")
        self.assertIn("S3 = 25", text)
        self.assertIn("I1 = <b 0x00>", text)
        self.assertIn("Endpoint = [2001:db8::1]:51820", text)

    def test_dump_missing_handshake_is_zero(self):
        public, peers = parse_dump("wg0\tprivate\tpublic\t51820\toff\nwg0\tkey\t(none)\t(none)\t10.0.0.2/32\t0\t10\t20\t0\n", "wg0")
        self.assertEqual(public, "public")
        self.assertEqual(peers["key"]["handshake"], 0)

    def test_missing_interface_is_an_error_not_empty_stats(self):
        with self.assertRaises(ManagerError):
            parse_dump("", "wg0")

    def test_amnezia_extended_dump_header_and_multiple_interfaces(self):
        text = "other\tprivate\totherpublic\t51820\toff\nwg0\tprivate\tpublic\t51820\t4\t40\t70\t0\t0\t1\t2\t3\t4\toff\nwg0\tkey\t(none)\t(none)\t10.0.0.2/32\t123\t10\t20\toff\n"
        public, peers = parse_dump(text,"wg0")
        self.assertEqual(public,"public")
        self.assertEqual(peers["key"]["received"],10)

    def test_dots_spaces_unicode_allowed(self):
        self.assertEqual(validate_name("  serv.host Москва  "), "serv.host Москва")

    def test_bad_name_rejected(self):
        with self.assertRaises(ManagerError):
            validate_name("name\nother")

    def test_connection_formats(self):
        self.assertEqual(connection("root@example.org:2222"), ("example.org", 2222, "root"))
        self.assertEqual(connection("203.0.113.1"), ("203.0.113.1", 22, "root"))
        self.assertEqual(connection("admin@[2001:db8::1]:2222"), ("2001:db8::1", 2222, "admin"))

    def test_bad_ports_rejected(self):
        for value in ("host:0", "host:65536", "host:abc", "$(id)"):
            with self.subTest(value=value), self.assertRaises(ManagerError):
                connection(value)

    def test_card_without_handshake_renders_and_escapes(self):
        text = client_text("<server>", {"name":"<client>","handshake":0,"total_rx":2,"total_tx":3,"expires":None,"allowed_ips":"10.0.0.2/32","traffic_limit":None})
        self.assertIn("Нет соединений", text)
        self.assertIn("&lt;client&gt;", text)
        self.assertNotIn("?d", text)

    def test_metrics_boundaries(self):
        self.assertIn("100%", progress(200, 100))
        self.assertEqual(rates([{"ts":10,"rx":0,"tx":10}, {"ts":20,"rx":100,"tx":30}]), (10,2))
        self.assertEqual(sparkline([0,0]), "▁▁")
