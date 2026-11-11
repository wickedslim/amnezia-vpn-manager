import json
import unittest

from manager.amnezia_key import connection, decode, vpn_key
from manager.domain import ManagerError

CONF = """[Interface]
Address = 10.8.1.7/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = cHJpdmF0ZS1rZXktZm9yLXRlc3RzLW9ubHktMDAwMDA=
Jc = 4
Jmin = 10
Jmax = 50
S1 = 112
S2 = 45
H1 = 429938500
H2 = 116602496
H3 = 742503079
H4 = 997711399
I1 = <b 0x00>
I2 = 

[Peer]
PublicKey = c2VydmVyLXB1YmxpYy1rZXktZm9yLXRlc3RzLTAwMDA=
PresharedKey = cHJlc2hhcmVkLWtleS1mb3ItdGVzdHMtb25seS0wMDA=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 203.0.113.5:39456
PersistentKeepalive = 25
"""


class AmneziaKeyTests(unittest.TestCase):
    def test_key_is_the_app_json_compressed_like_qcompress(self):
        key = vpn_key(CONF, "Амстердам", "Y2xpZW50LXB1YmxpYy1rZXktZm9yLXRlc3RzLTAwMDA=")
        self.assertTrue(key.startswith("vpn://"))
        self.assertNotIn("=", key[6:])
        self.assertNotIn("+", key)
        self.assertNotIn("/", key[6:])
        entry = decode(key)
        # The app's checkConfigFormat looks for "containers" to treat it as its own format.
        self.assertEqual(entry["defaultContainer"], "amnezia-awg")
        self.assertEqual(entry["description"], "Амстердам")
        self.assertEqual((entry["hostName"], entry["dns1"], entry["dns2"]), ("203.0.113.5", "1.1.1.1", "1.0.0.1"))
        container = entry["containers"][0]
        self.assertEqual(container["container"], "amnezia-awg")
        awg = container["awg"]
        self.assertEqual((awg["port"], awg["transport_proto"], awg["Jc"], awg["H4"]), ("39456", "udp", "4", "997711399"))
        last = json.loads(awg["last_config"])
        self.assertEqual(last["config"], CONF)
        self.assertEqual(last["port"], 39456)
        self.assertEqual(last["client_ip"], "10.8.1.7")
        self.assertEqual(awg["subnet_address"], "10.8.1.0")
        self.assertIs(entry["nameOverriddenByUser"], True)
        self.assertEqual(last["client_priv_key"], "cHJpdmF0ZS1rZXktZm9yLXRlc3RzLW9ubHktMDAwMDA=")
        self.assertEqual(last["server_pub_key"], "c2VydmVyLXB1YmxpYy1rZXktZm9yLXRlc3RzLTAwMDA=")
        self.assertEqual(last["psk_key"], "cHJlc2hhcmVkLWtleS1mb3ItdGVzdHMtb25seS0wMDA=")
        self.assertEqual(last["allowed_ips"], ["0.0.0.0/0", "::/0"])
        self.assertEqual((last["persistent_keep_alive"], last["mtu"], last["I1"]), ("25", "1280", "<b 0x00>"))
        self.assertEqual((last["I2"], awg["I2"]), ("", ""))
        self.assertEqual(last["clientId"], "Y2xpZW50LXB1YmxpYy1rZXktZm9yLXRlc3RzLTAwMDA=")

    def test_plain_wireguard_and_ipv6_endpoint(self):
        plain = "\n".join(line for line in CONF.splitlines() if not line.split("=")[0].strip() in {"Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4", "I1"})
        entry = connection(plain.replace("203.0.113.5:39456", "[2001:db8::5]:51820"), "srv")
        self.assertEqual(entry["defaultContainer"], "amnezia-wireguard")
        self.assertEqual(entry["hostName"], "2001:db8::5")
        self.assertEqual(json.loads(entry["containers"][0]["wireguard"]["last_config"])["mtu"], "1280")

    def test_broken_config_is_reported(self):
        with self.assertRaises(ManagerError):
            connection("[Interface]\nPrivateKey = x\n", "srv")
