"""Same-process Mini App API. Optional; binds to loopback behind an HTTPS proxy."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
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
BACKUP_INTERVALS = (3, 6, 9, 12, 24)
BACKUP_SCHEDULE_KEY = 'backup_schedule'
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
        self.restore_preview = None
        self.resource_since = {}
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

    async def backup_schedule(self, request):
        self.require_owner(request['uid'])
        if request.method == 'PATCH':
            body = await request.json()
            if not isinstance(body, dict) or 'hours' not in body:
                raise ManagerError('Выберите интервал отправки.')
            hours = body['hours']
            schedule = None
            if hours is not None:
                if type(hours) is not int or hours not in BACKUP_INTERVALS:
                    raise ManagerError('Выберите интервал отправки.')
                ids = body.get('server_ids', [body.get('server_id')])
                if (not isinstance(ids, list) or not ids or
                        any(type(sid) is not int for sid in ids) or self.bot is None):
                    raise ManagerError('Выберите серверы для бэкапа.')
                ids = list(dict.fromkeys(ids))
                for sid in ids:
                    self.store.server(sid)
                schedule = {'server_ids': ids, 'hours': hours,
                            'next_at': int(time.time()) + hours * 3600}
            self.store.set_pref(BACKUP_SCHEDULE_KEY, schedule)
        return web.json_response({'schedule': self.store.get_pref(BACKUP_SCHEDULE_KEY),
                                  'history': self.store.get_pref('backup_history', [])})

    async def deliver_backup(self, sid, automatic=False):
        outcome = 'failed'
        try:
            await asyncio.wait_for(send_backup(
                self.bot, self.service, self.settings.admin_id, sid
            ), timeout=120)
            outcome = 'sent'
        except asyncio.CancelledError:
            outcome = None
            raise
        finally:
            if outcome is not None:
                await self.record_backup(sid, automatic, outcome)

    async def record_backup(self, sid, automatic, outcome):
        history = self.store.get_pref('backup_history', [])
        previous = next((event for event in history
                         if event['server_id'] == sid and event['automatic']), None)
        self.store.set_pref('backup_history', [
            {'server_id': sid, 'at': int(time.time()), 'status': outcome,
             'automatic': automatic}, *history[:29],
        ])
        if automatic and ((outcome == 'failed' and (not previous or previous['status'] != 'failed'))
                          or (outcome == 'sent' and previous and previous['status'] == 'failed')):
            try:
                label = 'Бэкап по расписанию не отправлен' if outcome == 'failed' else 'Автоотправка бэкапов восстановлена'
                await asyncio.wait_for(self.bot.send_message(
                    self.settings.admin_id, f'{label}. Сервер №{sid}.'
                ), timeout=10)
            except Exception as exc:
                log.warning('Не удалось отправить уведомление о бэкапе: %s', type(exc).__name__)

    async def run_scheduled_backup(self):
        schedule = self.store.get_pref(BACKUP_SCHEDULE_KEY)
        if not schedule or schedule['next_at'] > time.time() or self.backup_lock.locked():
            return
        async with self.backup_lock:
            ids = schedule.get('server_ids', [schedule.get('server_id')])
            pending = schedule.get('retry_ids', ids)
            failures = []
            for sid in pending:
                if self.store.get_pref(BACKUP_SCHEDULE_KEY) != schedule:
                    return
                try:
                    await self.deliver_backup(sid, automatic=True)
                except Exception as exc:
                    failures.append(sid)
                    log.warning('Ошибка автоотправки бэкапа: server=%s type=%s', sid, type(exc).__name__)
            if self.store.get_pref(BACKUP_SCHEDULE_KEY) == schedule:
                delay = 900 if failures else schedule['hours'] * 3600
                updated = {**schedule, 'next_at': int(time.time()) + delay}
                updated.pop('retry_ids', None)
                if failures:
                    updated['retry_ids'] = failures
                self.store.set_pref(BACKUP_SCHEDULE_KEY, updated)

    async def backup(self, request):
        self.require_owner(request['uid'])
        if self.bot is None:
            raise ManagerError('Отправка резервной копии недоступна.')
        if self.backup_lock.locked():
            raise ManagerError('Резервная копия уже отправляется.')
        async with self.backup_lock:
            try:
                await self.deliver_backup(int(request.match_info['sid']))
            except TelegramAPIError:
                raise ManagerError('Telegram не принял файл. Проверьте, что чат с ботом открыт, и повторите.') from None
        return web.json_response({'sent': True})

    async def restore_backup(self, request):
        self.require_owner(request['uid'])
        sid = int(request.match_info['sid'])
        if request.path.endswith('/preview'):
            if request.content_length is None or not 0 < request.content_length <= 8 * 1024 * 1024:
                raise ManagerError('Выберите ZIP-архив размером до 8 МБ.')
            try:
                archive = await request.content.readexactly(request.content_length)
            except asyncio.IncompleteReadError:
                raise ManagerError('Архив загрузился не полностью. Повторите попытку.') from None
            preview = await self.service.restore_backup(sid, archive)
            token = secrets.token_urlsafe(24)
            self.restore_preview = (token, sid, archive, preview.pop('revision'), time.monotonic() + 300)
            return web.json_response({**preview, 'token': token})
        body = await request.json()
        pending = self.restore_preview
        if not pending or body.get('token') != pending[0] or sid != pending[1] or time.monotonic() >= pending[4]:
            raise ManagerError('Срок предпросмотра истёк. Выберите архив заново.')
        self.restore_preview = None
        result = await self.service.restore_backup(sid, pending[2], pending[3])
        return web.json_response({'restored': len(result['add'])})

    async def resource_alerts(self, request):
        self.require_owner(request['uid'])
        if request.method == 'PATCH':
            body = await request.json()
            if any(type(body.get(key)) is not int or not (body[key] == 0 or 1 <= body[key] <= 100)
                   for key in ('disk', 'cpu')):
                raise ManagerError('Укажите порог от 1 до 100% или 0 для отключения.')
            self.store.set_pref('resource_alerts', {key: body[key] for key in ('disk', 'cpu')})
            self.resource_since.clear()
            self.store.set_pref('resource_alert_state', {})
        return web.json_response(self.store.get_pref('resource_alerts', {'disk': 0, 'cpu': 0}))

    async def check_resource_alerts(self):
        if self.restore_preview and time.monotonic() >= self.restore_preview[4]:
            self.restore_preview = None
        thresholds = self.store.get_pref('resource_alerts', {})
        if not self.bot or not any(thresholds.values()):
            return
        sent = self.store.get_pref('resource_alert_state', {})
        for server in self.store.servers():
            sample = self.resources.get(server.id)
            if not sample or sample['stale']:
                for kind in ('disk', 'cpu'):
                    self.resource_since.pop(f'{server.id}:{kind}', None)
                continue
            disk = sample['disk']
            values = {'disk': disk['used'] / disk['total'] * 100, 'cpu': sample['cpu']}
            for kind, value in values.items():
                threshold = thresholds.get(kind)
                key = f'{server.id}:{kind}'
                if not threshold or value is None:
                    self.resource_since.pop(key, None)
                    continue
                label = 'Диск' if kind == 'disk' else 'Нагрузка CPU'
                if value >= threshold:
                    since = self.resource_since.setdefault(key, time.monotonic())
                    if sent.get(key) or (kind == 'cpu' and time.monotonic() - since < 180):
                        continue
                    message = f'{label}: {value:.0f}% (порог {threshold}%).'
                    active = True
                else:
                    self.resource_since.pop(key, None)
                    if not sent.get(key) or value > max(0, threshold - 2):
                        continue
                    message = f'{label}: показатель вернулся в норму ({value:.0f}%).'
                    active = False
                try:
                    from html import escape
                    await asyncio.wait_for(self.bot.send_message(
                        self.settings.admin_id, f'{escape(server.name)}. {message}'
                    ), timeout=8)
                    if self.store.get_pref('resource_alerts', {}) != thresholds:
                        return
                    if active:
                        sent[key] = True
                    else:
                        sent.pop(key, None)
                    self.store.set_pref('resource_alert_state', sent)
                except Exception as exc:
                    log.warning('Resource notification failed: %s', type(exc).__name__)

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
        elif request.method == 'PATCH':
            body = await request.json()
            self.access.set_scope(actor, int(request.match_info['uid']), body['servers'], body['operations'])
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
        async def refresh_resources():
            await self.resources.refresh()
            await self.check_resource_alerts()
        queue('resources', refresh_resources)
        queue('hoststats', self.hoststats.refresh)

    async def overview(self, request):
        self.queue_refresh()
        servers = []
        for s in self.store.servers():
            if not self.access.permits(request['uid'], s.id):
                continue
            health = self.store.get_pref(f'health:{s.id}', {})
            samples = self.store.samples(s.id, 120)
            rx, tx = rates(samples)
            latest = samples[-1] if samples else {'rx': 0, 'tx': 0, 'ts': 0}
            client_rows = self.store.clients(s.id)
            clients = [client_json(c) for c in client_rows]
            stale = time.time()-health.get('updated', 0) > 20
            reachable = bool(health.get('ok'))
            unknown = not reachable or stale
            paused = self.store.get_pref(f'paused_clients:{s.id}', {})
            for c, row in zip(clients, client_rows):
                c['paused'] = row['public_key'] in paused
                c['labels'] = self.store.get_pref(f"client_labels:{s.id}:{c['id']}", {})
                c['status_known'] = not unknown
                if unknown or c['paused']: c['online'] = False
            servers.append(dict(id=s.id, name=s.name, permissions={operation: self.access.permits(request['uid'], s.id, operation) for operation in ('delete', 'pause', 'reissue', 'reboot')}, ok=reachable, stale=stale, updated=latest['ts'], rx=latest['tx'], tx=latest['rx'],
                                rate_rx=tx if not unknown else 0, rate_tx=rx if not unknown else 0,
                                online=sum(c['online'] for c in clients), host_traffic=self.hoststats.get(s.id), resources=self.resources.get(s.id), clients=clients, samples=[{**p, "rx":p["tx"], "tx":p["rx"]} for p in samples]))
        return web.json_response(dict(servers=servers, monthly_since=self.store.get_pref('monthly_since'), build=build_info(), access={'owner':request['uid']==self.settings.admin_id}, demo=False))

    async def speedtest(self, request):
        sid = int(request.match_info['sid'])
        if request.method == 'GET':
            return web.json_response(self.speedtests.status(sid))
        body = await request.json()
        return web.json_response(self.speedtests.start(sid, body.get('action')), status=202)

    async def diagnostics(self, request):
        sid = int(request.match_info['sid'])
        self.store.server(sid)
        try:
            await self.service.snapshot(sid, force=True)
        except ManagerError:
            return web.json_response({'ok': False, 'resources': None})
        await self.resources.refresh()
        return web.json_response({'ok': True, 'resources': self.resources.get(sid)})

    async def client_labels(self, request):
        sid = int(request.match_info['sid'])
        cid = int(request.match_info['cid'])
        self.store.client(sid, cid)
        body = await request.json()
        labels = {}
        for key in ('owner', 'device', 'group', 'note'):
            value = body.get(key, '')
            if not isinstance(value, str) or len(value) > (250 if key == 'note' else 64):
                raise ManagerError('Сократите описание клиента.')
            labels[key] = value.strip()
        self.store.set_pref(f'client_labels:{sid}:{cid}', labels)
        return web.json_response(labels)

    async def service_traffic_control(self, request):
        self.require_owner(request["uid"])
        sid = int(request.match_info["sid"])
        self.store.server(sid)
        if request.method == "PATCH":
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                raise ValueError
            try:
                result = await self.service.service_traffic_control(sid, body["enabled"])
            except ManagerError as exc:
                raise ManagerError("Не удалось изменить состояние сборщика. Проверьте статус службы.") from exc
        else:
            try:
                result = await self.service.service_traffic_control(sid)
            except ManagerError:
                result = {"state": "unavailable"}
        return web.json_response(result)

    async def service_traffic(self, request):
        self.require_owner(request["uid"])
        sid = int(request.match_info["sid"])
        cid = request.match_info.get("cid")
        window = request.query.get("window", "day")
        if window not in ("day", "week"):
            raise ValueError
        self.store.server(sid)
        key = self.store.client(sid, int(cid))["public_key"] if cid else ""
        try:
            data = await self.service.service_traffic(sid, window, key)
        except (ManagerError, ValueError):
            return web.json_response({"state": "unavailable", "rows": []})
        return web.json_response(data)

    async def daily(self, request):
        sid = int(request.match_info['sid'])
        cid = request.match_info.get('cid')
        month = request.query.get('month', '')
        client_id = int(cid) if cid else None
        report = self.store.daily_report(sid, month, client_id)
        year, number = map(int, month.split('-'))
        if year > 1 or number > 1:
            prior = f'{year if number > 1 else year-1:04d}-{number-1 or 12:02d}'
            report['previous_days'] = self.store.daily_report(sid, prior, client_id)['days']
        return web.json_response(report)

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

    async def pause_client(self, request):
        body = await request.json()
        pause = body.get('paused')
        if type(pause) is not bool:
            raise ValueError()
        await self.service.set_paused(int(request.match_info['sid']), int(request.match_info['cid']), pause)
        self.last_refresh = 0
        return web.json_response({'paused': pause})

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
                if 'sid' in request.match_info:
                    route = request.match_info.route.resource.canonical
                    permission = None
                    if 'cid' in request.match_info and (request.method == 'DELETE' or route.endswith('/policy')):
                        permission = 'delete'
                    elif '/reissue' in route:
                        permission = 'reissue'
                    elif '/reboot' in route:
                        permission = 'reboot'
                    elif route.endswith('/pause'):
                        permission = 'pause'
                    if not api.access.permits(request['uid'], int(request.match_info['sid']), permission):
                        raise web.HTTPForbidden(text=json.dumps({'error': 'Недостаточно прав для этого действия.'}, ensure_ascii=False), content_type='application/json')
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
    app.router.add_post(r'/api/servers/{sid:\d+}/restore/preview', api.restore_backup)
    app.router.add_post(r'/api/servers/{sid:\d+}/restore', api.restore_backup)
    app.router.add_get('/api/resource-alerts', api.resource_alerts)
    app.router.add_patch('/api/resource-alerts', api.resource_alerts)
    app.router.add_post(r'/api/servers/{sid:\d+}/backup', api.backup)
    app.router.add_post(r'/api/servers/{sid:\d+}/reboot/prepare', api.prepare_reboot)
    app.router.add_post(r'/api/servers/{sid:\d+}/reboot', api.reboot)
    app.router.add_get('/api/backup-schedule', api.backup_schedule)
    app.router.add_patch('/api/backup-schedule', api.backup_schedule)
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
    app.router.add_patch(r'/api/admins/{uid:\d+}', api.administrators)
    app.router.add_delete(r'/api/admins/{uid:\d+}', api.administrators)
    app.router.add_post('/api/servers', api.add_server)
    app.router.add_get(r'/api/servers/{sid:\d+}/speedtest', api.speedtest)
    app.router.add_post(r'/api/servers/{sid:\d+}/speedtest', api.speedtest)
    host_task = backup_task = None
    async def backup_monitor():
        while True:
            await api.run_scheduled_backup()
            await asyncio.sleep(15)
    async def host_monitor():
        while True:
            api.queue_refresh()
            await asyncio.sleep(5)
    async def startup(app):
        nonlocal host_task, backup_task
        host_task = asyncio.create_task(host_monitor())
        if bot is not None:
            backup_task = asyncio.create_task(backup_monitor())
    async def cleanup(app):
        tasks = [task for task in (host_task, backup_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in api.poll_tasks.values(): task.cancel()
        await asyncio.gather(*api.poll_tasks.values(), return_exceptions=True)
        await api.speedtests.close()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_post(r'/api/servers/{sid:\d+}/diagnostics', api.diagnostics)
    app.router.add_patch(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/labels', api.client_labels)
    app.router.add_get(r'/api/servers/{sid:\d+}/service-traffic/control', api.service_traffic_control)
    app.router.add_patch(r'/api/servers/{sid:\d+}/service-traffic/control', api.service_traffic_control)
    app.router.add_get(r'/api/servers/{sid:\d+}/service-traffic', api.service_traffic)
    app.router.add_get(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/service-traffic', api.service_traffic)
    app.router.add_get(r'/api/servers/{sid:\d+}/daily', api.daily)
    app.router.add_get(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/daily', api.daily)
    app.router.add_get(r'/api/servers/{sid:\d+}/monthly', api.monthly)
    app.router.add_post(r'/api/servers/{sid:\d+}/clients', api.create)
    app.router.add_patch(r'/api/servers/{sid:\d+}/clients/{cid:\d+}/pause', api.pause_client)
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
