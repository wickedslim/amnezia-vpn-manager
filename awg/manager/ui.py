from __future__ import annotations

import asyncio
from contextvars import ContextVar
import base64
import ipaddress
import logging
import re
import secrets
import struct
import time
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import aiohttp
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .access import Access
from .domain import ManagerError, Server, validate_name
from .service import VPNService
from .reissue import Reissues
from .store import Store
from . import audit
from .views import ago, client_status, client_text, e, keyboard, rates, size, sparkline

log = logging.getLogger(__name__)
PAGE_SIZE = 10
DURATIONS = [("1 час", "3600"), ("1 день", "86400"), ("1 неделя", "604800"), ("1 месяц", "2592000"), ("Без срока", "none")]
LIMITS = [("5 ГБ", "5000000000"), ("10 ГБ", "10000000000"), ("30 ГБ", "30000000000"), ("100 ГБ", "100000000000"), ("Без лимита", "none")]


class AccessMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int, access=None, actor=None):
        self.admin_id,self.access,self.actor = admin_id,access,actor

    async def __call__(self, handler, event, data):
        uid = event.from_user.id if event.from_user else None
        chat = event.message.chat if isinstance(event, CallbackQuery) and event.message else getattr(event, "chat", None)
        if isinstance(event,Message) and (event.text or '').split(' ',1)[0].split('@',1)[0]=='/id' and uid is not None and chat and chat.type=='private' and chat.id==uid:
            await event.answer(f'Ваш Telegram ID: <code>{uid}</code>\nПередайте его владельцу для получения доступа. Команда не предоставляет права управления.')
            return
        if not (self.access.allowed(uid) if self.access else uid==self.admin_id) or chat is None or chat.type != "private" or chat.id != uid:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ только администратору в личном чате.", show_alert=True)
            return
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except TelegramBadRequest:
                return
        audit_token=audit.begin(uid,'bot')
        token=self.actor.set(uid) if self.actor else None
        try:
            return await handler(event, data)
        finally:
            audit.context.reset(audit_token)
            if self.actor:self.actor.reset(token)


async def send_backup(bot, service, owner_id: int, sid: int):
    content = await service.backup(sid)
    filename = f"amnezia-{sid}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.zip"
    await bot.send_document(
        owner_id, BufferedInputFile(content, filename=filename),
        caption="Резервная копия: база всех серверов и конфигурация выбранного VPN. Содержит секретные ключи и SSH-реквизиты.",
    )


def connection(value: str) -> tuple[str, int, str]:
    """root@host:22, host, [IPv6]:22; no shell is involved."""
    value = value.strip()
    username, hostpart = value.rsplit("@", 1) if "@" in value else ("root", value)
    if not username or len(username) > 64 or not re.fullmatch(r"[\w.-]+", username):
        raise ManagerError("Пользователь SSH указан неверно. Пример: root@203.0.113.10:22")
    if hostpart.startswith("["):
        match = re.fullmatch(r"\[([^]]+)\](?::(\d+))?", hostpart)
        if not match:
            raise ManagerError("Для IPv6 используйте [адрес]:порт.")
        host, port = match[1], int(match[2] or 22)
    elif hostpart.count(":") == 1:
        host, raw_port = hostpart.rsplit(":", 1)
        if not raw_port.isdigit():
            raise ManagerError("SSH-порт должен быть числом.")
        port = int(raw_port)
    else:
        host, port = hostpart, 22
    if not host or len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9.:_-]+", host) or not 1 <= port <= 65535:
        raise ManagerError("Проверьте адрес и порт. Пример: root@203.0.113.10:22")
    return host, port, username


