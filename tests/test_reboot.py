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
