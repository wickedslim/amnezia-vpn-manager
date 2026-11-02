import asyncio,json,time,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient,TestServer
from manager import audit
from manager.domain import Server
from manager.store import Store
from manager.web import create_web_app
from test_web import signed

class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(Path(self.tmp.name))
    async def asyncTearDown(self):self.store.close();self.tmp.cleanup()
    async def test_concurrent_actor_contexts_and_error_redaction(self):
        class Operation:
            store=self.store
            @audit.audited('client.reissue')
            async def execute(self,sid,cid,secret):
                await asyncio.sleep(0)
                if secret:raise RuntimeError(secret)
        async def run(uid,secret):
            token=audit.begin(uid,'bot')
            try:
                try:await Operation().execute(7,8,secret)
                except RuntimeError:pass
            finally:audit.context.reset(token)
        await asyncio.gather(run(1,''),run(2,'PRIVATE_KEY_PASSWORD'))
        rows=self.store.audit_events();self.assertEqual({r['actor'] for r in rows},{1,2})
        self.assertEqual(len({r['request_id'] for r in rows}),2)
        self.assertNotIn('PRIVATE_KEY_PASSWORD',json.dumps(rows))
        self.assertEqual(next(r for r in rows if r['actor']==2)['error'],'RuntimeError')
    async def test_owner_only_api_and_safe_failed_request(self):
        server=self.store.add_server(Server(0,'test',host='secret-host',password='secret-password'))
        self.store.set_pref('telegram_admins',[{'id':456,'name':'admin'}])
        service=SimpleNamespace(snapshot=AsyncMock(),create_client=AsyncMock(side_effect=RuntimeError('secret-exception')))
        async with TestClient(TestServer(create_web_app(SimpleNamespace(token='test-token',admin_id=123),self.store,service))) as client:
            owner={'Authorization':'tma '+signed()};admin={'Authorization':'tma '+signed(uid=456)}
            self.assertEqual((await client.get('/api/audit')).status,401)
            self.assertEqual((await client.get('/api/audit',headers=admin)).status,403)
            response=await client.post(f'/api/servers/{server.id}/clients',headers=owner,json={'name':'name','password':'DO_NOT_LOG'})
            self.assertEqual(response.status,500);rid=response.headers['X-Request-ID']
            response=await client.get('/api/audit',headers=owner);data=await response.json()
            row=next(r for r in data['events'] if r['request_id']==rid)
            self.assertEqual((row['actor'],row['error'],row['result']),(123,'RuntimeError','failed'))
            text=json.dumps(data)
            for secret in ['secret-password','secret-host','secret-exception','DO_NOT_LOG','test-token']:self.assertNotIn(secret,text)
    def test_retention_and_cursor(self):
        for i in range(4):audit.record(self.store,'client.create','ok',1,i)
        rows=self.store.audit_events(limit=2);older=self.store.audit_events(before=rows[-1]['id'],limit=2)
        self.assertFalse(set(r['id'] for r in rows)&set(r['id'] for r in older))
        with self.store.transaction() as db:db.execute('UPDATE audit_events SET ts=?',(int(time.time())-91*86400,))
        audit.record(self.store,'client.create','ok')
        self.assertEqual(len(self.store.audit_events()),1)
    def test_deleted_server_history_survives(self):
        server=self.store.add_server(Server(0,'a',host='host'));audit.record(self.store,'server.disconnect','ok',server.id)
        self.store.remove_server(server.id)
        self.assertTrue(any(r['sid']==server.id for r in self.store.audit_events()))
