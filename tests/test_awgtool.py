"""Helpers behind awg-vpn and awg-move: generated configs, container replay, database edits."""
import importlib.util
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("awgtool", ROOT / "scripts/awgtool.py")
awgtool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(awgtool)

# Shape of `docker inspect` for a container the AmneziaVPN app created.
APP_CONTAINER = {
    "Name": "/amnezia-awg",
    "Config": {"Image": "amnezia-awg", "Entrypoint": ["dumb-init", "/opt/amnezia/start.sh"], "Cmd": [""]},
    "HostConfig": {
        "Binds": ["/lib/modules:/lib/modules"], "CapAdd": ["CAP_NET_ADMIN", "CAP_SYS_MODULE"], "Privileged": True,
        "NetworkMode": "bridge", "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0},
        "PortBindings": {"42080/udp": [{"HostIp": "", "HostPort": "42080"}]},
        "Sysctls": {"net.ipv4.conf.all.src_valid_mark": "1"}, "LogConfig": {"Type": "none", "Config": {}},
        "Devices": [],
    },
}


class GeneratedServerTests(unittest.TestCase):
    def test_obfuscation_stays_within_amneziawg_limits(self):
        for seed in range(500):
            p = awgtool.obfuscation(random.Random(seed))
            self.assertTrue(3 <= p["Jc"] <= 10)
            self.assertLess(p["Jmin"], p["Jmax"])
            self.assertTrue(15 <= p["S1"] <= 150 and 15 <= p["S2"] <= 150)
            self.assertNotEqual(p["S1"] + 56, p["S2"])
            headers = [p["H1"], p["H2"], p["H3"], p["H4"]]
            self.assertEqual(len(set(headers)), 4)
            self.assertTrue(all(5 <= h <= 2147483647 for h in headers))

    def test_interface_round_trips_through_config_info(self):
        text = awgtool.interface("PRIVATE=", 42080, "10.9.0.0/24", random.Random(1))
        self.assertTrue(text.startswith("[Interface]\nPrivateKey = PRIVATE=\nAddress = 10.9.0.0/24\nListenPort = 42080\n"))
        info = awgtool.config_info(text + "[Peer]\nPublicKey = A\nAllowedIPs = 10.9.0.2/32\n")
        self.assertEqual((info["port"], info["subnet"], info["peers"]), (42080, "10.9.0.0/24", 1))
        self.assertEqual(set(info["params"]), {"Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"})

    def test_bad_port_or_subnet_is_refused(self):
        with self.assertRaises(ValueError):
            awgtool.interface("K", 80, "10.8.1.0/24", random.Random(1))
        with self.assertRaises(ValueError):
            awgtool.interface("K", 42080, "10.8.1.0/30", random.Random(1))

    def test_start_script_matches_the_app_layout(self):
        script = awgtool.start_script("10.8.1.0/24")
        self.assertIn("wg-quick up /opt/amnezia/awg/wg0.conf", script)
        self.assertIn("iptables -t nat -A POSTROUTING -s 10.8.1.0/24 -o eth0 -j MASQUERADE", script)
        self.assertTrue(script.rstrip().endswith("tail -f /dev/null"))

    def test_config_info_reads_an_app_config_with_server_address(self):
        info = awgtool.config_info("[Interface]\nAddress = 10.8.1.1/24\nListenPort = 51820\nJc = 5\n\n[Peer]\n[Peer]\n")
        self.assertEqual((info["port"], info["subnet"], info["peers"]), (51820, "10.8.1.0/24", 2))


