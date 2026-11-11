#!/usr/bin/env python3
"""Loopback-only, disposable Mini App demo. Never reads production settings."""
import argparse
import math
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'awg'))
from manager.domain import Peer, Server
from manager.store import Store
from manager.web import create_web_app


def create_demo():
    directory = tempfile.TemporaryDirectory(prefix='amnezia-miniapp-demo-')
    store = Store(Path(directory.name))
    for index,name in enumerate(('serv.host', 'Skystark_awg'),1):
        s = store.add_server(Server(0, name, host=f'192.0.2.{index}'))
        now = int(time.time())
        names = ['Admin · MacBook', 'Admin · iPhone', 'Kolida1', 'Kolida2', 'myRouter', 'office401', 'Склад', 'Дом', 'Длинное имя клиента для проверки переноса']
        peers = [Peer(f'demo-key-{i}', name, f'10.8.{s.id}.{i+2}/32', '', handshake=now-20 if i%3 else now-7200, received=(i+1)*144_000_000, sent=(i+1)*281_000_000) for i,name in enumerate(names[:9 if s.id==1 else 5])]
        store.sync(s.id, peers, 'demo-public')
        store.set_pref(f'hosttraffic:{s.id}',dict(rx=132_000_000_000,tx=167_000_000_000,since=now-11*86400,updated=now,interface='eth0',boot='demo',last_rx=132_000_000_000,last_tx=167_000_000_000))
        with store.transaction() as c:
            month = datetime.now(ZoneInfo('Europe/Samara')).strftime('%Y-%m')
            for row in store.clients(s.id):
                c.execute('UPDATE monthly SET rx=?,tx=? WHERE client_id=? AND month=?', (row['total_rx'],row['total_tx'],row['id'],month))
    today=datetime.now(ZoneInfo('Europe/Samara'))
    previous=today.replace(day=1)-timedelta(days=1)
    with store.transaction() as c:
        for client in c.execute('SELECT id FROM clients').fetchall():
            for month, count in [(previous,previous.day),(today,today.day)]:
                for day in range(1,count+1):
                    date=f"{month:%Y-%m}-{day:02d}"
                    c.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?,?)',(client['id'],date,(day*client['id']%7+1)*5000000,(day*client['id']%11+1)*50000000))
    store.set_pref('daily_since',datetime(previous.year,previous.month,1,tzinfo=ZoneInfo('Europe/Samara')).timestamp())
    class DemoRunner:
        installed=False
        def __init__(self,*args):pass
        def run(self,command,**kwargs):
            from manager.resources import READ as RESOURCE_READ
            if command==RESOURCE_READ:
                import json
                ticks=int(time.time()*100)
                return json.dumps(dict(boot="demo",updated=int(time.time()),cpu_total=ticks*4,cpu_idle=int(ticks*3.36),memory_total=2*1024**3,memory_used=int((.48+.02*math.sin(time.time()/15))*2*1024**3),disk_total=40*1024**3,disk_used=13*1024**3))
            from manager.speedtest import PROBE,INSTALL
            if command==PROBE:return 'python' if self.installed else 'missing'
            if command==INSTALL:DemoRunner.installed=True;return ''
            if command.startswith('speedtest-cli '):time.sleep(6)
            return '{"download":310000000,"upload":185000000,"ping":12.8,"server":{"name":"Демо-узел"}}'
        def close(self):pass
    class DemoService:
        runner_factory=DemoRunner
        def runner(self,sid):return DemoRunner()
        async def worker(self,operation):
            import asyncio
            return await asyncio.to_thread(operation)
        def lock(self, sid):
            import asyncio
            return asyncio.Lock()
        traffic_enabled = {1: True}

        async def start_service_traffic_install(self, sid, *, enable=False):
            self.traffic_enabled.setdefault(sid, False)
            if enable:
                self.traffic_enabled[sid] = True
            return await self.service_traffic_control(sid)

        async def service_traffic_control(self, sid, enabled=None):
            if sid not in self.traffic_enabled:
                return {"state": "missing"}
            if enabled is not None:
                self.traffic_enabled[sid] = enabled
            active = self.traffic_enabled[sid]
            return {"state": "ready", "enabled": active, "running": active}

        async def service_traffic(self, sid, window, public_key=""):
            factor = (1 if window == "day" else 7) * (1 if public_key else 4)
            now = int(time.time())
            rows = [
                ("youtube", 178_000_000, 7_000_000),
                ("telegram", 41_000_000, 18_000_000),
                ("instagram", 33_000_000, 4_000_000),
                ("tiktok", 28_000_000, 6_000_000),
                ("web", 26_000_000, 9_000_000),
                ("unknown", 18_000_000, 5_000_000),
                ("whatsapp", 9_000_000, 6_000_000),
                ("vk", 8_000_000, 2_000_000),
                ("discord", 3_000_000, 4_000_000),
                ("spotify", 7_000_000, 400_000),
                ("group_music", 6_000_000, 200_000),
                ("steam", 11_000_000, 600_000),
                ("group_updates", 2_000_000, 100_000),
            ]
            return {"state": "active", "started": now - 86400 * 8, "updated": now,
                    "rows": [{"category": name, "rx": rx * factor, "tx": tx * factor}
                             for name, rx, tx in rows]}

        async def snapshot(self, sid, force=False):
            await self.overview(force)
        async def overview(self, force=False):
            now=int(time.time())
            for s in store.servers():
                for c in store.clients(s.id):
                    if c['handshake'] > now-180:
                        with store.transaction() as db:
                            db.execute('UPDATE clients SET handshake=? WHERE id=?',(now-20,c['id']))
                samples=store.samples(s.id,1)
                first=samples[-1] if samples else {'rx':0,'tx':0,'ts':now-5}
                if len(store.samples(s.id,2))<2:
                    with store.transaction() as db:
                        for i in range(60):
                            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)',(s.id,now-(60-i)*5,first['rx']+i*700000,first['tx']+i*200000,4))
                samples=store.samples(s.id,1);first=samples[-1]
                dt=max(0,now-first['ts'])
                with store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)',(s.id,now,first['rx']+int(dt*(180000+60000*math.sin(now/10+s.id))),first['tx']+int(dt*(70000+30000*math.cos(now/8))),4))
                store.set_pref(f'health:{s.id}',{'ok':True,'updated':now})
            return {}
        async def probe(self,server):
            if not server.host.endswith('.example'):raise ValueError('Demo accepts .example hosts only')
        async def apply_speed(self,sid):return None
        async def create_client(self,sid,name,duration,limit,speed=None):
            from manager.domain import validate_name,ManagerError
            validate_name(name)
            if any(c['name']==name for c in store.clients(sid)):raise ManagerError('Клиент с таким именем уже есть.')
            existing=store.clients(sid)
            peers=[Peer(c['public_key'],c['name'],c['allowed_ips'],'',handshake=c['handshake'],received=c['last_rx'] or 0,sent=c['last_tx'] or 0) for c in existing]
            key=f'demo-new-{time.time_ns()}'
            peers.append(Peer(key,name,f'10.8.{sid}.{len(existing)+20}/32',''))
            store.sync(sid,peers,'demo-public')
            c=next(c for c in store.clients(sid) if c['public_key']==key)
            store.set_policy(sid,c['id'],int(time.time())+duration if duration else None,limit,speed)
            return store.client(sid,c['id'])
        async def rename_client(self,sid,cid,name):
            from manager.domain import validate_name,ManagerError
            name=validate_name(name)
            if any(c['name'].casefold()==name.casefold() and c['id']!=cid for c in store.clients(sid)):raise ManagerError('Клиент с таким именем на этом сервере уже есть.')
            with store.transaction() as db:db.execute('UPDATE clients SET name=? WHERE id=? AND server_id=?',(name,cid,sid))
            return store.client(sid,cid)
        def paused(self,sid):return store.get_pref(f'paused_clients:{sid}',{})
        async def set_paused(self,sid,cid,pause):
            c=store.client(sid,cid);saved=dict(self.paused(sid))
            if pause:saved[c['public_key']]={'name':c['name']}
            else:saved.pop(c['public_key'],None)
            store.set_pref(f'paused_clients:{sid}',saved)
        async def reissue_client(self,sid,cid,expected_key):
            from manager.domain import ManagerError
            c=store.client(sid,cid)
            if c['public_key']!=expected_key:raise ManagerError('Конфиг уже изменился. Откройте карточку заново.')
            config='# ДЕМОНСТРАЦИОННЫЙ ФАЙЛ: не подключается к VPN\n# '+c['name']+'\n'
            with store.transaction() as db:db.execute('UPDATE clients SET config=?,public_key=? WHERE id=?',(config,f'demo-rotated-{time.time_ns()}',cid))
            return store.client(sid,cid)
        async def delete_client(self,sid,cid):
            store.client(sid,cid)
            with store.transaction() as db:db.execute('UPDATE clients SET present=0 WHERE id=? AND server_id=?',(cid,sid))
    app = create_web_app(SimpleNamespace(token='DEMO_ONLY',admin_id=0),store,DemoService())
    # Demo-only outer middleware: replaces credentials and labels every response.
    import hashlib,hmac,json
    from urllib.parse import urlencode
    update_demo={'phase':'idle','enabled':True}
    @web.middleware
    async def demo(request,handler):
        if request.method == 'POST' and request.path.endswith('/backup'):
            return web.json_response({'sent': True})
        if request.path.startswith('/api/updates'):
            if request.path.endswith('/changelog'):
                path=Path(__file__).resolve().parents[1]/'CHANGELOG.md'
                return web.json_response({'text':path.read_text() if path.exists() else '# Changelog\nДемонстрационная версия.'})
            # Fully fictional update flow. Never invoke the real updater from demo.
            if request.path.endswith('/preferences'):update_demo['enabled']=(await request.json())['enabled']
            if request.path.endswith('/prepare'):return web.json_response({'nonce':'DEMO','version':'2.0.0-beta.99'})
            if request.path.endswith('/start'):
                update_demo['phase']='queued'
                return web.json_response({'phase':'queued','version':'2.0.0-beta.99'},status=202)
            phase=update_demo['phase']
            if phase in ['queued','downloading','checking','installing','verifying']:
                update_demo['phase']=['queued','downloading','checking','installing','verifying','complete'][['queued','downloading','checking','installing','verifying'].index(phase)+1]
            return web.json_response({'current':'2.0.0-beta.5','checked':time.time(),'error':False,'enabled':update_demo['enabled'],'release':{'version':'2.0.0-beta.99'},'job':{'phase':phase,'version':'2.0.0-beta.99'}})
        pairs={'auth_date':str(int(time.time())),'user':json.dumps({'id':0})}
        secret=hmac.new(b'WebAppData',b'DEMO_ONLY',hashlib.sha256).digest()
        pairs['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(pairs.items())).encode(),hashlib.sha256).hexdigest()
        headers=dict(request.headers);headers['Authorization']='tma '+urlencode(pairs)
        response=await handler(request.clone(headers=headers))
        if request.path=='/api/overview' and response.status==200:
            data=json.loads(response.text);data['demo']=True
            response.text=json.dumps(data)
        return response
    async def qa_page(request):
        return web.FileResponse(Path(__file__).resolve().parents[1]/'tests/browser/dom.html')
    app.router.add_get('/_qa/dom',qa_page)
    async def qa_script(request):
        return web.FileResponse(Path(__file__).resolve().parents[1]/'tests/browser/dom.test.js')
    app.router.add_get('/_qa/dom.js',qa_script)
    app.middlewares.insert(0,demo)
    async def cleanup(app):
        store.close();directory.cleanup()
    app.on_cleanup.append(cleanup)
    return app

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    web.run_app(create_demo(),host='127.0.0.1',port=args.port,access_log=None)
