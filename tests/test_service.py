import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
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

    READY = ("LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
             "ExecStart={ argv[]=/opt/amnezia-service-traffic/venv/bin/python "
             "/opt/amnezia-service-traffic/collector.py collect amnezia-awg wg0 ; }")

    async def test_outdated_collector_is_replaced_once_per_start(self):
        sid = self.server.id
        source = (Path(__file__).resolve().parents[1] / "scripts" / "service_traffic.py").read_bytes()
        with patch.object(self.service.runner(sid), "run", create=True,
                          side_effect=[self.READY, "0" * 64 + "  /opt/amnezia-service-traffic/collector.py\n", ""]) as run:
            self.assertTrue(await self.service.refresh_service_traffic(sid))
            self.assertFalse(await self.service.refresh_service_traffic(sid))
        command, payload = run.call_args_list[2].args
        self.assertIn("try-restart amnezia-service-traffic.service", command)
        self.assertEqual(base64.b64decode(payload), source)
        self.assertEqual(run.call_count, 3)

    async def test_current_foreign_or_missing_collector_is_left_alone(self):
        sid = self.server.id
        source = (Path(__file__).resolve().parents[1] / "scripts" / "service_traffic.py").read_bytes()
        current = hashlib.sha256(source).hexdigest() + "  /opt/amnezia-service-traffic/collector.py\n"
        for outputs in ([self.READY, current], [self.READY.replace("wg0", "wg1")], ["LoadState=not-found"]):
            self.service.traffic_refreshed.clear()
            with patch.object(self.service.runner(sid), "run", create=True, side_effect=outputs) as run:
                self.assertFalse(await self.service.refresh_service_traffic(sid))
            self.assertEqual(run.call_count, len(outputs))
            self.assertIn(sid, self.service.traffic_refreshed)

    async def test_unreachable_server_is_checked_again_later(self):
        sid = self.server.id
        with patch.object(self.service.runner(sid), "run", create=True, side_effect=ManagerError("network")):
            with self.assertRaises(ManagerError):
                await self.service.refresh_service_traffic(sid)
        self.assertNotIn(sid, self.service.traffic_refreshed)

    def test_update_script_replaces_only_with_a_file_that_compiles(self):
        from manager.service import TRAFFIC_COLLECTOR, TRAFFIC_PYTHON, TRAFFIC_UPDATE_COMMAND
        root = Path(self.tmp.name) / "vps"
        (root / "bin").mkdir(parents=True)
        collector = root / "collector.py"
        collector.write_text("OLD = 1\n")
        systemctl = root / "bin" / "systemctl"
        systemctl.write_text(f'#!/bin/sh\necho "$@" >> {root}/systemctl.log\n')
        systemctl.chmod(0o755)
        script = TRAFFIC_UPDATE_COMMAND.replace(TRAFFIC_COLLECTOR, str(collector)).replace(TRAFFIC_PYTHON, sys.executable)
        env = {**os.environ, "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}"}
        def apply(text):
            return subprocess.run(["sh", "-c", script], input=base64.b64encode(text.encode()) + b"\n", env=env, capture_output=True)
        self.assertNotEqual(apply("def broken(:\n").returncode, 0)
        self.assertEqual(collector.read_text(), "OLD = 1\n")
        self.assertFalse((root / "collector.py.new").exists())
        self.assertFalse((root / "systemctl.log").exists())
        self.assertEqual(apply("NEW = 2\n").returncode, 0)
        self.assertEqual(collector.read_text(), "NEW = 2\n")
        self.assertEqual(oct(collector.stat().st_mode & 0o777), "0o644")
        self.assertFalse((root / "collector.py.new").exists())
        self.assertEqual((root / "systemctl.log").read_text(), "--no-ask-password try-restart amnezia-service-traffic.service\n")

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

    async def test_peer_changes_keep_obfuscation_parameters_on_the_running_interface(self):
        # AmneziaWG drops Jc/S*/H* on every runtime peer change; clients then cannot handshake.
        expected = {"jc": "4", "s3": "25", "i1": "<b 0x00>"}
        runtime = lambda: json.loads((self.vpn / "runtime.json").read_text())["params"]
        self.assertEqual(runtime(), expected)
        sid = self.server.id
        created = await self.service.create_client(sid, "phone", None, None)
        self.assertEqual(runtime(), expected)
        reissued = await self.service.reissue_client(sid, created["id"], created["public_key"])
        self.assertEqual(runtime(), expected)
        await self.service.set_paused(sid, reissued["id"], True)
        self.assertEqual(runtime(), expected)
        await self.service.set_paused(sid, reissued["id"], False)
        self.assertEqual(runtime(), expected)
        await self.service.delete_client(sid, reissued["id"])
        self.assertEqual(runtime(), expected)

    async def test_rename_changes_only_the_client_table(self):
        sid = self.server.id
        created = await self.service.create_client(sid, "phone", None, None)
        config_before = (self.vpn / "wg0.conf").read_text()
        runtime_before = (self.vpn / "runtime.json").read_text()
        commands_before = (self.vpn / "commands").read_text()
        renamed = await self.service.rename_client(sid, created["id"], "  Мамин телефон ")
        self.assertEqual(renamed["name"], "Мамин телефон")
        self.assertEqual(renamed["public_key"], created["public_key"])
        self.assertEqual(renamed["config"], created["config"])
        table = json.loads((self.vpn / "clientsTable").read_text())
        entry = next(e for e in table if e["clientId"] == created["public_key"])
        self.assertEqual(entry["userData"]["clientName"], "Мамин телефон")
        self.assertIn("creationDate", entry["userData"])
        self.assertEqual((self.vpn / "wg0.conf").read_text(), config_before)
        self.assertEqual((self.vpn / "runtime.json").read_text(), runtime_before)
        new_commands = (self.vpn / "commands").read_text()[len(commands_before):]
        self.assertFalse([line for line in new_commands.splitlines() if line.startswith(("set", "addconf"))])
        # Survives a fresh read of the server.
        await self.service.snapshot(sid, force=True)
        self.assertEqual(self.store.client(sid, created["id"])["name"], "Мамин телефон")

    async def test_rename_rejects_a_taken_name_and_bad_input(self):
        sid = self.server.id
        first = await self.service.create_client(sid, "phone", None, None)
        await self.service.create_client(sid, "tablet", None, None)
        table_before = (self.vpn / "clientsTable").read_text()
        with self.assertRaisesRegex(ManagerError, "уже есть"):
            await self.service.rename_client(sid, first["id"], "TABLET")
        with self.assertRaises(ManagerError):
            await self.service.rename_client(sid, first["id"], "a\nb")
        with self.assertRaises(ManagerError):
            await self.service.rename_client(sid, first["id"], "  ")
        self.assertEqual((self.vpn / "clientsTable").read_text(), table_before)
        # Same name in another case is just a rename of the same client.
        self.assertEqual((await self.service.rename_client(sid, first["id"], "Phone"))["name"], "Phone")

    async def test_rename_of_a_paused_client_survives_resume(self):
        sid = self.server.id
        client = await self.service.create_client(sid, "phone", None, None)
        await self.service.set_paused(sid, client["id"], True)
        await self.service.rename_client(sid, client["id"], "old phone")
        self.assertEqual(self.store.client(sid, client["id"])["name"], "old phone")
        await self.service.set_paused(sid, client["id"], False)
        await self.service.snapshot(sid, force=True)
        self.assertEqual(self.store.client(sid, client["id"])["name"], "old phone")

    async def test_failed_reissue_rollback_also_keeps_obfuscation_parameters(self):
        expected = {"jc": "4", "s3": "25", "i1": "<b 0x00>"}
        created = await self.service.create_client(self.server.id, "phone", None, None)
        (self.vpn / "fail_add_once").write_text("")
        with self.assertRaises(ManagerError):
            await self.service.reissue_client(self.server.id, created["id"], created["public_key"])
        self.assertEqual(json.loads((self.vpn / "runtime.json").read_text())["params"], expected)

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