class ContainerReplayTests(unittest.TestCase):
    def test_app_container_is_recreated_with_the_same_options(self):
        self.assertEqual(awgtool.create_args(APP_CONTAINER), [
            "--name", "amnezia-awg", "--restart", "always", "--privileged",
            "--cap-add", "CAP_NET_ADMIN", "--cap-add", "CAP_SYS_MODULE",
            "--sysctl", "net.ipv4.conf.all.src_valid_mark=1", "-v", "/lib/modules:/lib/modules",
            "-p", "42080:42080/udp", "--log-driver", "none", "amnezia-awg"])
        self.assertEqual(awgtool.published_udp(APP_CONTAINER), [42080])

    def test_host_network_is_refused(self):
        container = json.loads(json.dumps(APP_CONTAINER))
        container["HostConfig"]["NetworkMode"] = "host"
        with self.assertRaises(ValueError):
            awgtool.create_args(container)

    def test_cli_reads_docker_inspect_list(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "inspect.json"
            path.write_text(json.dumps([APP_CONTAINER]))
            out = subprocess.run([sys.executable, str(ROOT / "scripts/awgtool.py"), "udp-ports", str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(out.stdout.split(), ["42080"])


class DatabaseTests(unittest.TestCase):
    def test_move_updates_saved_ip_without_replacing_selected_dns(self):
        with TemporaryDirectory() as d:
            path=self.make_db(d)
            db=sqlite3.connect(path)
            config=json.loads(db.execute('SELECT config FROM servers WHERE id=3').fetchone()[0])
            config['endpoint_ip']='198.51.100.7'
            db.execute('UPDATE servers SET config=? WHERE id=3',(json.dumps(config),));db.commit();db.close()
            self.assertEqual(awgtool.set_endpoint(str(path),'198.51.100.7','203.0.113.10'),['Local','Named'])
            db=sqlite3.connect(path)
            config=json.loads(db.execute('SELECT config FROM servers WHERE id=3').fetchone()[0]);db.close()
            self.assertEqual(config['endpoint'],'vpn.example.com')
            self.assertEqual(config['endpoint_ip'],'203.0.113.10')

    def make_db(self, d):
        path = Path(d) / "manager.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT, config TEXT)")
        rows = [(1, "Local", {"local": True, "container": "amnezia-awg", "endpoint": "198.51.100.7", "password": "secret"}),
                (2, "Remote", {"local": False, "container": "amnezia-awg", "endpoint": "198.51.100.7"}),
                (3, "Named", {"local": True, "container": "amnezia-awg2", "endpoint": "vpn.example.com"})]
        db.executemany("INSERT INTO servers VALUES (?,?,?)", [(i, n, json.dumps(c)) for i, n, c in rows])
        db.commit(); db.close()
        return path

    def test_servers_lists_containers_without_secrets(self):
        with TemporaryDirectory() as d:
            listed = awgtool.servers(str(self.make_db(d)))
        self.assertEqual([s["container"] for s in listed if s["local"]], ["amnezia-awg", "amnezia-awg2"])
        self.assertNotIn("secret", json.dumps(listed))

    def test_only_local_servers_with_the_old_ip_get_the_new_one(self):
        with TemporaryDirectory() as d:
            path = self.make_db(d)
            self.assertEqual(awgtool.set_endpoint(str(path), "198.51.100.7", "203.0.113.10"), ["Local"])
            endpoints = {s["name"]: s["endpoint"] for s in awgtool.servers(str(path))}
            stored = json.loads(sqlite3.connect(path).execute("SELECT config FROM servers WHERE id=1").fetchone()[0])
        self.assertEqual(endpoints, {"Local": "203.0.113.10", "Remote": "198.51.100.7", "Named": "vpn.example.com"})
        self.assertEqual(stored["password"], "secret")

    def test_is_ip(self):
        self.assertTrue(awgtool.is_ip("203.0.113.10"))
        self.assertFalse(awgtool.is_ip("vpn.example.com"))


class ScriptTests(unittest.TestCase):
    def test_move_empty_server_lists_emit_no_phantom_entries(self):
        source = (ROOT / "scripts/awg-move.sh").read_text()
        for name in ("CONTAINERS", "ENDPOINT_IPS", "DOMAINS"):
            code = re.search(r"mapfile -t " + name + r" < <\(python3 -c '(.*?)' \"\$SERVERS\"\)", source, re.S).group(1)
            result = subprocess.run([sys.executable, "-c", code, "[]"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "", name)

    def test_move_cleanup_does_not_touch_destination_before_preparation(self):
        source = (ROOT / "scripts/awg-move.sh").read_text()
        quote = re.search(r"^q\(\) .*\n", source, re.M).group()
        cleanup = re.search(r"^cleanup\(\) \{.*?^\}", source, re.M | re.S).group()
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "remote-called"
            shell = r'''
REMOTE_TMP=""
WORK=""
SOCK=unused
SSH_PORT=22
HOST=unused
remote() { touch "$MARKER"; }
ssh() { :; }
rm() { :; }
'''
            result = subprocess.run(["bash", "-c", shell + quote + cleanup + "\ncleanup\n"], env={**os.environ, "MARKER": str(marker)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), "Read-only preflight must not run remote cleanup")

    def test_move_stops_when_package_installation_fails_even_if_docker_exists(self):
        source = (ROOT / "scripts/awg-move.sh").read_text()
        install = re.search(r"^install_packages\(\) \{.*?^\}", source, re.M | re.S).group()
        shell = r'''
remote() { bash -c "$1"; }
apt-get() { return 43; }
docker() { echo UNEXPECTED_DOCKER_CALL >&2; }
systemctl() { echo UNEXPECTED_SYSTEMCTL_CALL >&2; }
export -f apt-get docker systemctl
PACKAGES=docker.io
'''
        result = subprocess.run(["bash", "-c", shell + install + "\ninstall_packages\n"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertNotIn("UNEXPECTED", result.stderr)

    def test_move_copies_archive_to_one_container_destination_argument(self):
        source = (ROOT / "scripts/awg-move.sh").read_text()
        quote = re.search(r"^q\(\) .*\n", source, re.M).group()
        copy = re.search(r"^copy_container\(\) \{.*?^\}", source, re.M | re.S).group()
        shell = r'''
set -euo pipefail
docker() {
    if [[ "$1" == exec ]]; then printf archive;
    else
        [[ "$#" == 3 && "$1" == cp && "$2" == - && "$3" == amnezia-awg:/ ]] || return 42
        [[ "$(cat)" == archive ]]
    fi
}
export -f docker
remote() { bash -c "$1"; }
'''
        result = subprocess.run(["bash", "-c", shell + quote + copy + "\ncopy_container amnezia-awg\n"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scripts_parse_and_show_help(self):
        for name in ("awg-move.sh", "awg-vpn.sh"):
            subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
        out = subprocess.run(["bash", str(ROOT / "scripts/awg-move.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("awg-move НОВЫЙ_IP", out.stdout)


if __name__ == "__main__":
    unittest.main()
