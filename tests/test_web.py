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
from manager.web import MiniApp, create_web_app, validate_init_data


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
        self.bot=SimpleNamespace(send_document=AsyncMock())
        self.client=TestClient(TestServer(create_web_app(SimpleNamespace(token='test-token',admin_id=123),self.store,self.service,self.bot)))
        await self.client.start_server();self.headers={'Authorization':'tma '+signed()}
    async def asyncTearDown(self):
        await self.client.close();self.store.close();self.tmp.cleanup()
    async def test_service_traffic_control_is_owner_only_and_validates_boolean(self):
        self.store.set_pref("telegram_admins", [{"id": 124, "name": "admin"}])
        sid = self.server.id
        url = f"/api/servers/{sid}/service-traffic/control"
        status = {"state": "ready", "enabled": False, "running": False}
        self.service.service_traffic_control = AsyncMock(return_value=status)
        admin = {"Authorization": "tma " + signed(uid=124)}
        response = await self.client.get(url, headers=admin)
        self.assertEqual(response.status, 403)
        response = await self.client.patch(url, headers=admin, json={"enabled": False})
        self.assertEqual(response.status, 403)
        response = await self.client.patch(url, headers=self.headers, json={"enabled": "false"})
        self.assertEqual(response.status, 400)
        self.service.service_traffic_control.assert_not_awaited()
        response = await self.client.patch(url, headers=self.headers, json={"enabled": False})
        self.assertEqual(await response.json(), status)
        self.service.service_traffic_control.assert_awaited_once_with(sid, False)

    async def test_service_traffic_requires_owner_and_scopes_client(self):
        self.store.set_pref('telegram_admins', [{'id': 124, 'name': 'admin'}])
        cid = self.store.clients(self.server.id)[0]['id']
        url = f'/api/servers/{self.server.id}/clients/{cid}/service-traffic'
        self.service.service_traffic = AsyncMock(return_value={'state': 'disabled', 'rows': []})
        response = await self.client.get(url, headers={'Authorization': 'tma '+signed(uid=124)})
        self.assertEqual(response.status, 403)
        response = await self.client.get(url+'?window=year', headers=self.headers)
        self.assertEqual(response.status, 400)
        self.service.service_traffic.assert_not_awaited()
        response = await self.client.get(url+'?window=week', headers=self.headers)
        self.assertEqual(await response.json(), {'state': 'disabled', 'rows': []})
        self.service.service_traffic.assert_awaited_once_with(self.server.id, 'week', 'public')

    async def test_overview_retains_connection_history_without_claiming_online(self):
        stamp = int(time.time()) - 3600
        self.store.sync(self.server.id, [Peer('public','client','10.0.0.2/32','',handshake=stamp)], 'server')
        self.store.sync(self.server.id, [Peer('public','client','10.0.0.2/32','',handshake=0)], 'server')
        response = await self.client.get('/api/overview', headers=self.headers)
        client = (await response.json())['servers'][0]['clients'][0]
        self.assertEqual(client['handshake'], 0)
        self.assertEqual(client['last_handshake'], stamp)
        self.assertTrue(client['has_history'])
        self.assertFalse(client['online'])

    async def test_new_client_has_no_invented_connection_history(self):
        self.store.sync(self.server.id, [Peer('new','new','10.0.0.3/32','')], 'server')
        response = await self.client.get('/api/overview', headers=self.headers)
        client = (await response.json())['servers'][0]['clients'][0]
        self.assertFalse(client['has_history'])
        self.assertEqual(client['last_handshake'], 0)

    async def test_backup_schedule_sends_once_when_due_and_can_be_disabled(self):
        path = '/api/backup-schedule'
        self.store.set_pref('telegram_admins', [{'id': 124, 'name': 'admin'}])
        data = {'hours': 3, 'server_id': self.server.id}
        response = await self.client.patch(path, json=data, headers={'Authorization': 'tma '+signed(uid=124)})
        self.assertEqual(response.status, 403)
        response = await self.client.patch(path, json={**data, 'hours': 4}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.patch(path, json=data, headers=self.headers)
        self.assertEqual(response.status, 200)
        schedule = (await response.json())['schedule']
        self.assertGreater(schedule['next_at'], time.time())
        self.service.backup = AsyncMock(return_value=b'private archive')
        worker = MiniApp(SimpleNamespace(admin_id=123), self.store, self.service, self.bot)
        await worker.run_scheduled_backup()
        self.bot.send_document.assert_not_awaited()
        self.store.set_pref('backup_schedule', {**schedule, 'next_at': 0})
        await worker.run_scheduled_backup()
        await worker.run_scheduled_backup()
        self.service.backup.assert_awaited_once_with(self.server.id)
        self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.bot.send_document.await_args.args[0], 123)
        response = await self.client.patch(path, json={'hours': None}, headers=self.headers)
        result = await response.json()
        self.assertIsNone(result['schedule'])
        self.assertEqual(result['history'][0]['status'], 'sent')

    async def test_restore_requires_owner_and_a_single_use_preview(self):
        self.store.set_pref('telegram_admins', [{'id': 124, 'name': 'admin'}])
        admin = {'Authorization': 'tma '+signed(uid=124)}
        self.service.restore_backup = AsyncMock(side_effect=[
            {'add': ['phone'], 'conflicts': [], 'revision': 'before'},
            {'add': ['phone'], 'conflicts': []},
        ])
        url = f'/api/servers/{self.server.id}/restore'
        archive = b'fixture archive'
        self.assertEqual((await self.client.post(url+'/preview', data=archive, headers=admin)).status, 403)
        response = await self.client.post(url+'/preview', data=archive, headers=self.headers)
        preview = await response.json()
        self.assertEqual(preview['add'], ['phone'])
        self.assertNotIn('revision', preview)
        confirm = {'token': preview['token']}
        self.assertEqual((await self.client.post(url, json=confirm, headers=admin)).status, 403)
        response = await self.client.post(url, json=confirm, headers=self.headers)
        self.assertEqual(await response.json(), {'restored': 1})
        self.service.restore_backup.assert_awaited_with(self.server.id, archive, 'before')
        self.assertEqual((await self.client.post(url, json=confirm, headers=self.headers)).status, 400)

    async def test_resource_alerts_wait_for_cpu_and_deduplicate_until_recovery(self):
        self.bot.send_message = AsyncMock()
        worker = MiniApp(SimpleNamespace(admin_id=123), self.store, self.service, self.bot)
        self.store.set_pref('resource_alerts', {'disk': 90, 'cpu': 80})
        sample = {'stale': False, 'cpu': 95, 'disk': {'used': 93, 'total': 100}}
        worker.resources.get = lambda sid: sample
        await worker.check_resource_alerts()
        self.assertEqual(self.bot.send_message.await_count, 1)
        worker.resource_since[f'{self.server.id}:cpu'] -= 181
        await worker.check_resource_alerts()
        await worker.check_resource_alerts()
        self.assertEqual(self.bot.send_message.await_count, 2)
        sample.update(stale=True)
        await worker.check_resource_alerts()
        sample.update(stale=False, cpu=30, disk={'used': 50, 'total': 100})
        await worker.check_resource_alerts()
        self.assertEqual(self.bot.send_message.await_count, 4)
        self.assertEqual(self.store.get_pref('resource_alert_state'), {})

    async def test_backup_sends_archive_only_to_owner(self):
        self.service.backup=AsyncMock(return_value=b'private archive')
        self.store.set_pref('telegram_admins',[{'id':124,'name':'admin'}])
        path=f'/api/servers/{self.server.id}/backup'
        response=await self.client.post(path,headers={'Authorization':'tma '+signed(uid=124)})
        self.assertEqual(response.status,403)
        self.service.backup.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()
        response=await self.client.post(path,headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual(await response.json(),{'sent':True})
        self.service.backup.assert_awaited_once_with(self.server.id)
        self.bot.send_document.assert_awaited_once()
        recipient, document=self.bot.send_document.await_args.args
        self.assertEqual(recipient,123)
        self.assertEqual(document.data,b'private archive')

    async def test_updates_owner_only(self):
        self.store.set_pref('telegram_admins',[{'id':124,'name':'admin'}])
        for path in ['/api/updates','/api/updates/check','/api/updates/prepare','/api/updates/start']:
            method=self.client.get if path=='/api/updates' else self.client.post
            response=await method(path,headers={'Authorization':'tma '+signed(uid=124)})
            self.assertEqual(response.status,403)
        from unittest.mock import patch
        with patch('manager.updates.fetch_releases',return_value=[]):
            response=await self.client.get('/api/updates',headers=self.headers)
            self.assertEqual(response.status,200);self.assertIsNone((await response.json())['release'])
        response=await self.client.patch('/api/updates/preferences',headers=self.headers,json={'enabled':False})
        self.assertFalse((await response.json())['enabled'])
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

    async def test_admin_scope_filters_overview_and_blocks_direct_mutations(self):
        other = self.store.add_server(Server(0, 'Other', host='other'))
        await self.client.post('/api/admins', headers=self.headers, json={'id': 456, 'name': 'Друг'})
        admin = {'Authorization': 'tma ' + signed(uid=456)}
        scope = {'servers': [self.server.id], 'operations': []}
        self.assertEqual((await self.client.patch('/api/admins/456', headers=admin, json=scope)).status, 403)
        self.assertEqual((await self.client.patch('/api/admins/456', headers=self.headers, json=scope)).status, 200)
        response = await self.client.get('/api/overview', headers=admin)
        self.assertEqual([s['id'] for s in (await response.json())['servers']], [self.server.id])
        self.assertEqual((await self.client.get(f'/api/servers/{other.id}/monthly', headers=admin)).status, 403)
        cid = self.store.clients(self.server.id)[0]['id']
        base = f'/api/servers/{self.server.id}/clients/{cid}'
        self.assertEqual((await self.client.delete(base, headers=admin)).status, 403)
        self.assertEqual((await self.client.patch(base+'/pause', headers=admin, json={'paused': True})).status, 403)
        self.service.delete_client.assert_not_awaited()

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
