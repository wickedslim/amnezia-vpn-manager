import asyncio
import base64
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

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

    async def test_traffic_control_switches_only_the_matching_collector(self):
        sid = self.server.id
        prefix = "/opt/amnezia-service-traffic"
        command = f"{prefix}/venv/bin/python {prefix}/collector.py collect amnezia-awg wg0"
        def output(mode, active, args=command):
            return (f"LoadState=loaded\nActiveState={active}\nUnitFileState={mode}\n"
                    f"ExecStart={{ path={prefix}/venv/bin/python ; argv[]={args} ; ignore_errors=no }}")
        with patch.object(self.service.runner(sid), "run", create=True, side_effect=[
            output("disabled", "inactive"), "", "", output("enabled", "active"),
            output("enabled", "active"), "", output("disabled", "inactive"),
            output("enabled", "active", command.replace("wg0", "wg1")),
            output("enabled", "active", command.replace("wg0", "wg1")),
        ]) as run:
            self.assertEqual(await self.service.service_traffic_control(sid, True),
                             {"state": "ready", "enabled": True, "running": True})
            self.assertEqual(await self.service.service_traffic_control(sid, False),
                             {"state": "ready", "enabled": False, "running": False})
            self.assertEqual(await self.service.service_traffic_control(sid), {"state": "unmanaged"})
            with self.assertRaises(ManagerError):
                await self.service.service_traffic_control(sid, False)
            operations = [call.args[0] for call in run.call_args_list if " --now " in call.args[0]]
            self.assertEqual(operations, [
                "systemctl --no-ask-password enable --now amnezia-service-traffic.service",
                "systemctl --no-ask-password disable --now amnezia-service-traffic.service",
            ])

    async def test_traffic_install_transfers_bundled_scripts_and_does_not_start_service(self):
        sid = self.server.id
        existing = self.service.runner(sid)
        install_runner = Mock()
        install_runner.run.side_effect = [ManagerError("network"), "", (
            "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\n"
            "ExecStart={ argv[]=/opt/amnezia-service-traffic/venv/bin/python "
            "/opt/amnezia-service-traffic/collector.py collect amnezia-awg wg0 ; }"
        )]
        with patch.object(existing, "run", create=True, return_value="LoadState=not-found"):
            self.service.runner_factory = Mock(return_value=install_runner)
            self.assertEqual(await self.service.start_service_traffic_install(sid), {"state": "installing"})
            job = self.service.traffic_installs[sid]
            self.assertEqual(await self.service.start_service_traffic_install(sid), {"state": "installing"})
            self.assertIs(self.service.traffic_installs[sid], job)
            await job
            self.assertIn("error", await self.service.service_traffic_control(sid))
            await self.service.start_service_traffic_install(sid)
            await self.service.traffic_installs[sid]
            self.assertNotIn("error", await self.service.service_traffic_control(sid))
            command, payload = install_runner.run.call_args_list[1].args
            self.assertTrue(command.endswith("install-traffic amnezia-awg wg0"))
            for name, line in zip(("install-service-traffic.sh", "service_traffic.py"), payload.splitlines(), strict=True):
                source = Path(__file__).resolve().parents[1] / "scripts" / name
                self.assertEqual(base64.b64decode(line), source.read_bytes())
            self.assertTrue(all("--now" not in call.args[0] for call in install_runner.run.call_args_list))

    async def test_traffic_install_can_enable_collection_after_installation(self):
        sid = self.server.id
        existing = self.service.runner(sid)
        runner = Mock()
        disabled = ("LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\n"
                    "ExecStart={ argv[]=/opt/amnezia-service-traffic/venv/bin/python "
                    "/opt/amnezia-service-traffic/collector.py collect amnezia-awg wg0 ; }")
        enabled = disabled.replace("=disabled", "=enabled").replace("=inactive", "=active")
        runner.run.side_effect = ["", disabled, "", "", enabled]
        self.service.runner_factory = Mock(return_value=runner)
        with patch.object(existing, "run", create=True, return_value="LoadState=not-found"):
            await self.service.start_service_traffic_install(sid, enable=True)
            await self.service.traffic_installs[sid]
        operations = [call.args[0] for call in runner.run.call_args_list]
        self.assertIn('install-service-traffic.sh', operations[0])
        self.assertEqual(operations[3], "systemctl --no-ask-password enable --now amnezia-service-traffic.service")
        self.assertNotIn(sid, self.service.traffic_install_errors)

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

    async def test_pause_reserves_address_and_resumes_same_client_after_restart(self):
        sid = self.server.id
        client = await self.service.create_client(sid, 'phone', None, None)
        await self.service.set_paused(sid, client['id'], True)
        self.assertNotIn(client['public_key'], json.loads((self.vpn / 'runtime.json').read_text())['peers'])
        self.assertNotIn(client['public_key'], (self.vpn / 'wg0.conf').read_text())
        await self.service.close()
        self.service = VPNService(self.store, FakeRunner)
        self.assertEqual(self.store.client(sid, client['id'])['config'], client['config'])
        other = await self.service.create_client(sid, 'tablet', None, None)
        self.assertNotEqual(other['allowed_ips'], client['allowed_ips'])
        await self.service.set_paused(sid, client['id'], False)
        self.assertEqual(self.store.client(sid, client['id'])['config'], client['config'])
        self.assertIn(client['public_key'], json.loads((self.vpn / 'runtime.json').read_text())['peers'])

    async def test_restore_preview_checks_revision_and_restores_only_missing_client(self):
        sid = self.server.id
        client = await self.service.create_client(sid, 'phone', None, None)
        archive = await self.service.backup(sid)
        await self.service.delete_client(sid, client['id'])
        preview = await self.service.restore_backup(sid, archive)
        self.assertEqual(preview['add'], ['phone'])
        self.assertEqual(len(self.store.clients(sid)), 2)
        with self.assertRaisesRegex(ManagerError, 'изменилась'):
            await self.service.restore_backup(sid, archive, 'wrong-revision')
        await self.service.restore_backup(sid, archive, preview['revision'])
        self.assertEqual(len(self.store.clients(sid)), 3)
        self.assertEqual(self.store.client(sid, client['id'])['config'], client['config'])

    async def test_bad_client_table_blocks_mutation(self):
        (self.vpn / "clientsTable").write_text("{broken")
        with self.assertRaises(ManagerError):
            await self.service.create_client(self.server.id,"new",None,None)
        self.assertEqual(len(parse_config((self.vpn / "wg0.conf").read_text()).peers),2)

    async def test_reissue_preserves_identity_policy_history_and_other_clients(self):
        created = await self.service.create_client(self.server.id, 'phone', 3600, 1000000)
        sid, cid, old_key = self.server.id, created['id'], created['public_key']
        with self.store.transaction() as conn:
            conn.execute('UPDATE clients SET total_rx=12345,total_tx=67890 WHERE id=?',(cid,))
            conn.execute('UPDATE monthly SET rx=123,tx=456 WHERE client_id=?',(cid,))
        monthly = [tuple(r) for r in self.store.connection.execute('SELECT * FROM monthly WHERE client_id=?',(cid,))]
        before = self.store.client(sid,cid)
        other = json.loads((self.vpn/'runtime.json').read_text())['peers'];other.pop(old_key)
        result = await self.service.reissue_client(sid,cid,old_key)
        self.assertNotEqual(result['public_key'],old_key)
        self.assertNotEqual(result['config'],created['config'])
        for field in ('id','name','allowed_ips','expires','traffic_limit','total_rx','total_tx'):
            self.assertEqual(result[field],before[field],field)
        self.assertEqual(monthly,[tuple(r) for r in self.store.connection.execute('SELECT * FROM monthly WHERE client_id=?',(cid,))])
        runtime=json.loads((self.vpn/'runtime.json').read_text())
        self.assertNotIn(old_key,runtime['peers'])
        new_peer=runtime['peers'].pop(result['public_key'])
        self.assertEqual(runtime['peers'],other)
        self.assertEqual(new_peer['ips'],created['allowed_ips'])
        self.assertEqual(self.store.client(sid,cid)['config'],result['config'])
        self.assertEqual(len(self.store.clients(sid)),3)
        table=json.loads((self.vpn/'clientsTable').read_text())
        self.assertEqual([v['userData']['clientName'] for v in table if v['clientId']==result['public_key']],['phone'])
        with self.assertRaises(ManagerError):await self.service.reissue_client(sid,cid,old_key)
        self.assertNotIn('down',(self.vpn/'commands').read_text())

    async def test_reissue_rollback_restores_old_peer_on_new_key_failure(self):
        created=await self.service.create_client(self.server.id,'phone',None,None)
        before=[(self.vpn/n).read_bytes() for n in ('wg0.conf','clientsTable')]
        (self.vpn/'fail_add_once').touch()
        with self.assertRaises(ManagerError):await self.service.reissue_client(self.server.id,created['id'],created['public_key'])
        self.assertEqual(before,[(self.vpn/n).read_bytes() for n in ('wg0.conf','clientsTable')])
        await self.service.snapshot(self.server.id,force=True)
        current=self.store.client(self.server.id,created['id'])
        self.assertEqual((current['public_key'],current['config']),(created['public_key'],created['config']))
        self.assertIn(created['public_key'],json.loads((self.vpn/'runtime.json').read_text())['peers'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM rotations').fetchone()[0],0)

    async def test_reissue_recovers_after_remote_success_before_database_commit(self):
        created=await self.service.create_client(self.server.id,'phone',3600,1000000)
        original=self.store.reconcile_rotations
        def interrupt(sid,keys):
            if self.store.connection.execute('SELECT count(*) FROM rotations').fetchone()[0]:raise RuntimeError('simulated process interruption')
            return original(sid,keys)
        self.store.reconcile_rotations=interrupt
        with self.assertRaises(RuntimeError):await self.service.reissue_client(self.server.id,created['id'],created['public_key'])
        self.store.reconcile_rotations=original
        await self.service.snapshot(self.server.id,force=True)
        result=self.store.client(self.server.id,created['id'])
        self.assertNotEqual(result['public_key'],created['public_key'])
        self.assertIn('PrivateKey =',result['config'])
        self.assertEqual(len(self.store.clients(self.server.id)),3)
        self.assertEqual(result['allowed_ips'],created['allowed_ips'])

    async def test_reissue_accumulates_new_epoch_traffic_without_resetting_old_totals(self):
        created=await self.service.create_client(self.server.id,'phone',None,None)
        with self.store.transaction() as conn:conn.execute('UPDATE clients SET total_rx=1000,total_tx=2000 WHERE id=?',(created['id'],))
        result=await self.service.reissue_client(self.server.id,created['id'],created['public_key'])
        state=json.loads((self.vpn/'runtime.json').read_text());peer=state['peers'][result['public_key']];peer['rx']=50;peer['tx']=80
        (self.vpn/'runtime.json').write_text(json.dumps(state))
        await self.service.snapshot(self.server.id,force=True)
        result=self.store.client(self.server.id,created['id'])
        self.assertEqual((result['total_rx'],result['total_tx']),(1050,2080))

