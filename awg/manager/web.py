"""Same-process Mini App API. Optional; binds to loopback behind an HTTPS proxy."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.exceptions import TelegramAPIError

from . import audit
from .build import build_info
from .updates import Updates
from .access import Access
from .domain import Server, validate_name
from .ui import connection, send_backup
from .reboot import Reboots
from .reissue import Reissues
from .preferences import normalize_preferences
from .domain import ManagerError
from .views import rates
from .speedtest import Speedtests
from .hoststats import HostStats
from .resources import Resources

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parents[2] / 'miniapp'


def validate_init_data(raw: str, token: str, admin_id: int, now=None) -> int:
    """Verify Telegram HMAC, bounded age and the configured administrator."""
    try:
        pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=32)
        values = dict(pairs)
        if not raw or len(raw) > 16384 or len(values) != len(pairs):
            raise ValueError
        digest = values.pop('hash')
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ValueError
        age = (time.time() if now is None else now) - int(values['auth_date'])
        if not -30 <= age <= 3600:
            raise ValueError
        uid = json.loads(values['user'])['id']
        if type(uid) is not int or uid not in (admin_id if isinstance(admin_id, (list,tuple,set)) else [admin_id]):
            raise ValueError
        return uid
    except (ValueError, KeyError, TypeError):
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Откройте Mini App заново из бота. Доступ разрешён только администратору.'}, ensure_ascii=False), content_type='application/json') from None


def client_json(c):
    return dict(id=c['id'], name=c['name'], ip=c['allowed_ips'], rx=c['total_tx'], tx=c['total_rx'],
                handshake=c['handshake'], online=bool(c['handshake'] and 0 <= time.time()-c['handshake'] <= 180),
                last_handshake=max(c['handshake'], c.get('last_handshake', 0)),
                has_history=bool(c['handshake'] or c.get('last_handshake') or c['total_rx'] or c['total_tx']),
                limit=c['traffic_limit'], expires=c['expires'], has_config=bool(c['config']))


def policy(body):
    def number(key, maximum):
        value = body.get(key)
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > maximum:
            raise ManagerError('Укажите положительный срок и лимит или оставьте их пустыми.')
        return int(value)
    return number('duration', 10*365*86400), number('limit', 1024**4*1024)


class MiniApp:
    def __init__(self, settings, store, service, bot=None):
        self.settings, self.store, self.service = settings, store, service
        self.bot = bot
        self.backup_lock = asyncio.Lock()
        self.access = Access(store, settings.admin_id)
        self.updates = Updates(Path(__file__).resolve().parents[2],build_info()["version"])
        self.server_add_lock = asyncio.Lock()
        self.refresh_lock = asyncio.Lock()
        self.last_refresh = 0.0
        self.poll_tasks = {}
        self.speedtests = Speedtests(store, service)
        self.hoststats = HostStats(store, service)
        self.resources = Resources(store, service)
        self.reboots = Reboots(store, service)
        self.reissues = Reissues(store, service)
        self.actor_controllers = {}

    async def update_changelog(self,request):
        path=Path(__file__).resolve().parents[2]/'CHANGELOG.md'
        try:text=path.read_text()[:100000]
        except OSError:text='Changelog этой установки пока недоступен.'
        return web.json_response({'text':text})
    async def update_info(self,request):
        self.require_owner(request['uid'])
        enabled=self.store.get_pref('update_notifications',True)
        if request.method=='PATCH':
            body=await request.json()
            if type(body.get('enabled')) is not bool:raise ValueError()
            enabled=body['enabled'];self.store.set_pref('update_notifications',enabled)
        info=await self.updates.check(request.method=='POST') if enabled or request.method=='POST' else (self.updates.cached or {'current':self.updates.current,'release':None,'checked':None,'error':False})
        return web.json_response({**info,'enabled':enabled,'job':self.updates.status()})
    async def update_prepare(self,request):
        self.require_owner(request['uid'])
        return web.json_response(await self.updates.prepare())
    async def update_start(self,request):
        self.require_owner(request['uid'])
        body=await request.json()
        nonce=body.get('nonce')
        if not isinstance(nonce,str) or len(nonce)>100:raise ValueError()
        try:result=await self.updates.start(nonce)
        except ValueError as e:raise ManagerError(str(e)) from None
        return web.json_response(result,status=202)

    def require_owner(self,uid):
        if uid!=self.settings.admin_id:
            raise web.HTTPForbidden(text=json.dumps({'error':'Это действие доступно только владельцу бота.'},ensure_ascii=False),content_type='application/json')

    async def backup(self, request):
        self.require_owner(request['uid'])
        if self.bot is None:
            raise ManagerError('Отправка резервной копии недоступна.')
        if self.backup_lock.locked():
            raise ManagerError('Резервная копия уже отправляется.')
        async with self.backup_lock:
            try:
                await send_backup(self.bot, self.service, self.settings.admin_id, int(request.match_info['sid']))
            except TelegramAPIError:
                raise ManagerError('Telegram не принял файл. Проверьте, что чат с ботом открыт, и повторите.') from None
        return web.json_response({'sent': True})

    async def audit_log(self,request):
        self.require_owner(request['uid'])
        before=int(request.query.get('before','0'))
        if before<0:raise ManagerError('Некорректная страница журнала.')
        rows=self.store.audit_events(before=before)
        return web.json_response({'events':rows,'next':rows[-1]['id'] if len(rows)==50 else None})

    async def administrators(self, request):
        actor=request['uid']
        self.require_owner(actor)
        if request.method=='POST':
            body=await request.json()
            if not isinstance(body,dict):raise ManagerError('Проверьте данные администратора.')
            self.access.grant(actor,body.get('id'),body.get('name'))
        elif request.method=='DELETE':
            self.access.revoke(actor,int(request.match_info['uid']))
        return web.json_response({'admins':self.access.admins(),'owner_id':self.settings.admin_id})

    async def add_server(self, request):
        self.require_owner(request['uid'])
        body=await request.json()
        if not isinstance(body,dict):raise ManagerError('Проверьте параметры сервера.')
        name=body.get('name')
        if not isinstance(name,str):raise ManagerError('Введите название сервера.')
        name=validate_name(name)
        address=body.get('connection','')
        if not isinstance(address,str):raise ManagerError('Введите SSH-адрес сервера.')
        host,port,username=connection(address)
        auth=body.get('auth_type','password')
        password=body.get('password');key=body.get('key_path')
        if auth not in {'password','key'}:raise ManagerError('Выберите пароль или SSH-ключ.')
        if auth=='password' and (not isinstance(password,str) or not password or len(password)>4096):raise ManagerError('Введите пароль SSH.')
        if auth=='key' and (not isinstance(key,str) or not Path(key).is_absolute() or not Path(key).is_file()):raise ManagerError('Файл SSH-ключа не найден на машине с ботом.')
        container=body.get('container') or 'amnezia-awg'
        path=body.get('config_path') or '/opt/amnezia/awg/wg0.conf'
        endpoint=body.get('endpoint') or host
        if not isinstance(container,str) or not re.fullmatch(r'[\w.-]{1,128}',container) or not isinstance(path,str) or not re.fullmatch(r'/[\w./-]+\.conf',path):raise ManagerError('Проверьте имя контейнера и путь к конфигу.')
        if not isinstance(endpoint,str) or len(endpoint)>253 or not re.fullmatch(r'[a-zA-Z0-9.:_-]+',endpoint):raise ManagerError('Укажите VPN-адрес без протокола и порта.')
        server=Server(0,name,host=host,port=port,username=username,auth_type=auth,password=password if auth=='password' else None,key_path=key if auth=='key' else None,endpoint=endpoint,container=container,config_path=path)
        async with self.server_add_lock:
            await self.service.probe(server)
            server=self.store.add_server(server)
        self.last_refresh=0
        return web.json_response({'id':server.id,'name':server.name},status=201)

    def controller(self, request, kind):
        uid=request['uid']
        if uid==self.settings.admin_id:return self.reboots if kind=='reboot' else self.reissues
        key=(uid,kind)
        if key not in self.actor_controllers:self.actor_controllers[key]=(Reboots if kind=='reboot' else Reissues)(self.store,self.service)
        return self.actor_controllers[key]

    async def prepare_reboot(self, request):
        return web.json_response(self.controller(request,'reboot').prepare(int(request.match_info['sid'])))

    async def reboot(self, request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ManagerError('Подтвердите перезагрузку сервера.')
        result = await self.controller(request,'reboot').execute(int(request.match_info['sid']), body.get('nonce'), body.get('name'))
        return web.json_response(result)

    async def preferences(self, request):
        key = f"ui_preferences:{request['uid']}"
        if request.method == 'POST':
            body = await request.json()
            if not isinstance(body, dict):
                raise ManagerError('Проверьте настройки оформления.')
            self.store.set_pref(key, normalize_preferences(body))
        return web.json_response(normalize_preferences(self.store.get_pref(key, {})))

    def queue_refresh(self):
        # A slow host cannot hold up reads or the next poll of another host.
        if time.monotonic()-self.last_refresh < 5:
            return
        self.last_refresh = time.monotonic()
        def queue(key, operation):
            task = self.poll_tasks.get(key)
            if task is None or task.done():
                async def run():
                    try:
                        await operation()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning('Background poll failed: key=%s type=%s', key, type(exc).__name__)
                self.poll_tasks[key] = asyncio.create_task(run())
        for server in self.store.servers():
            queue(('vpn', server.id), lambda sid=server.id: self.service.snapshot(sid, force=True))
        queue('resources', self.resources.refresh)
        queue('hoststats', self.hoststats.refresh)

    async def overview(self, request):
        self.queue_refresh()
        servers = []
        for s in self.store.servers():
            health = self.store.get_pref(f'health:{s.id}', {})
            samples = self.store.samples(s.id, 120)
            rx, tx = rates(samples)
            latest = samples[-1] if samples else {'rx': 0, 'tx': 0, 'ts': 0}
            clients = [client_json(c) for c in self.store.clients(s.id)]
            stale = time.time()-health.get('updated', 0) > 20
            reachable = bool(health.get('ok'))
            unknown = not reachable or stale
            for c in clients:
                c['status_known'] = not unknown
                if unknown: c['online'] = False
            servers.append(dict(id=s.id, name=s.name, ok=reachable, stale=stale, updated=latest['ts'], rx=latest['tx'], tx=latest['rx'],
                                rate_rx=tx if not unknown else 0, rate_tx=rx if not unknown else 0,
                                online=sum(c['online'] for c in clients), host_traffic=self.hoststats.get(s.id), resources=self.resources.get(s.id), clients=clients, samples=[{**p, "rx":p["tx"], "tx":p["rx"]} for p in samples]))
        return web.json_response(dict(servers=servers, monthly_since=self.store.get_pref('monthly_since'), build=build_info(), access={'owner':request['uid']==self.settings.admin_id}, demo=False))

    async def speedtest(self, request):
        sid = int(request.match_info['sid'])
        if request.method == 'GET':
            return web.json_response(self.speedtests.status(sid))
        body = await request.json()
        return web.json_response(self.speedtests.start(sid, body.get('action')), status=202)

    async def daily(self, request):
        sid = int(request.match_info['sid'])
        cid = request.match_info.get('cid')
        return web.json_response(self.store.daily_report(sid, request.query.get('month', ''), int(cid) if cid else None))

    async def monthly(self, request):
        sid = int(request.match_info['sid'])
        self.store.server(sid)
        rows = self.store.monthly_report(sid, request.query.get('month', ''), request.query.get('sort', 'total'))
        return web.json_response({'clients': [dict(id=c['id'], name=c['name'], ip=c['allowed_ips'], rx=c['tx'], tx=c['rx'], deleted=not c['present']) for c in rows]})

    async def create(self, request):
        body = await request.json()
        duration, limit = policy(body)
        name = body.get('name')
        if not isinstance(name, str):
            raise ManagerError('Введите имя клиента.')
        c = await self.service.create_client(int(request.match_info['sid']), name, duration, limit)
        self.last_refresh = 0
        return web.json_response(client_json(c), status=201)

    async def delete(self, request):
        await self.service.delete_client(int(request.match_info['sid']), int(request.match_info['cid']))
        self.last_refresh = 0
        return web.json_response({'ok': True})

    async def set_policy(self, request):
        sid, cid = int(request.match_info['sid']), int(request.match_info['cid'])
        body = await request.json()
        duration, limit = policy(body)
        # Serialise with monitor/client deletion, using the same service lock.
        async with self.service.lock(sid):
            c = self.store.client(sid, cid)
            expiry = c['expires'] if body.get('keep_expiry') is True else int(time.time())+duration if duration else None
            self.store.set_policy(sid, cid, expiry, limit)
        return web.json_response({'ok': True})

    async def prepare_reissue(self, request):
        return web.json_response(self.controller(request,'reissue').prepare(int(request.match_info['sid']), int(request.match_info['cid'])))

    async def reissue(self, request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ManagerError('Подтвердите перевыпуск конфига.')
        c = await self.controller(request,'reissue').execute(int(request.match_info['sid']), int(request.match_info['cid']), body.get('nonce'))
        self.last_refresh = 0
        return web.json_response({'config': c['config'], 'name': c['name']})

    async def config(self, request):
        c = self.store.client(int(request.match_info['sid']), int(request.match_info['cid']))
        if not c['config']:
            raise ManagerError('Приватный ключ этого клиента не сохранён в боте.')
        return web.json_response({'config': c['config']})


def create_web_app(settings, store, service, bot=None):
    api = MiniApp(settings, store, service, bot)

    @web.middleware
    async def guard(request, handler):
        token=None; started=time.monotonic(); error=None
        request_id=hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[:16]
        operation=request.method+' '+(request.match_info.route.resource.canonical if request.match_info.route.resource else 'unmatched')
        try:
            if request.path.startswith('/api/'):
                auth = request.headers.get('Authorization', '')
                request['uid'] = validate_init_data(auth[4:] if auth.startswith('tma ') else '', settings.token, [settings.admin_id]+[a['id'] for a in api.access.admins()])
                token=audit.begin(request['uid'],'web')
                request_id=audit.context.get()['request_id']
            response = await handler(request)
        except ManagerError as exc:
            response = web.json_response({'error': str(exc)}, status=400)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            response = web.json_response({'error': 'Проверьте введённые данные.'}, status=400)
        except web.HTTPException as exc:
            response = exc
        except Exception as exc:
            error=type(exc).__name__
            log.error('Mini App failure: request=%s operation=%s server=%s type=%s',request_id,operation,request.match_info.get('sid'),error)
            response = web.json_response({'error': 'Не удалось выполнить запрос. Обновите данные и повторите.'}, status=500)
        try:
            if token is not None and (request.method in {'POST','PATCH','DELETE'} or operation.endswith('/config')) and not operation.endswith('/preferences'):
                audit.record(store,operation,'accepted' if response.status==202 else 'ok' if response.status<400 else 'failed',int(request.match_info['sid']) if 'sid' in request.match_info else None,int(request.match_info['cid']) if 'cid' in request.match_info else None,error or ('HTTP'+str(response.status) if response.status>=400 else None),int((time.monotonic()-started)*1000))
        finally:
            if token is not None:audit.context.reset(token)
        response.headers['X-Request-ID']=request_id
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[guard], client_max_size=16384)
    async def index(request):
        return web.FileResponse(STATIC / 'index.html')
    async def asset(request):
        name = request.match_info['name']
        if name not in {'app.js', 'app.css', 'preferences.js', 'dom.js', 'daily.js'}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)
    app.router.add_post(r'/api/servers/{sid:\d+}/backup', api.backup)
    app.router.add_post(r'/api/servers/{sid:\d+}/reboot/prepare', api.prepare_reboot)
    app.router.add_post(r'/api/servers/{sid:\d+}/reboot', api.reboot)
    app.router.add_get('/api/updates/changelog',api.update_changelog)
    app.router.add_get('/api/updates',api.update_info)
    app.router.add_post('/api/updates/check',api.update_info)
    app.router.add_patch('/api/updates/preferences',api.update_info)
    app.router.add_post('/api/updates/prepare',api.update_prepare)
    app.router.add_post('/api/updates/start',api.update_start)
    app.router.add_get('/api/preferences', api.preferences)
    app.router.add_post('/api/preferences', api.preferences)
    app.router.add_get('/', index)
    app.router.add_get('/assets/{name}', asset)
    app.router.add_get('/api/overview', api.overview)
    app.router.add_get('/api/audit',api.audit_log)
    app.router.add_get('/api/admins', api.administrators)
    app.router.add_post('/api/admins', api.administrators)
    app.router.add_delete(r'/api/admins/{uid:\d+}', api.administrators)
    app.router.add_post('/api/servers', api.add_server)
    app.router.add_get(r'/api/servers/{sid:\d+}/speedtest', api.speedtest)
    app.router.add_post(r'/api/servers/{sid:\d+}/speedtest', api.speedtest)
    host_task = None
    async def host_monitor():
        while True:
            api.queue_refresh()
            await asyncio.sleep(5)
    async def startup(app):
        nonlocal host_task
        host_task = asyncio.create_task(host_monitor())
    async def cleanup(app):
        if host_task:
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)
        for task in api.poll_tasks.values(): task.cancel()
        await asyncio.gather(*api.poll_tasks.values(), return_exceptions=True)
        await api.speedtests.close()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get(r'/api/servers/{sid:\d+}/daily', api.daily)
    app.router.add_get(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/daily', api.daily)
    app.router.add_get(r'/api/servers/{sid:\d+}/monthly', api.monthly)
    app.router.add_post(r'/api/servers/{sid:\d+}/clients', api.create)
    app.router.add_delete(r'/api/servers/{sid:\d+}/clients/{cid:\d+}', api.delete)
    app.router.add_patch(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/policy', api.set_policy)
    app.router.add_get(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/config', api.config)
    app.router.add_post(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/reissue/prepare', api.prepare_reissue)
    app.router.add_post(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/reissue', api.reissue)
    return app


async def start_web(settings, store, service, host, port, bot=None):
    runner = web.AppRunner(create_web_app(settings, store, service, bot), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info('Mini App: HTTP на %s:%d (требуется HTTPS-прокси)', host, port)
    return runner
