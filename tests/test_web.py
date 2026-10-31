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
        self.store.set_pref(f'health:{self.server.id}',{'ok':True})
        self.service=SimpleNamespace(overview=AsyncMock(),create_client=AsyncMock(),delete_client=AsyncMock())
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
        self.service.overview.assert_awaited_once()
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
