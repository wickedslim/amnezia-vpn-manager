import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from manager.resources import Resources, measure


def row(total=1000,idle=700,boot='a'):
    return dict(boot=boot,updated=100+(total-1000)//100,cpu_total=total,cpu_idle=idle,
                memory_total=1000,memory_used=400,disk_total=10000,disk_used=2000)


class ResourceTests(unittest.TestCase):
    def test_cpu_uses_delta_not_uptime_average_and_waits_for_second_sample(self):
        self.assertIsNone(measure(row())['cpu'])
        self.assertEqual(measure(row(1200,750),row())['cpu'],75)
        self.assertEqual(measure(row(1200,900),row())['cpu'],0)
        self.assertEqual(measure(row(1200,700),row())['cpu'],100)
    def test_reboot_reset_and_impossible_delta_need_new_baseline(self):
        for current in (row(1200,750,'b'),row(500,300),row(),row(1100,900)):
            self.assertIsNone(measure(current,row())['cpu'])
    def test_capacity_and_counter_validation(self):
        for key,value in [('memory_total',0),('memory_used',1001),('disk_used',-1),('cpu_total',True),('disk_total',float('nan')),('cpu_idle',1001)]:
            with self.subTest(key=key),self.assertRaises(ValueError):measure({**row(),key:value})
        result=measure(row())
        self.assertEqual(result['memory'],dict(used=400,total=1000))
        self.assertNotIn('boot',result)


class ResourceRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_cache_failure_and_recovery(self):
        class Runner:
            calls=0;fail=False
            def run(self,command,timeout):
                self.calls+=1
                if self.fail:raise RuntimeError('offline')
                return json.dumps(row(1000+self.calls*100,700+self.calls*50))
        runner=Runner()
        async def worker(fn):return fn()
        service=SimpleNamespace(lock=lambda sid:asyncio.Lock(),runner=lambda sid:runner,worker=worker)
        resources=Resources(SimpleNamespace(servers=lambda:[SimpleNamespace(id=1)]),service)
        with patch('manager.resources.time.time',return_value=100):
            await asyncio.gather(resources.refresh(),resources.refresh())
            self.assertEqual(runner.calls,1)
            self.assertIsNone(resources.get(1)['cpu'])
            self.assertFalse(resources.get(1)['stale'])
            resources.last-=6;runner.fail=True
            await resources.refresh()
            self.assertTrue(resources.get(1)['stale'])
            self.assertEqual(resources.get(1)['memory']['used'],400)
            runner.fail=False;resources.last-=6
            await resources.refresh()
            self.assertEqual(resources.get(1)['cpu'],50)
            self.assertFalse(resources.get(1)['stale'])
        with patch('manager.resources.time.time',return_value=130):self.assertTrue(resources.get(1)['stale'])
        self.assertIsNone(resources.get(2))
