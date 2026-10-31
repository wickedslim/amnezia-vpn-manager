import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from manager.domain import ManagerError, Server, parse_config
from manager.service import VPNService
from manager.store import Store
from manager.transport import read_snapshot, write_peer
from helpers import FakeRunner, fixture


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.vpn = self.root / "vpn"
        fixture(self.vpn)
        self.server = self.store.add_server(Server(0,"test",host="test",config_path=str(self.vpn / "wg0.conf"),endpoint="203.0.113.5"))
        self.service = VPNService(self.store, FakeRunner)

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_snapshot_handles_adjacent_peers_and_duplicate_names(self):
        snapshot = await self.service.snapshot(self.server.id)
        self.assertEqual(len(snapshot.config.peers),2)
        self.assertEqual(len(self.store.clients(self.server.id)),2)

    async def test_create_and_remove_client_without_dropping_other_peers(self):
        before = json.loads((self.vpn / "runtime.json").read_text())
        created = await self.service.create_client(self.server.id,"my.client 東京",3600,5000000000)
        self.assertIn("S3 = 25", created["config"])
        self.assertEqual(created["allowed_ips"],"10.30.0.4/32")
        self.assertEqual(len(self.store.clients(self.server.id)),3)
        await self.service.delete_client(self.server.id,created["id"])
        after = json.loads((self.vpn / "runtime.json").read_text())
        self.assertEqual(before,after)
        self.assertEqual(len(parse_config((self.vpn / "wg0.conf").read_text()).peers),2)
        self.assertNotIn("down",(self.vpn / "commands").read_text())

    async def test_duplicate_name_deletion_uses_public_key(self):
        await self.service.snapshot(self.server.id)
        clients = self.store.clients(self.server.id)
        await self.service.delete_client(self.server.id,clients[1]["id"])
        remaining = self.store.clients(self.server.id)
        self.assertEqual([c["public_key"] for c in remaining],[clients[0]["public_key"]])

    async def test_failed_runtime_mutation_rolls_back_config_and_table(self):
        before = [(self.vpn / name).read_text() for name in ("wg0.conf","clientsTable","runtime.json")]
        (self.vpn / "fail_once").touch()
        with self.assertRaises(ManagerError):
            await self.service.create_client(self.server.id,"new",None,None)
        after = [(self.vpn / name).read_text() for name in ("wg0.conf","clientsTable","runtime.json")]
        self.assertEqual(before,after)

    async def test_compare_and_swap_rejects_external_change(self):
        runner = FakeRunner(self.server,self.store.directory)
        before, table, _ = read_snapshot(runner)
        (self.vpn / "wg0.conf").write_text(before + "\n# external change\n")
        with self.assertRaisesRegex(ManagerError,"изменилась"):
            write_peer(runner,before=before,after=before,table_before=table,table_after=table,interface="wg0",key="key",allowed_ips="ip",psk="")
        self.assertTrue((self.vpn / "wg0.conf").read_text().endswith("# external change\n"))

    async def test_monitor_enforces_policy_on_each_server_without_selection(self):
        other = self.root / "vpn2"
        fixture(other,names=("office401",))
        second = self.store.add_server(Server(0,"second",host="second",config_path=str(other / "wg0.conf"),endpoint="203.0.113.6"))
        await self.service.overview()
        target = self.store.clients(second.id)[0]
        self.store.set_policy(second.id,target["id"],int(time.time())-1,None)
        notifications = await self.service.monitor_once()
        self.assertEqual(len(notifications),1)
        self.assertEqual(notifications[0][0],second.id)
        self.assertEqual(len(self.store.clients(second.id)),0)
        self.assertEqual(len(self.store.clients(self.server.id)),2)

    async def test_slow_worker_does_not_block_event_loop(self):
        ticks = []
        async def ticker():
            await asyncio.sleep(0.02)
            ticks.append(True)
        await asyncio.gather(self.service.worker(lambda: time.sleep(0.1)),ticker())
        self.assertEqual(ticks,[True])

    async def test_cancelled_worker_keeps_lock_until_thread_finishes(self):
        events = []
        async def operation():
            async with self.service.lock(self.server.id):
                await self.service.worker(lambda: (time.sleep(0.1),events.append("finished")))
        task = asyncio.create_task(operation())
        await asyncio.sleep(0.02)
        task.cancel()
        async with self.service.lock(self.server.id):
            events.append("acquired")
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(events,["finished","acquired"])

    async def test_two_servers_refresh_concurrently(self):
        self.service._read = lambda sid: (time.sleep(0.12),sid)[1]
        other = self.store.add_server(Server(0,"other",host="other"))
        start = time.monotonic()
        result = await asyncio.gather(self.service.snapshot(self.server.id),self.service.snapshot(other.id))
        self.assertEqual(result,[self.server.id,other.id])
        self.assertLess(time.monotonic()-start,0.23)

    async def test_backup_contains_valid_database_and_server_snapshot(self):
        import io, zipfile
        data = await self.service.backup(self.server.id)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn("manager.sqlite3",archive.namelist())
            self.assertIn("server/wg0.conf",archive.namelist())

    async def test_bad_client_table_blocks_mutation(self):
        (self.vpn / "clientsTable").write_text("{broken")
        with self.assertRaises(ManagerError):
            await self.service.create_client(self.server.id,"new",None,None)
        self.assertEqual(len(parse_config((self.vpn / "wg0.conf").read_text()).peers),2)