class UI:
    def __init__(self, bot: Bot, store: Store, service: VPNService, admin_id: int):
        self.bot, self.store, self.service, self.owner_id = bot, store, service, admin_id
        self.actor=ContextVar("telegram_actor",default=admin_id)
        self.access=Access(store,admin_id)
        self.revisions={}
        self.reissue_controllers={}
        self.router = Router()
        self.router.message.outer_middleware(AccessMiddleware(admin_id,self.access,self.actor))
        self.router.callback_query.outer_middleware(AccessMiddleware(admin_id,self.access,self.actor))
        self.router.message.register(self.start, Command("start", "help", "servers", "cancel"))
        self.router.message.register(self.audit_log, Command("audit"))
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback, F.data)
        self.wizard_lock = asyncio.Lock()
        self.panel_lock = asyncio.Lock()
        self.revision = 0


    async def audit_log(self,message):
        if self.admin_id!=self.owner_id:
            await message.answer('Журнал доступен только владельцу.');return
        rows=self.store.audit_events(limit=15)
        text='\n'.join(f"#{r['id']} · {r['actor'] if r['actor'] is not None else 'система'} · {r['source']} · {r['action']} · {r['result']} · сервер {r['sid'] or '—'}" for r in rows)
        await message.answer('<b>Последние действия</b>\n'+e(text or 'Записей пока нет.'))

    @property
    def admin_id(self):return self.actor.get()
    @property
    def revision(self):return self.revisions.get(self.admin_id,0)
    @revision.setter
    def revision(self,value):self.revisions[self.admin_id]=value
    @property
    def reissues(self):
        if self.admin_id not in self.reissue_controllers:self.reissue_controllers[self.admin_id]=Reissues(self.store,self.service)
        return self.reissue_controllers[self.admin_id]
    def panel_key(self):return 'panel' if self.admin_id==self.owner_id else f'panel:{self.admin_id}'

    async def panel(self, text, markup, revision=None):
        if self.admin_id!=self.owner_id:
            restricted={'newserver','editserver','settings','unlink','unlinkyes','backup'}
            rows=[[b for b in row if (b.callback_data or '').split(':')[0] not in restricted] for row in markup.inline_keyboard]
            markup=markup.model_copy(update={'inline_keyboard':[row for row in rows if row]})
        async with self.panel_lock:
            if revision is not None and revision != self.revision:
                return
            previous = self.store.get_pref(self.panel_key())
            if previous:
                try:
                    await self.bot.edit_message_text(chat_id=self.admin_id, message_id=previous, text=text, reply_markup=markup)
                    return
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc):
                        return
                except TelegramForbiddenError:
                    raise
            message = await self.bot.send_message(self.admin_id, text, reply_markup=markup)
            self.store.set_pref(self.panel_key(), message.message_id)

    async def start(self, message: Message):
        self.revision += 1
        # /start resumes an unfinished wizard; /cancel explicitly cancels it.
        if message.text.split()[0].split("@", 1)[0] in {"/cancel", "/servers"}:
            self.store.clear_wizard(self.admin_id)
        wizard = self.store.wizard(self.admin_id)
        if wizard and 'sid' in wizard and not self.access.permits(self.admin_id, wizard['sid']):
            self.store.clear_wizard(self.admin_id)
            wizard = None
        if wizard:
            await self.show_wizard(wizard)
        else:
            await self.home(self.revision)

    async def home(self, revision=None, force=False, page=0):
        servers = [s for s in self.store.servers() if self.access.permits(self.admin_id, s.id)]
        if not servers:
            await self.panel("<b>Amnezia · Панель управления</b>\n\nПодключите первый VPN-сервер.\nОдин бот может управлять несколькими серверами по SSH.", keyboard([("＋ Подключить сервер", "newserver")], [("Как это работает", "help")]), revision)
            return
        await self.panel("<b>Amnezia · Обзор серверов</b>\n\nОбновляю доступность и статистику…", keyboard([("＋ Подключить сервер", "newserver")]), revision)
        results = await self.service.overview(force)
        total, online, traffic = 0, 0, 0
        lines, rows = [], []
        pages = max(1, (len(servers) + 7) // 8)
        page = min(max(0, page), pages-1)
        visible = {s.id for s in servers[page*8:(page+1)*8]}
        for server in servers:
            result = results.get(server.id)
            ok = result is not None and not isinstance(result, ManagerError)
            clients = self.store.clients(server.id)
            active = sum(c["handshake"] > 0 and 0 <= time.time() - c["handshake"] <= 180 for c in clients) if ok else 0
            total += len(clients)
            online += active
            sample = self.store.samples(server.id, 1)
            rx, tx = (sample[0]["rx"], sample[0]["tx"]) if sample else (0, 0)
            traffic += rx + tx
            label = f"{active}/{len(clients)} в сети" if ok else "нет связи · последние данные"
            if server.id in visible:
                lines.append(f"{'🟢' if ok else '🔴'} <b>{e(server.name)}</b>\n{label}\n↓ {size(tx)} · ↑ {size(rx)} · Σ <b>{size(rx + tx)}</b>")
                rows.append([(f"{'🟢' if ok else '🔴'} {server.name}", f"s:{server.id}")])
        text = f"<b>Amnezia · Обзор</b>\n\n<b>{len(servers)}</b> серверов  ·  <b>{total}</b> клиентов\n🟢 В сети: <b>{online}</b>  ·  Σ <b>{size(traffic)}</b>\n\n" + "\n\n".join(lines)
        text += "\n\nТрафик накопительный · ↓ загрузка на устройства · ↑ отдача с устройств.\nВыберите сервер для управления."
        if pages > 1:
            text += f" Страница {page+1}/{pages}."
            rows.append(([('‹', f'hp:{page-1}')] if page else []) + ([('›', f'hp:{page+1}')] if page+1 < pages else []))
        rows += [[("＋ Сервер", "newserver"), ("↻ Обновить", "refreshhome")], [("Как это работает", "help")]]
        await self.panel(text, keyboard(*rows), revision)

    async def server(self, sid, revision=None, force=False):
        server = self.store.server(sid)
        self.store.set_pref(f"selected_server:{self.admin_id}", sid)
        navigation = keyboard([("↻ Повторить", f"sr:{sid}")], [("‹ Все серверы", "home")])
        await self.panel(f"<b>{e(server.name)}</b>\n\nПолучаю статистику…", navigation, revision)
        try:
            snapshot = await self.service.snapshot(sid, force)
        except ManagerError as exc:
            await self.panel(f"<b>{e(server.name)}</b>\n\n🔴 Нет связи\n{e(str(exc))}\n\nСуществующие VPN-подключения бот не изменял.", keyboard([("↻ Повторить", f"sr:{sid}")], [("Изменить подключение", f"editserver:{sid}")], [("Отключить от бота", f"unlink:{sid}")], [("‹ Все серверы", "home")]), revision)
            return
        clients = self.store.clients(sid)
        active = sum(p.online for p in snapshot.config.peers)
        samples = self.store.samples(sid)
        tx, rx = rates(samples)
        total_rx = samples[-1]["rx"] if samples else 0
        total_tx = samples[-1]["tx"] if samples else 0
        text = (
            f"<b>{e(server.name)}</b>\n🟢 Сервер доступен\n\n"
            f"<b>Клиенты</b>\n🟢 {active} в сети  ·  🔴 {len(clients)-active} не в сети\nВсего: <b>{len(clients)}</b>\n\n"
            f"<b>VPN-трафик клиентов</b>\n↓ На устройства {size(total_tx)}\n↑ С устройств {size(total_rx)}\n"
            f"Скорость: ↓ {size(rx)}/с  ↑ {size(tx)}/с\n\n"
            f"Обновлено: {ago(snapshot.timestamp)}\nФоновый замер: каждые 60 с; ручное обновление — дополнительный замер.\nНаправления со стороны клиентов. Трафик накопительный, включая начальные счётчики VPN. Для календарного периода откройте статистику по месяцам."
        )
        if self.store.pending_imports(sid):
            text += "\n\n⚠️ Часть старых лимитов не сопоставлена с клиентами. Проверьте ограничения одноимённых клиентов в их карточках."
        await self.panel(text, keyboard([("Клиенты", f"list:{sid}:0"), ("＋ Клиент", f"newclient:{sid}")], [("↻ Обновить", f"sr:{sid}"), ("Резервная копия", f"backup:{sid}")], [("Статистика по месяцам", f"months:{sid}:0")], [("Настройки сервера", f"settings:{sid}")], [("‹ Все серверы", "home")]), revision)

    async def clients(self, sid, page=0, revision=None, force=False, query=""):
        server = self.store.server(sid)
        await self.service.snapshot(sid, force)
        clients = self.store.clients(sid)
        if query:
            clients = [c for c in clients if query.casefold() in c["name"].casefold() or query in c["allowed_ips"]]
        pages = max(1, (len(clients) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = []
        client_buttons = []
        for c in clients[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            icon, _ = client_status(c)
            client_buttons.append((f"{icon} {c['name'][:32]}", f"c:{sid}:{c['id']}"))
        rows.extend(client_buttons[i:i+2] for i in range(0,len(client_buttons),2))
        if not query:
            pager = []
            if page > 0:
                pager.append(("‹", f"list:{sid}:{page-1}"))
            if page + 1 < pages:
                pager.append(("›", f"list:{sid}:{page+1}"))
            rows.append(pager)
        rows += [[("＋ Клиент", f"newclient:{sid}"), ("Поиск", f"search:{sid}")], [("↻ Обновить", f"lr:{sid}:{page}")], [("‹ Сервер", f"s:{sid}"), ("⌂ Главная", "home")]]
        title = f"Поиск: {e(query)}" if query else "Клиенты"
        text = f"<b>{e(server.name)} › {title}</b>\n\nНайдено: <b>{len(clients)}</b>  ·  {page+1}/{pages}\n🟢 В сети · 🔴 Не в сети · ⚪ Нет истории подключения"
        if not clients:
            text += "\n\nКлиентов пока нет." if not query else "\n\nСовпадений не найдено."
        await self.panel(text, keyboard(*rows), revision)

    async def client(self, sid, cid, revision=None, force=False):
        server = self.store.server(sid)
        await self.service.snapshot(sid, force)
        client = self.store.client(sid, cid)
        paused = client['public_key'] in self.store.get_pref(f'paused_clients:{sid}', {})
        pause_button = []
        if self.access.permits(self.admin_id, sid, 'pause'):
            pause_button = [("Возобновить доступ" if paused else "Приостановить доступ",
                             f"{'resume' if paused else 'pause'}:{sid}:{cid}")]
        await self.panel(client_text(server.name, client) + ("\n\n⏸ Доступ приостановлен" if paused else ""), keyboard(pause_button, [("Получить конфигурацию", f"conf:{sid}:{cid}")], [("Перевыпустить конфиг", f"reissue:{sid}:{cid}")], [("↻ Обновить", f"cr:{sid}:{cid}"), ("Лимиты и срок", f"policy:{sid}:{cid}")], [("IP подключения", f"ip:{sid}:{cid}"), ("Удалить", f"delete:{sid}:{cid}")], [("‹ Клиенты", f"list:{sid}:0"), ("⌂ Главная", "home")]), revision)

    async def callback(self, call: CallbackQuery):
        self.revision += 1
        revision = self.revision
        parts = call.data.split(":")
        action = parts[0]
        try:
            if action in {"newserver","editserver","settings","unlink","unlinkyes","backup"}:self.access.require_owner(self.admin_id)
            if action in {"s","sr","list","lr","c","cr","months","report","csv","newclient",
                          "policy","search","reissue","reissueyes","delete","deleteyes","conf","ip","pause","resume"}:
                operation = {"reissue":"reissue","reissueyes":"reissue","delete":"delete","deleteyes":"delete",
                             "pause":"pause","resume":"pause","policy":"delete"}.get(action)
                if not self.access.permits(self.admin_id, int(parts[1]), operation):
                    raise ManagerError("Недостаточно прав для этого действия.")
            if action == "w":
                async with self.wizard_lock:
                    await self.wizard_action(parts)
                return
            if action in {"home", "refreshhome", "hp", "s", "sr", "list", "lr", "c", "cr"}:
                self.store.clear_wizard(self.admin_id)
            if action in {"home", "refreshhome"}:
                await self.home(revision, action == "refreshhome")
            elif action == "hp":
                await self.home(revision, page=int(parts[1]))
            elif action == "help":
                await self.panel("<b>Как это работает</b>\n\nОдин Telegram-бот управляет VPN-серверами по SSH. На каждом VPN-сервере должен работать AmneziaWG.\n\nВыберите сервер → откройте клиентов → выберите клиента. Каждая кнопка относится к указанному серверу.\n\nШаги добавления сохраняются. /start продолжит ввод, /cancel отменит его.\n\nБот не закрепляет сообщения. Меню обновляется в одном сообщении.\n\nПри конфликте getUpdates остановите вторую копию с тем же токеном.", keyboard([("‹ Главная", "home")]), revision)
            elif action in {"s", "sr"}:
                await self.server(int(parts[1]), revision, action == "sr")
            elif action in {"list", "lr"}:
                await self.clients(int(parts[1]), int(parts[2]), revision, action == "lr")
            elif action in {"c", "cr"}:
                await self.client(int(parts[1]), int(parts[2]), revision, action == "cr")
            elif action == "months":
                await self.months(int(parts[1]), int(parts[2]), revision)
            elif action in {"report", "csv"}:
                await self.report(int(parts[1]), parts[2], parts[3], int(parts[4]), revision, action == "csv")
            elif action == "newserver":
                await self.begin({"kind": "server", "step": "name"})
            elif action == "editserver":
                s = self.store.server(int(parts[1]))
                await self.begin({"kind": "server", "step": "name", "edit_sid": s.id, "name": s.name, "host": s.host, "port": s.port, "username": s.username, "endpoint": s.endpoint, "local": s.local, "container": s.container, "config_path": s.config_path})
            elif action in {"newclient", "policy"}:
                sid = int(parts[1])
                self.store.server(sid)
                wizard = {"kind": "client" if action == "newclient" else "policy", "sid": sid, "step": "name" if action == "newclient" else "duration"}
                if action == "policy":
                    client = self.store.client(sid, int(parts[2]))
                    wizard.update(cid=client["id"], name=client["name"])
                await self.begin(wizard)
            elif action == "search":
                self.store.server(int(parts[1]))
                await self.begin({"kind": "search", "step": "query", "sid": int(parts[1])})
            elif action == "settings":
                s = self.store.server(int(parts[1]))
                await self.panel(f"<b>{e(s.name)} › Настройки</b>\n\nПодключение: <code>{e('локально' if s.local else f'{s.username}@{s.host}:{s.port}')}</code>\nКонтейнер: <code>{e(s.container)}</code>\nКонфигурация: <code>{e(s.config_path)}</code>\nАдрес VPN: <code>{e(s.endpoint)}</code>\n\nОтключение от бота сохраняет клиентов на VPN-сервере.", keyboard([("Изменить подключение", f"editserver:{s.id}")], [("Отключить от бота", f"unlink:{s.id}")], [("‹ Сервер", f"s:{s.id}")]), revision)
            elif action == "unlink":
                s = self.store.server(int(parts[1]))
                await self.panel(f"<b>Отключить {e(s.name)} от бота?</b>\n\nVPN продолжит работать. Клиенты и конфигурация на сервере сохранятся. Учёт трафика и ограничений этим ботом прекратится.\n\nРекомендуется сначала скачать резервную копию.", keyboard([("Скачать копию", f"backup:{s.id}")], [("Да, отключить", f"unlinkyes:{s.id}")], [("‹ Отмена", f"s:{s.id}")]), revision)
            elif action == "unlinkyes":
                await self.service.disconnect_server(int(parts[1]))
                await self.home(revision)
            elif action == "reissue":
                sid, cid = map(int, parts[1:3])
                confirmation = self.reissues.prepare(sid, cid)
                await self.panel(f"<b>Перевыпустить конфиг?</b>\n\nСервер: {e(confirmation['server'])}\nКлиент: <b>{e(confirmation['name'])}</b>\n\nСтарый конфиг перестанет работать. Имя, VPN-адрес, лимиты, срок и статистика сохранятся. Новый файл можно будет скачать повторно без перевыпуска.", keyboard([("Перевыпустить", f"reissueyes:{sid}:{cid}:{confirmation['nonce']}")], [("Отмена", f"c:{sid}:{cid}")]), revision)
            elif action == "reissueyes":
                sid, cid = map(int, parts[1:3])
                client = await self.reissues.execute(sid, cid, parts[3])
                filename = re.sub(r"[^\w.-]", "_", client['name'])[:40] or str(cid)
                await self.bot.send_document(self.admin_id, BufferedInputFile(client['config'].encode(), filename=f"{filename}.conf"), caption=f"{e(client['name'])} · новый конфиг. Старый отключён. Файл сохранён для повторного скачивания.")
                await self.client(sid, cid, revision=revision)
            elif action == "delete":
                sid, cid = map(int, parts[1:3])
                client = self.store.client(sid, cid)
                server = self.store.server(sid)
                await self.panel(f"<b>Удалить клиента из VPN?</b>\n\nСервер: {e(server.name)}\nКлиент: <b>{e(client['name'])}</b>\nАдрес: <code>{e(client['allowed_ips'])}</code>\n\nЭтот клиент потеряет доступ. Остальные подключения продолжат работу.", keyboard([("Да, удалить", f"deleteyes:{sid}:{cid}")], [("‹ Отмена", f"c:{sid}:{cid}")]), revision)
            elif action == "deleteyes":
                sid, cid = map(int, parts[1:3])
                await self.service.delete_client(sid, cid)
                await self.clients(sid, revision=revision)
            elif action in {"pause", "resume"}:
                sid, cid = map(int, parts[1:3])
                await self.service.set_paused(sid, cid, action == "pause")
                await self.client(sid, cid, revision=revision)
            elif action == "conf":
                sid, cid = map(int, parts[1:3])
                await self.service.snapshot(sid)
                client = self.store.client(sid, cid)
                text = client["config"]
                if not text:
                    raise ManagerError("Сохранённого конфига пока нет. Нажмите «Перевыпустить конфиг»: новый файл сохранится для повторной выдачи, старый перестанет работать.")
                filename = re.sub(r"[^\w.-]", "_", client["name"])[:40] or str(cid)
                await self.bot.send_document(self.admin_id, BufferedInputFile(text.encode(), filename=f"{filename}.conf"), caption=f"{e(self.store.server(sid).name)} · {e(client['name'])}")
                audit.record(self.store,'config.download','ok',sid,cid)
                raw = text.encode()
                vpn = "vpn://" + base64.urlsafe_b64encode(struct.pack(">I", len(raw)) + zlib.compress(raw, 8)).decode().rstrip("=")
                if len(vpn) < 3800:
                    await self.bot.send_message(self.admin_id, f"<b>Ключ для AmneziaVPN</b>\n<code>{e(vpn)}</code>")
                else:
                    await self.bot.send_document(self.admin_id, BufferedInputFile(vpn.encode(), filename=f"{filename}-vpn.txt"))
            elif action == "backup":
                sid = int(parts[1])
                await send_backup(self.bot, self.service, self.admin_id, sid)
            elif action == "ip":
                await self.ip_info(int(parts[1]), int(parts[2]), revision)
            else:
                raise ManagerError("Эта кнопка устарела. Откройте главную через /servers.")
        except (ValueError, IndexError):
            await self.error("Эта кнопка устарела. Откройте главную.", revision)
        except ManagerError as exc:
            await self.error(str(exc), revision)
        except Exception as exc:
            # Do not log event bodies: the message can contain SSH credentials.
            log.error("Ошибка интерфейса: request=%s action=%s type=%s", (audit.context.get() or {}).get("request_id","—"), action, type(exc).__name__)
            await self.error("Действие не завершилось. Обновите экран; подробности доступны в журнале службы.", revision)

    async def error(self, text, revision=None):
        wizard = self.store.wizard(self.admin_id)
        buttons = [("Продолжить ввод", f"w:{wizard['nonce']}:resume")] if wizard else [("↻ Главная", "home")]
        await self.panel(f"<b>Не удалось выполнить действие</b>\n\n{e(text)}", keyboard(buttons, [("⌂ Главная", "home")]), revision)

    async def ip_info(self, sid, cid, revision):
        await self.service.snapshot(sid)
        c = self.store.client(sid, cid)
        endpoint = c["endpoint"]
        if not endpoint:
            raise ManagerError("У клиента ещё нет IP подключения.")
        host = endpoint[1:].split("]", 1)[0] if endpoint.startswith("[") else endpoint.rsplit(":", 1)[0]
        address = ipaddress.ip_address(host)
        details = ""
        if address.is_global:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    async with session.get(f"http://ip-api.com/json/{address}", params={"fields": "status,country,city,isp", "lang": "ru"}) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data.get("status") == "success":
                                details = f"\n{e(data.get('country', ''))}, {e(data.get('city', ''))}\n{e(data.get('isp', ''))}"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                details = "\nГеоданные временно недоступны."
        history = self.store.connections(sid, cid)
        recent = "\n".join(f"<code>{e(row['endpoint'])}</code> · {ago(row['ts'])}" for row in history[:5])
        await self.panel(f"<b>{e(c['name'])} › IP подключения</b>\n\n<code>{e(str(address))}</code>{details}\n\nПоследнее соединение: {ago(c['handshake'])}\n\n<b>Последние адреса</b>\n{recent}", keyboard([("‹ Клиент", f"c:{sid}:{cid}")]), revision)

    async def begin(self, wizard):
        if wizard.get("kind")=="server":self.access.require_owner(self.admin_id)
        wizard.update(nonce=secrets.token_hex(4), history=[])
        self.store.set_wizard(self.admin_id, wizard)
        await self.show_wizard(wizard)

    def advance(self, wizard, step):
        wizard["history"].append(wizard["step"])
        wizard["step"] = step
        self.store.set_wizard(self.admin_id, wizard)

    async def show_wizard(self, w):
        kind, step, nonce = w["kind"], w["step"], w["nonce"]
        button = lambda text, action: (text, f"w:{nonce}:{action}")
        rows = []
        if kind == "server":
            prompts = {
                "name": "<b>Подключение сервера · 1/5</b>\n\nКак назвать сервер?\nНапример: <code>serv.host</code> или <code>Москва</code>.",
                "connect": "<b>Подключение сервера · 2/5</b>\n\nОтправьте адрес SSH:\n<code>root@203.0.113.10:22</code>\n\nМожно отправить только IP: пользователь root и порт 22 подставятся автоматически.",
                "auth": "<b>Подключение сервера · 3/5</b>\n\nОтправьте SSH-пароль следующим сообщением. Сообщение с паролем будет удалено.\n\nДля входа по ключу нажмите кнопку ниже.",
                "key": "<b>Вход по SSH-ключу</b>\n\nОтправьте путь к приватному ключу на машине, где работает бот.\nНапример: <code>/root/.ssh/id_ed25519</code>",
                "endpoint": "<b>Подключение сервера · 4/5</b>\n\nОтправьте внешний IP или домен VPN-сервера без порта.\nОн попадёт в конфигурации новых клиентов.",
                "review": f"<b>Подключение сервера · 5/5</b>\n\nНазвание: <b>{e(w.get('name', ''))}</b>\nSSH: <code>{e('локально' if w.get('local') else str(w.get('username', '')) + '@' + str(w.get('host', '')) + ':' + str(w.get('port', 22)))}</code>\nВход: {'локально' if w.get('local') else 'SSH-ключ' if w.get('auth_type') == 'key' else 'пароль сохранён'}\nVPN-адрес: <code>{e(w.get('endpoint', ''))}</code>\n\nПеред сохранением проверю SSH, контейнер и VPN. Клиенты на сервере сохранятся.",
                "advanced": "<b>Параметры контейнера</b>\n\nОтправьте две строки:\n<code>amnezia-awg\n/opt/amnezia/awg/wg0.conf</code>",
                "saving": "<b>Подключаю сервер…</b>\n\nПроверяю SSH и доступность VPN. Это может занять несколько секунд.",
            }
            if step == "connect":
                rows.append([button("На машине с ботом", "local")])
            if step == "auth":
                rows.append([button("Использовать SSH-ключ", "key")])
            if step == "endpoint" and w.get("host"):
                rows.append([button(f"Использовать {w['host'][:35]}", "samehost")])
            if step == "review":
                rows += [[button("✓ Проверить и подключить", "save")], [button("Параметры контейнера", "advanced")]]
            text = prompts.get(step, prompts["review"])
        elif kind in {"client", "policy"}:
            server = self.store.server(w["sid"])
            title = f"<b>{e(server.name)} › {'Новый клиент' if kind == 'client' else 'Лимиты клиента'}</b>"
            if step == "name":
                text = title + "\n\n<b>Шаг 1/4 · Имя</b>\nОтправьте имя клиента. Допускаются пробелы, точки и подчёркивания."
            elif step == "duration":
                text = title + f"\n\n<b>{e(w.get('name', ''))} · Срок действия</b>\nВыберите срок. Он начнётся после сохранения."
                rows += [[button(label, f"duration={value}")] for label, value in DURATIONS]
            elif step == "limit":
                text = title + "\n\n<b>Лимит трафика</b>\nВходящий и исходящий трафик вместе."
                rows += [[button(label, f"limit={value}")] for label, value in LIMITS]
            elif step == "saving":
                text = title + "\n\nСохраняю клиента… Остальные VPN-подключения продолжат работу."
            else:
                duration = dict((v, k) for k, v in DURATIONS).get(str(w.get("duration")) if w.get("duration") else "none", "Без срока")
                limit = dict((v, k) for k, v in LIMITS).get(str(w.get("limit")) if w.get("limit") else "none", "Без лимита")
                text = title + f"\n\nИмя: <b>{e(w.get('name', ''))}</b>\nСрок: {duration}\nТрафик: {limit}\n\n" + ("Новые лимиты заменят предыдущие. Уже учтённый трафик сохранится." if kind == "policy" else "Проверьте параметры перед созданием.")
                rows.append([button("✓ Сохранить", "save")])
        else:
            text = "<b>Поиск клиента</b>\n\nОтправьте часть имени или VPN-адрес клиента."
        if step != "saving":
            if w["history"]:
                rows.append([button("‹ Назад", "back"), button("Отмена", "cancel")])
            else:
                rows.append([button("Отмена", "cancel")])
        await self.panel(text, keyboard(*rows))

    async def wizard_action(self, parts):
        w = self.store.wizard(self.admin_id)
        if w and 'sid' in w and not self.access.permits(self.admin_id, w['sid'], 'delete' if w['kind']=='policy' else None):
            raise ManagerError('Доступ к серверу отозван.')
        if len(parts) != 3 or not w or w["nonce"] != parts[1]:
            raise ManagerError("Этот шаг уже закрыт. Начните добавление заново.")
        action = parts[2]
        if w["step"] == "saving":
            return
        if action == "cancel":
            self.store.clear_wizard(self.admin_id)
            if w.get("sid"):
                await self.server(w["sid"], self.revision)
            else:
                await self.home(self.revision)
            return
        if action == "back" and w["history"]:
            w["step"] = w["history"].pop()
            self.store.set_wizard(self.admin_id, w)
        elif action == "local" and w["kind"] == "server" and w["step"] == "connect":
            w.update(local=True, host="", port=22, username="", auth_type="local")
            self.advance(w, "endpoint")
        elif action == "key" and w["step"] == "auth":
            self.advance(w, "key")
        elif action == "samehost" and w["step"] == "endpoint":
            w["endpoint"] = w["host"]
            self.advance(w, "review")
        elif action == "advanced" and w["step"] == "review" and w["kind"] == "server":
            self.advance(w, "advanced")
        elif action.startswith("duration=") and w["step"] == "duration":
            value = action.split("=", 1)[1]
            if value not in dict((v, k) for k, v in DURATIONS):
                raise ManagerError("Неизвестный срок действия.")
            w["duration"] = None if value == "none" else int(value)
            self.advance(w, "limit")
        elif action.startswith("limit=") and w["step"] == "limit":
            value = action.split("=", 1)[1]
            if value not in dict((v, k) for k, v in LIMITS):
                raise ManagerError("Неизвестный лимит трафика.")
            w["limit"] = None if value == "none" else int(value)
            self.advance(w, "review")
        elif action == "save" and w["step"] == "review":
            await self.save_wizard(w)
            return
        elif action != "resume":
            raise ManagerError("Кнопка относится к предыдущему шагу. Продолжите текущий ввод.")
        await self.show_wizard(w)

    async def message(self, message: Message):
        async with self.wizard_lock:
            w = self.store.wizard(self.admin_id)
            if not w:
                await message.answer("Используйте кнопки меню. /servers — открыть серверы; /start — продолжить настройку.")
                return
            if 'sid' in w and not self.access.permits(self.admin_id, w['sid'], 'delete' if w['kind']=='policy' else None):
                self.store.clear_wizard(self.admin_id)
                await message.answer('Доступ к серверу отозван.')
                return
            if not message.text:
                await message.answer("На этом шаге нужен текст. /cancel — отменить ввод.")
                return
            value = message.text.strip()
            try:
                step = w["step"]
                if w["kind"] == "search":
                    query = validate_name(value)
                    self.store.clear_wizard(self.admin_id)
                    await self.clients(w["sid"], query=query, revision=self.revision)
                    return
                if step == "name":
                    w["name"] = validate_name(value)
                    if w["kind"] == "server" and any(s.id != w.get("edit_sid") and s.name.casefold() == w["name"].casefold() for s in self.store.servers()):
                        raise ManagerError("Сервер с таким названием уже есть.")
                    self.advance(w, "connect" if w["kind"] == "server" else "duration")
                elif step == "connect":
                    host, port, user = connection(value)
                    w.update(host=host, port=port, username=user, local=False)
                    self.advance(w, "auth")
                elif step == "auth":
                    try:
                        await message.delete()
                    except TelegramBadRequest:
                        pass
                    if value.casefold() in {"пароль", "password"}:
                        await self.show_wizard(w)
                        return
                    w.update(password=message.text, auth_type="password", key_path=None)
                    self.advance(w, "endpoint")
                elif step == "key":
                    path = Path(value).expanduser()
                    if not path.is_absolute() or not path.is_file():
                        raise ManagerError("Файл ключа не найден на машине с ботом. Укажите полный путь.")
                    w.update(key_path=str(path), auth_type="key", password=None)
                    self.advance(w, "endpoint")
                elif step == "endpoint":
                    if not re.fullmatch(r"[a-zA-Z0-9.:_-]+", value) or len(value) > 253:
                        raise ManagerError("Отправьте IP или домен без протокола и порта.")
                    if ":" in value:
                        try:
                            ipaddress.IPv6Address(value)
                        except ValueError as exc:
                            raise ManagerError("Укажите IPv6 без порта и квадратных скобок.") from exc
                    w["endpoint"] = value
                    self.advance(w, "review")
                elif step == "advanced":
                    lines = value.splitlines()
                    if len(lines) != 2 or not re.fullmatch(r"[\w.-]+", lines[0]) or not re.fullmatch(r"/[\w./-]+\.conf", lines[1]):
                        raise ManagerError("Нужны две строки: имя контейнера и абсолютный путь к .conf.")
                    w.update(container=lines[0], config_path=lines[1])
                    self.advance(w, "review")
                else:
                    await message.answer("На этом шаге выберите кнопку ниже. Можно вернуться назад или отменить ввод.")
                await self.show_wizard(w)
            except ManagerError as exc:
                await message.answer(e(str(exc)))
            except Exception:
                log.error("Ошибка ввода на шаге %s", w.get("step"))
                await message.answer("Не удалось обработать ввод. /start продолжит текущий шаг.")

    async def save_wizard(self, w):
        if w["kind"]=="server":self.access.require_owner(self.admin_id)
        revision = self.revision
        w["step"] = "saving"
        self.store.set_wizard(self.admin_id, w)
        await self.show_wizard(w)
        try:
            if w["kind"] == "server":
                server = Server(id=w.get("edit_sid", 0), name=w["name"], host=w["host"], port=w["port"], username=w["username"], auth_type=w["auth_type"], password=w.get("password"), key_path=w.get("key_path"), endpoint=w["endpoint"], local=w.get("local", False), container=w.get("container", "amnezia-awg"), config_path=w.get("config_path", "/opt/amnezia/awg/wg0.conf"))
                if w.get("edit_sid"):
                    await self.service.update_server(server)
                else:
                    await self.service.probe(server)
                    server = self.store.add_server(server)
                self.store.clear_wizard(self.admin_id, w["nonce"])
                if revision == self.revision:
                    await self.server(server.id, revision)
            else:
                sid = w["sid"]
                if not self.access.permits(self.admin_id, sid, 'delete' if w['kind']=='policy' else None):
                    raise ManagerError('Недостаточно прав для этого действия.')
                if w["kind"] == "client":
                    client = await self.service.create_client(sid, w["name"], w.get("duration"), w.get("limit"))
                    cid = client["id"]
                else:
                    cid = w["cid"]
                    self.store.set_policy(sid, cid, int(time.time()) + w["duration"] if w.get("duration") else None, w.get("limit"))
                self.store.clear_wizard(self.admin_id, w["nonce"])
                if revision == self.revision:
                    await self.client(sid, cid, revision)
        except Exception:
            w["step"] = "review"
            current = self.store.wizard(self.admin_id)
            if current and current["nonce"] == w["nonce"]:
                self.store.set_wizard(self.admin_id, w)
            raise

    async def months(self, sid, offset=0, revision=None):
        server = self.store.server(sid)
        now = datetime.now(ZoneInfo("Europe/Samara"))
        year = now.year + offset
        if not 2000 <= year <= now.year:
            raise ManagerError("Выберите год не позднее текущего.")
        names = ["Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
        buttons = [(name, f"report:{sid}:{year}-{i:02d}:total:0") for i,name in enumerate(names,1) if year < now.year or i <= now.month]
        rows = [buttons[i:i+3] for i in range(0,len(buttons),3)]
        rows.append([("‹ Год",f"months:{sid}:{offset-1}")] + ([("Год ›",f"months:{sid}:{offset+1}")] if offset < 0 else []))
        rows.append([("‹ Сервер",f"s:{sid}")])
        since = datetime.fromtimestamp(self.store.get_pref("monthly_since"),ZoneInfo("Europe/Samara"))
        await self.panel(f"<b>{e(server.name)} › Статистика · {year}</b>\n\nВыберите месяц. Часовой пояс: Самара (UTC+4).\nУчёт по месяцам начат {since:%d.%m.%Y %H:%M}. Более ранняя история недоступна.\nУдалённые конфиги сохраняются в отчётах.",keyboard(*rows),revision)

    async def report(self, sid, month, sort="total", page=0, revision=None, export=False):
        server = self.store.server(sid)
        # Historical reports work even when the VPN server is offline.
        data = self.store.monthly_report(sid,month,sort)
        if export:
            import csv,io
            output=io.StringIO()
            writer=csv.writer(output,delimiter=";")
            writer.writerow(["Конфиг","VPN-адрес","Статус","Получено клиентом, байт","Отправлено клиентом, байт","Всего, байт"])
            for c in data:
                name = c["name"]
                if name.startswith(("=","+","-","@")): name="'"+name
                writer.writerow([name,c["allowed_ips"],"действующий" if c["present"] else "удалён",c["tx"],c["rx"],c["rx"]+c["tx"]])
            await self.bot.send_document(self.admin_id,BufferedInputFile(output.getvalue().encode("utf-8-sig"),filename=f"traffic-{sid}-{month}.csv"))
            return
        pages=max(1,(len(data)+7)//8); page=min(max(0,page),pages-1)
        total=sum(c["rx"]+c["tx"] for c in data)
        text=f"<b>{e(server.name)} › {month}</b>\n\nВсего: <b>{size(total)}</b> · Конфигов: {len(data)}\nПоказано с точки зрения клиента: ↓ получил, ↑ отправил.\nСортировка: {dict(total='суммарный трафик',rx='получено',tx='отправлено')[sort]} ↓\n\n"
        for rank,c in enumerate(data[page*8:(page+1)*8],page*8+1):
            text += f"<b>{rank}. {e(c['name'])}</b>{' · удалён' if not c['present'] else ''}\n{e(c['allowed_ips'])} · ↓ {size(c['tx'])} · ↑ {size(c['rx'])}\nВсего {size(c['rx']+c['tx'])}\n\n"
        if not data: text += "Данных за этот месяц нет.\n\n"
        text += f"Страница {page+1}/{pages}. Учёт с момента включения месячной статистики. При смене месяца интервал относится к месяцу завершающего замера (обычно до 60 с)."
        rows=[[("Σ Всего",f"report:{sid}:{month}:total:0"),("↓ На устройства",f"report:{sid}:{month}:rx:0"),("↑ С устройств",f"report:{sid}:{month}:tx:0")]]
        pager=([("‹",f"report:{sid}:{month}:{sort}:{page-1}")] if page else [])+([("›",f"report:{sid}:{month}:{sort}:{page+1}")] if page+1<pages else [])
        if pager: rows.append(pager)
        rows += [[("Скачать CSV",f"csv:{sid}:{month}:{sort}:0")],[("‹ Выбрать месяц",f"months:{sid}:0"),("‹ Сервер",f"s:{sid}")]]
        await self.panel(text,keyboard(*rows),revision)
