#!/usr/bin/env python3
"""Loopback-only, disposable Mini App demo. Never reads production settings."""
import argparse
import math
import sys
import tempfile
import time
from datetime import datetime
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
    class DemoRunner:
        installed=False
        def __init__(self,*args):pass
        def run(self,command,**kwargs):
            from manager.speedtest import PROBE,INSTALL
            if command==PROBE:return 'python' if self.installed else 'missing'
            if command==INSTALL:DemoRunner.installed=True;return ''
            return '{"download":310000000,"upload":185000000,"ping":12.8,"server":{"name":"Демо-узел"}}'
        def close(self):pass
    class DemoService:
        runner_factory=DemoRunner
        def runner(self,sid):return DemoRunner()
        async def worker(self,operation):return operation()
        def lock(self, sid):
            import asyncio
            return asyncio.Lock()
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
        async def create_client(self,sid,name,duration,limit):
            from manager.domain import validate_name,ManagerError
            validate_name(name)
            if any(c['name']==name for c in store.clients(sid)):raise ManagerError('Клиент с таким именем уже есть.')
            existing=store.clients(sid)
            peers=[Peer(c['public_key'],c['name'],c['allowed_ips'],'',handshake=c['handshake'],received=c['last_rx'] or 0,sent=c['last_tx'] or 0) for c in existing]
            key=f'demo-new-{time.time_ns()}'
            peers.append(Peer(key,name,f'10.8.{sid}.{len(existing)+20}/32',''))
            store.sync(sid,peers,'demo-public')
            c=next(c for c in store.clients(sid) if c['public_key']==key)
            store.set_policy(sid,c['id'],int(time.time())+duration if duration else None,limit)
            return store.client(sid,c['id'])
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
    @web.middleware
    async def demo(request,handler):
        pairs={'auth_date':str(int(time.time())),'user':json.dumps({'id':0})}
        secret=hmac.new(b'WebAppData',b'DEMO_ONLY',hashlib.sha256).digest()
        pairs['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(pairs.items())).encode(),hashlib.sha256).hexdigest()
        headers=dict(request.headers);headers['Authorization']='tma '+urlencode(pairs)
        response=await handler(request.clone(headers=headers))
        if request.path=='/api/overview' and response.status==200:
            data=json.loads(response.text);data['demo']=True
            response.text=json.dumps(data)
        return response
    app.middlewares.insert(0,demo)
    async def cleanup(app):
        store.close();directory.cleanup()
    app.on_cleanup.append(cleanup)
    return app

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    web.run_app(create_demo(),host='127.0.0.1',port=args.port,access_log=None)
