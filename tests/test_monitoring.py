import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from manager.domain import Server
from manager.store import Store
from manager.monitoring import notify_health
from manager.web import MiniApp
from manager.resources import measure

class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(Path(self.tmp.name))
        self.a=self.store.add_server(Server(0,'a',host='a'));self.b=self.store.add_server(Server(0,'b',host='b'))
    async def asyncTearDown(self):self.store.close();self.tmp.cleanup()
    async def test_transitions_survive_shared_reads_without_duplicates(self):
        bot=SimpleNamespace(send_message=AsyncMock())
        for ok in [True,False,False,True,True]:
            self.store.set_pref(f'health:{self.a.id}',{'ok':ok});await notify_health(self.store,bot,123)
        self.assertEqual(bot.send_message.await_count,2)
        self.assertIn('Нет связи',bot.send_message.call_args_list[0].args[1]);self.assertIn('восстановлена',bot.send_message.call_args_list[1].args[1])
    async def test_failed_delivery_retries_without_blocking_other_hosts(self):
        for s in [self.a,self.b]:self.store.set_pref(f'health:{s.id}',{'ok':False})
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError(),None,None]))
        await notify_health(self.store,bot,123)
        self.assertIsNone(self.store.get_pref(f'health_notified:{self.a.id}'))
        self.assertIs(self.store.get_pref(f'health_notified:{self.b.id}'),False)
        await notify_health(self.store,bot,123);self.assertEqual(bot.send_message.await_count,3)
    async def test_slow_host_does_not_block_overview_or_healthy_poll(self):
        blocked=asyncio.Event();started=asyncio.Event();calls={self.a.id:0,self.b.id:0}
        async def snapshot(sid,force=False):
            calls[sid]+=1
            if sid==self.a.id:started.set();await blocked.wait()
            self.store.set_pref(f'health:{sid}',{'ok':True,'updated':time.time()})
        api=MiniApp(SimpleNamespace(admin_id=123),self.store,SimpleNamespace(snapshot=snapshot))
        api.hoststats.refresh=AsyncMock();api.resources.refresh=AsyncMock()
        try:
            self.assertEqual((await asyncio.wait_for(api.overview({'uid':123}),.5)).status,200)
            await asyncio.wait_for(started.wait(),.5);await asyncio.sleep(0)
            api.last_refresh=0
            await asyncio.wait_for(api.overview({'uid':123}),.5);await asyncio.sleep(0)
            self.assertEqual(calls,{self.a.id:1,self.b.id:2})
        finally:
            for task in api.poll_tasks.values():task.cancel()
            await asyncio.gather(*api.poll_tasks.values(),return_exceptions=True)
    def test_cpu_pause_requires_new_baseline(self):
        old=dict(boot='a',updated=100,cpu_total=1000,cpu_idle=500,memory_used=40,memory_total=100,disk_used=40,disk_total=100)
        resumed={**old,'updated':3700,'cpu_total':2000,'cpu_idle':1000}
        self.assertIsNone(measure(resumed,old)['cpu'])
        result=measure({**resumed,'updated':3705,'cpu_total':2100,'cpu_idle':1025},resumed)
        self.assertEqual(result['cpu'],75);self.assertEqual(result['interval'],5)
