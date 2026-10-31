import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from manager.domain import ManagerError, Server
from manager.speedtest import Speedtests, parse_result, PROBE, INSTALL
from manager.store import Store

class ResultTests(unittest.TestCase):
    def test_python_bits_and_ookla_bytes(self):
        p=parse_result(json.dumps({'download':100000000,'upload':20000000,'ping':12,'server':{'name':'test'}}),'python')
        o=parse_result(json.dumps({'download':{'bandwidth':12500000},'upload':{'bandwidth':2500000},'ping':{'latency':12}}),'ookla')
        self.assertEqual((p['download'],p['upload'],p['ping']),(100,20,12))
        self.assertEqual((o['download'],o['upload'],o['ping']),(100,20,12))
    def test_invalid_and_nonfinite_rejected(self):
        for raw in ['{}','not json','{"download":NaN,"upload":3,"ping":4}','{"download":-1,"upload":3,"ping":4}']:
            with self.assertRaises(ManagerError):parse_result(raw,'python')

class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(Path(self.tmp.name));self.server=self.store.add_server(Server(0,'test',host='host'))
        self.calls=[]
        outer=self
        class Runner:
            def __init__(self,*args):pass
            def run(self,command,**kwargs):
                outer.calls.append((command,kwargs))
                return 'python' if command==PROBE else json.dumps({'download':100000000,'upload':20000000,'ping':12})
            def close(self):pass
        async def worker(operation):return operation()
        self.jobs=Speedtests(self.store,SimpleNamespace(runner_factory=Runner,worker=worker))
    async def asyncTearDown(self):
        await self.jobs.close();self.store.close();self.tmp.cleanup()
    async def test_check_then_run_retains_result_and_cooldown(self):
        self.jobs.start(self.server.id,'check');await self.jobs.tasks[self.server.id]
        self.assertEqual(self.jobs.status(self.server.id)['tool'],'python')
        self.jobs.start(self.server.id,'run');await self.jobs.tasks[self.server.id]
        self.assertEqual(self.jobs.status(self.server.id)['result']['download'],100)
        self.assertEqual(self.calls[-1][1]['timeout'],90)
        with self.assertRaises(ManagerError):self.jobs.start(self.server.id,'run')
    async def test_coalesces_parallel_clicks_and_no_implicit_install(self):
        self.jobs.start(self.server.id,'check');self.jobs.start(self.server.id,'check')
        await self.jobs.tasks[self.server.id]
        self.assertEqual(len(self.calls),1)
        self.assertNotIn(INSTALL,[c[0] for c in self.calls])
    async def test_missing_server_and_unknown_actions(self):
        with self.assertRaises(ManagerError):self.jobs.start(999,'check')
        with self.assertRaises(ManagerError):self.jobs.start(self.server.id,'invalid')
