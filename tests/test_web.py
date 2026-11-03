import hashlib
import hmac
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from manager.domain import Peer, Server
from manager.store import Store
from manager.web import create_web_app, validate_init_data


def signed(token='test-token',uid=123,age=0,extra=None):
    data={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':uid})}
    data.update(extra or {})
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)

class AuthTests(unittest.TestCase):
    def test_valid(self):self.assertEqual(validate_init_data(signed(),'test-token',123),123)
    def test_reject_tampering_expiry_wrong_admin_and_duplicates(self):
        for raw in [signed()+'x',signed(age=3601),signed(uid=124),signed(age=-60),signed()+'&user=other','',signed(token='wrong')]:
            with self.subTest(raw=raw),self.assertRaises(web.HTTPUnauthorized):validate_init_data(raw,'test-token',123)
    def test_signed_extra_fields_included(self):
        self.assertEqual(validate_init_data(signed(extra={'signature':'example'}),'test-token',123),123)

class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(Path(self.tmp.name));self.server=self.store.add_server(Server(0,'test',host='private-host',password='secret-password'))
        self.store.sync(self.server.id,[Peer('public','client','10.0.0.2/32','',received=100,sent=200)],'server')
        self.store.set_pref(f'health:{self.server.id}',{'ok':True,'updated':time.time()})
        self.service=SimpleNamespace(overview=AsyncMock(),snapshot=AsyncMock(),create_client=AsyncMock(),delete_client=AsyncMock())
        self.client=TestClient(TestServer(create_web_app(SimpleNamespace(token='test-token',admin_id=123),self.store,self.service)))
        await self.client.start_server();self.headers={'Authorization':'tma '+signed()}
    async def asyncTearDown(self):
        await self.client.close();self.store.close();self.tmp.cleanup()
    async def test_no_api_without_auth(self):
        for path in ['/api/overview',f'/api/servers/{self.server.id}/clients/1/config']:
            response=await self.client.get(path);self.assertEqual(response.status,401)
    async def test_overview_redacts_credentials_keys_configs_and_coalesces_reads(self):
        for _ in range(2):
            response=await self.client.get('/api/overview',headers=self.headers);self.assertEqual(response.status,200)
            text=await response.text();data=json.loads(text)
            self.assertNotIn('secret-password',text);self.assertNotIn('private-host',text);self.assertNotIn('public_key',text);self.assertNotIn('config',data['servers'][0]['clients'][0])
            self.assertEqual(data['servers'][0]['clients'][0]['rx'],200)
        self.service.snapshot.assert_awaited_once()
    async def test_server_directions_match_client_download(self):
        with self.store.transaction() as c:
            c.execute('DELETE FROM samples')
            for ts,rx,tx in [(100,10,100),(105,20,300)]:
                c.execute('INSERT INTO samples VALUES (?,?,?,?,?)',(self.server.id,ts,rx,tx,0))
        response=await self.client.get('/api/overview',headers=self.headers)
        s=(await response.json())['servers'][0]
        self.assertEqual((s['rx'],s['tx']),(300,20))
        self.assertEqual((s['rate_rx'],s['rate_tx']),(40,2))
        self.assertEqual(s['samples'][-1]['rx'],300)
    async def test_old_health_marks_client_unknown_and_hides_live_rates(self):
        self.store.set_pref(f'health:{self.server.id}',{'ok':True,'updated':time.time()-60})
        response=await self.client.get('/api/overview',headers=self.headers)
        server=(await response.json())['servers'][0]
        self.assertTrue(server['stale'])
        self.assertFalse(server['clients'][0]['status_known'])
        self.assertEqual((server['rate_rx'],server['rate_tx']),(0,0))
    async def test_speedtest_requires_auth(self):
        path=f'/api/servers/{self.server.id}/speedtest'
        self.assertEqual((await self.client.post(path,json={'action':'install'})).status,401)
        response=await self.client.get(path,headers=self.headers)
        self.assertEqual((await response.json())['phase'],'idle')
    async def test_invalid_report_does_not_leak_internal_error(self):
        response=await self.client.get(f'/api/servers/{self.server.id}/monthly?month=invalid',headers=self.headers)
        self.assertEqual(response.status,400)
    async def test_monthly_preserves_deleted_and_direction(self):
        month=self.store.connection.execute('select month from monthly').fetchone()[0]
        with self.store.transaction() as c:c.execute('update clients set present=0');c.execute('update monthly set rx=30,tx=70')
        response=await self.client.get(f'/api/servers/{self.server.id}/monthly?month={month}',headers=self.headers)
        row=(await response.json())['clients'][0]
        self.assertTrue(row['deleted']);self.assertEqual((row['rx'],row['tx']),(70,30))
    async def test_mutation_auth_and_validation(self):
        path=f'/api/servers/{self.server.id}/clients'
        response=await self.client.post(path,json={'name':'client'});self.assertEqual(response.status,401)
        response=await self.client.post(path,headers=self.headers,json={'name':'client','limit':-1});self.assertEqual(response.status,400)
        self.service.create_client.assert_not_awaited()
    async def test_static_and_traversal(self):
        response=await self.client.get('/');self.assertEqual(response.status,200)
        self.assertIn('Content-Security-Policy',response.headers)
        response=await self.client.get('/assets/setting.ini');self.assertEqual(response.status,404)

    async def test_reissue_auth_confirmation_single_use_and_download(self):
        self.service.reissue_client=AsyncMock(return_value={'config':'NEW CONFIG','name':'client'})
        base=f'/api/servers/{self.server.id}/clients/1/reissue'
        response=await self.client.post(base+'/prepare',json={})
        self.assertEqual(response.status,401)
        response=await self.client.post(base,headers=self.headers,json={'nonce':'wrong'})
        self.assertEqual(response.status,400)
        self.service.reissue_client.assert_not_awaited()
        response=await self.client.post(base+'/prepare',headers=self.headers,json={})
        confirmation=await response.json()
        response=await self.client.post(base,headers=self.headers,json={'nonce':confirmation['nonce']})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['config'],'NEW CONFIG')
        self.service.reissue_client.assert_awaited_once_with(self.server.id,1,'public')
        response=await self.client.post(base,headers=self.headers,json={'nonce':confirmation['nonce']})
        self.assertEqual(response.status,400)
        self.assertEqual(self.service.reissue_client.await_count,1)

    async def test_preferences_authenticated_persisted_and_normalized(self):
        response=await self.client.post('/api/preferences',json={'palette':'pink'})
        self.assertEqual(response.status,401)
        response=await self.client.post('/api/preferences',headers=self.headers,json={'palette':'pink','fontSize':16,'motion':False})
        self.assertEqual(response.status,200)
        response=await self.client.get('/api/preferences',headers=self.headers)
        p=await response.json()
        self.assertEqual((p['palette'],p['fontSize'],p['motion']),('pink',16,False))

    async def test_reboot_api_rejects_unauthenticated_and_unconfirmed(self):
        import asyncio
        self.service.lock=lambda sid: asyncio.Lock()
        base=f'/api/servers/{self.server.id}/reboot'
        response=await self.client.post(base+'/prepare',json={})
        self.assertEqual(response.status,401)
        response=await self.client.post(base,headers=self.headers,json={'nonce':'invalid','name':'test'})
        self.assertEqual(response.status,400)


    async def test_admin_grant_revocation_and_owner_only_controls(self):
        response=await self.client.post('/api/admins',headers=self.headers,json={'id':456,'name':'Друг'})
        self.assertEqual(response.status,200)
        admin={'Authorization':'tma '+signed(uid=456)}
        response=await self.client.get('/api/overview',headers=admin)
        self.assertEqual(response.status,200)
        self.assertFalse((await response.json())['access']['owner'])
        for path,body in [('/api/admins',{'id':789,'name':'Other'}),('/api/servers',{'name':'blocked'})]:
            self.assertEqual((await self.client.post(path,headers=admin,json=body)).status,403)
        self.assertEqual((await self.client.get('/api/admins',headers=admin)).status,403)
        self.assertEqual((await self.client.delete('/api/admins/123',headers=self.headers)).status,400)
        self.assertEqual((await self.client.delete('/api/admins/456',headers=self.headers)).status,200)
        self.assertEqual((await self.client.get('/api/overview',headers=admin)).status,401)

    async def test_admin_preferences_and_confirmations_are_isolated(self):
        await self.client.post('/api/admins',headers=self.headers,json={'id':456,'name':'Друг'})
        admin={'Authorization':'tma '+signed(uid=456)}
        await self.client.post('/api/preferences',headers=admin,json={'mode':'dark'})
        self.assertEqual((await (await self.client.get('/api/preferences',headers=self.headers)).json())['mode'],'auto')
        self.assertEqual((await (await self.client.get('/api/preferences',headers=admin)).json())['mode'],'dark')
        c=self.store.clients(self.server.id)[0]
        url=f'/api/servers/{self.server.id}/clients/{c["id"]}/reissue'
        nonce=(await (await self.client.post(url+'/prepare',headers=self.headers,json={})).json())['nonce']
        self.assertEqual((await self.client.post(url,headers=admin,json={'nonce':nonce})).status,400)

    async def test_add_server_probes_before_save_and_redacts_credentials(self):
        self.service.probe=AsyncMock()
        data=dict(name='Второй',connection='root@203.0.113.5:2222',password='secret-new-password')
        response=await self.client.post('/api/servers',headers=self.headers,json=data)
        self.assertEqual(response.status,201)
        self.assertNotIn('secret-new-password',await response.text())
        self.service.probe.assert_awaited_once()
        server=self.store.server((await response.json())['id'])
        self.assertEqual((server.port,server.password),(2222,'secret-new-password'))
        self.service.probe.side_effect=RuntimeError('SSH failed')
        response=await self.client.post('/api/servers',headers=self.headers,json={**data,'name':'Broken','connection':'203.0.113.6'})
        self.assertEqual(response.status,500)
        self.assertEqual(len(self.store.servers()),2)

    async def test_daily_auth_directions_and_deleted_client(self):
        month=self.store.connection.execute('SELECT month FROM monthly LIMIT 1').fetchone()[0]
        cid=self.store.clients(self.server.id)[0]['id']
        path=f'/api/servers/{self.server.id}/clients/{cid}/daily?month={month}'
        self.assertEqual((await self.client.get(path)).status,401)
        with self.store.transaction() as c:
            c.execute('UPDATE daily SET rx=20,tx=80 WHERE client_id=?',(cid,))
            c.execute('UPDATE clients SET present=0 WHERE id=?',(cid,))
        response=await self.client.get(path,headers=self.headers)
        self.assertEqual(response.status,200)
        report=await response.json();self.assertTrue(report['deleted'])
        day=next(d for d in report['days'] if d['observed'])
        self.assertEqual((day['rx'],day['tx']),(80,20))
        response=await self.client.get(f'/api/servers/{self.server.id}/daily?month=bad',headers=self.headers)
        self.assertEqual(response.status,400)
