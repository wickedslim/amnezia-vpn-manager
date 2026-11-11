import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from manager import shaping
from manager.domain import ManagerError, Server
from manager.service import VPNService
from manager.store import Store
from helpers import FakeRunner, fixture


class ShapingUnitTests(unittest.TestCase):
    def test_desired_takes_only_limited_clients_with_a_host_address(self):
        clients = [{"allowed_ips": "10.8.1.2/32", "speed_limit": 8}, {"allowed_ips": "10.8.1.3/32", "speed_limit": None},
                   {"allowed_ips": "10.8.1.4/32, fd00::4/128", "speed_limit": 20}, {"allowed_ips": "10.8.2.0/24", "speed_limit": 5}]
        self.assertEqual(shaping.desired(clients), {"10.8.1.2": 8, "10.8.1.4": 20})

    def test_script_limits_both_directions_per_client_and_clears_old_rules(self):
        script = shaping.apply_script("wg0", {"10.8.1.2": 8})
        self.assertIn("tc qdisc del dev wg0 root", script)
        self.assertIn("htb rate 8mbit ceil 8mbit", script)
        self.assertIn("match ip dst 10.8.1.2/32", script)
        self.assertIn("parent ffff: protocol ip prio 1 u32 match ip src 10.8.1.2/32 police rate 8mbit", script)
        self.assertNotIn("replace", script)

    def test_empty_limits_remove_shaping_and_add_nothing(self):
        script = shaping.apply_script("wg0", {})
        self.assertIn("tc qdisc del dev wg0 root", script)
        self.assertNotIn(" add ", script)

    def test_script_rejects_bad_input(self):
        with self.assertRaises(ManagerError):
            shaping.apply_script("wg0; reboot", {})
        with self.assertRaises(ManagerError):
            shaping.apply_script("wg0", {"10.8.1.2": 0})
        with self.assertRaises(ValueError):
            shaping.apply_script("wg0", {"10.8.1.2; reboot": 5})

    def test_in_sync_detects_missing_changed_and_matching_rules(self):
        limits = {"10.8.1.2": 8}
        number = f"{shaping.minor('10.8.1.2'):x}"
        good = (f"qdisc htb 1: root refcnt 2\nqdisc ingress ffff: parent ffff:fff1\n\x1e\nclass htb 1:1 root rate 10Gbit ceil 10Gbit\n"
                f"class htb 1:{number} parent 1:1 leaf 8002: prio 0 rate 8Mbit ceil 8Mbit\n\x1e\n police 0x1 rate 8Mbit burst 400Kb\n match 0a080102/ffffffff at 12\n")
        self.assertTrue(shaping.in_sync(good, limits, "wg0"))
        self.assertFalse(shaping.in_sync(good, {"10.8.1.2": 9}, "wg0"))
        self.assertFalse(shaping.in_sync(good, {"10.8.1.2": 8, "10.8.1.3": 8}, "wg0"))
        self.assertFalse(shaping.in_sync("qdisc fq_codel 0: root\n\x1e\n\x1e\n", limits, "wg0"))
        self.assertTrue(shaping.in_sync("qdisc fq_codel 0: root\n\x1e\n\x1e\n", {}, "wg0"))


class RecordingRunner(FakeRunner):
    scripts = []
    healthy = False

    def docker(self, script, data=None):
        if "tc qdisc show" in script:
            RecordingRunner.scripts.append("check")
            return RecordingRunner.state
        if "tc qdisc" in script:
            RecordingRunner.scripts.append(script)
            return ""
        return super().docker(script, data)


class ServiceSpeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.vpn = self.root / "vpn"
        fixture(self.vpn)
        self.server = self.store.add_server(Server(0, "test", host="test", config_path=str(self.vpn / "wg0.conf"), endpoint="203.0.113.5"))
        self.service = VPNService(self.store, RecordingRunner)
        RecordingRunner.scripts = []
        RecordingRunner.state = "qdisc fq_codel 0: root\n\x1e\n\x1e\n"

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_servers_without_limits_are_never_touched(self):
        await self.service.snapshot(self.server.id, force=True)
        await self.service.apply_speed(self.server.id)
        self.assertEqual(RecordingRunner.scripts, [])

    async def test_policy_speed_is_stored_applied_and_removed(self):
        sid = self.server.id
        client = await self.service.create_client(sid, "phone", None, None, 12)
        self.assertEqual(self.store.client(sid, client["id"])["speed_limit"], 12)
        await self.service.apply_speed(sid)
        self.assertTrue(any("htb rate 12mbit" in s for s in RecordingRunner.scripts if s != "check"))
        self.assertTrue(self.store.get_pref(f"speed_active:{sid}"))
        self.store.set_policy(sid, client["id"], None, None, None)
        RecordingRunner.scripts.clear()
        await self.service.apply_speed(sid)
        cleanup = [s for s in RecordingRunner.scripts if s != "check"][-1]
        self.assertIn("tc qdisc del dev wg0 root", cleanup)
        self.assertNotIn(" add ", cleanup)
        self.assertFalse(self.store.get_pref(f"speed_active:{sid}"))

    async def test_rules_lost_after_container_restart_are_restored_by_the_monitor(self):
        sid = self.server.id
        client = await self.service.create_client(sid, "phone", None, None, 12)
        await self.service.apply_speed(sid)
        RecordingRunner.scripts.clear()
        self.service.speed_checked[sid] = (self.service.speed_checked[sid][0], time.monotonic() - 120)
        await self.service.snapshot(sid, force=True)
        self.assertEqual(RecordingRunner.scripts[0], "check")
        self.assertTrue(any("htb rate 12mbit" in s for s in RecordingRunner.scripts[1:]))

    async def test_failure_to_apply_does_not_break_reading_the_server(self):
        sid = self.server.id
        await self.service.create_client(sid, "phone", None, None, 12)
        def broken(self, script, data=None):
            if "tc qdisc" in script:
                raise ManagerError("tc missing")
            return FakeRunner.docker(self, script, data)
        original = RecordingRunner.docker
        RecordingRunner.docker = broken
        try:
            snapshot = await self.service.snapshot(sid, force=True)
            self.assertTrue(snapshot.config.peers)
        finally:
            RecordingRunner.docker = original
