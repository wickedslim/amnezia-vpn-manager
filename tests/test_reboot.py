import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from manager.domain import ManagerError, Server
from manager.store import Store
from manager.reboot import Reboots, REBOOT_COMMAND


class RebootTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory(); self.store=Store(Path(self.tmp.name))
        self.server=self.store.add_server(Server(0,'demo',host='example.test'))
        self.runner=SimpleNamespace(run=Mock(return_value=''))
        async def worker(fn): return fn()
        self.service=SimpleNamespace(lock=lambda sid:asyncio.Lock(),worker=worker,runner=lambda sid:self.runner)
        self.reboots=Reboots(self.store,self.service)

    async def asyncTearDown(self):
        self.store.close();self.tmp.cleanup()

    async def test_confirmation_is_bound_single_use_and_cooldown(self):
        c=self.reboots.prepare(self.server.id)
        for nonce,name in [(None,'demo'),('wrong','demo'),(c['nonce'],'another')]:
            with self.assertRaises(ManagerError):await self.reboots.execute(self.server.id,nonce,name)
        self.runner.run.assert_not_called()
        await self.reboots.execute(self.server.id,c['nonce'],'demo')
        self.runner.run.assert_called_once_with(REBOOT_COMMAND,timeout=12)
        with self.assertRaises(ManagerError):await self.reboots.execute(self.server.id,c['nonce'],'demo')
        with self.assertRaises(ManagerError):self.reboots.prepare(self.server.id)

    async def test_expiry_and_changed_connection_rejected(self):
        c=self.reboots.prepare(self.server.id)
        token,_,server=self.reboots.confirmations[self.server.id]
        self.reboots.confirmations[self.server.id]=(token,time.monotonic()-1,server)
        with self.assertRaises(ManagerError):await self.reboots.execute(self.server.id,c['nonce'],'demo')
        c=self.reboots.prepare(self.server.id)
        from dataclasses import replace
        self.store.update_server(replace(self.server,host='other.test'))
        with self.assertRaises(ManagerError):await self.reboots.execute(self.server.id,c['nonce'],'demo')
        self.runner.run.assert_not_called()

    def run_command(self, uid):
        import os, subprocess
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir(exist_ok=True)
        log = Path(self.tmp.name) / 'calls'
        for name in ('systemctl', 'systemd-run', 'sudo'):
            (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n[ {name} != sudo ] || {{ shift; [ "$1" != -n ] || shift; exec "$@"; }}\n')
            (bin_dir / name).chmod(0o755)
        (bin_dir / 'id').write_text(f'#!/bin/sh\necho {uid}\n'); (bin_dir / 'id').chmod(0o755)
        result = subprocess.run(['sh', '-c', REBOOT_COMMAND], env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return log.read_text().splitlines()

    def test_command_tightens_timer_accuracy_and_resets_a_failed_unit_first(self):
        calls = self.run_command(0)
        self.assertEqual(calls[0], 'systemctl reset-failed awg-manager-reboot.service awg-manager-reboot.timer')
        self.assertIn('--on-active=10s', calls[1])
        self.assertIn('--timer-property=AccuracySec=1s', calls[1])
        self.assertTrue(calls[1].endswith('/usr/bin/systemctl reboot'))

    def test_command_uses_passwordless_sudo_for_non_root_users(self):
        calls = self.run_command(1000)
        self.assertEqual([c.split()[0] for c in calls], ['sudo', 'systemctl', 'sudo', 'systemd-run'])
        self.assertTrue(all(c.startswith('sudo -n') or c.split()[0] in ('systemctl', 'systemd-run') for c in calls))

