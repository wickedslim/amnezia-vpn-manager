from __future__ import annotations
from .monitoring import notify_health

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from .config import Settings, initialize
from .domain import ManagerError
from .migration import migrate
from .service import VPNService
from .store import Store
from .ui import UI
from .views import e

log = logging.getLogger(__name__)


class InstanceLock:
    def __init__(self, token: str):
        digest = hashlib.sha256(token.encode()).hexdigest()[:24]
        self.path = os.path.join(tempfile.gettempdir(), f"amnezia-manager-{os.getuid()}-{digest}.lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise ManagerError("На этой машине уже запущен бот с таким токеном. Остановите лишнюю копию.") from exc
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ConflictGuard(BaseRequestMiddleware):
    def __init__(self, event: asyncio.Event):
        self.event = event

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramConflictError:
            self.event.set()
            raise


async def monitor(service: VPNService, bot: Bot, admin: int, interval: int):
    while True:
        try:
            notifications = await service.monitor_once()
            await notify_health(service.store, bot, admin)
            for sid, text in notifications:
                await bot.send_message(admin, f"{e(service.store.server(sid).name)}\n{e(text)}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Ошибка фонового обновления: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def run(settings: Settings, store: Store) -> int:
    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode="HTML"))
    service = VPNService(store)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(UI(bot, store, service, settings.admin_id).router)
    conflict = asyncio.Event()
    bot.session.middleware(ConflictGuard(conflict))
    background = polling = conflict_wait = None
    web_runner = None
    try:
        web_port = os.environ.get("AWG_WEB_PORT")
        if web_port:
            from .web import start_web
            web_runner = await start_web(settings, store, service, os.environ.get("AWG_WEB_HOST", "127.0.0.1"), int(web_port), bot=bot)
        web_url = os.environ.get("AWG_WEB_URL", "")
        if web_url:
            from urllib.parse import urlsplit
            parsed = urlsplit(web_url)
            if not web_runner or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Mini App: задайте AWG_WEB_PORT и публичный HTTPS-адрес AWG_WEB_URL")
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Панель VPN", web_app=WebAppInfo(url=web_url)))
        await bot.set_my_commands([
            BotCommand(command="servers", description="Обзор серверов"),
            BotCommand(command="start", description="Открыть меню или продолжить ввод"),
            BotCommand(command="cancel", description="Отменить текущий ввод"),
            BotCommand(command="id", description="Мой Telegram ID"),
        ])
        background = asyncio.create_task(monitor(service, bot, settings.admin_id, settings.poll_seconds))
        polling = asyncio.create_task(dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False))
        conflict_wait = asyncio.create_task(conflict.wait())
        await asyncio.wait([polling, conflict_wait], return_when=asyncio.FIRST_COMPLETED)
        if conflict.is_set():
            log.error("Другой экземпляр использует этот Telegram-токен. Остановите копию на другом сервере. Бот завершён с кодом 78.")
            with contextlib.suppress(RuntimeError):
                await dispatcher.stop_polling()
            return 78
        await polling
        return 0
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил токен. Проверьте настройки бота.")
        return 78
    finally:
        for task in (conflict_wait, polling, background):
            if task:
                task.cancel()
        for task in (conflict_wait, polling, background):
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if web_runner:
            await web_runner.cleanup()
        await service.close()
        await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AmneziaWG Telegram Manager v2")
    parser.add_argument("--init", action="store_true", help="создать настройки нового бота")
    parser.add_argument("--check", action="store_true", help="проверить настройки и импорт без запуска Telegram и SSH")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    os.umask(0o077)
    try:
        if args.init:
            initialize()
            return 0
        settings = Settings.load()
        with InstanceLock(settings.token):
            store = Store(settings.data_dir)
            try:
                migrate(store, settings.legacy_dir)
                with store.transaction() as conn:
                    for row in conn.execute("SELECT user_id,value FROM wizards").fetchall():
                        wizard = json.loads(row["value"])
                        if wizard.get("step") == "saving":
                            wizard["step"] = "review"
                            conn.execute("UPDATE wizards SET value=? WHERE user_id=?", (json.dumps(wizard), row["user_id"]))
                if args.check:
                    print(f"Настройки корректны. Серверов: {len(store.servers())}. Telegram и SSH не запускались.")
                    return 0
                return asyncio.run(run(settings, store))
            finally:
                store.close()
    except (ManagerError, ValueError) as exc:
        log.error("%s", exc)
        return 78
    except TelegramNetworkError:
        log.error("Telegram недоступен. Проверьте сеть сервера.")
        return 1
    except KeyboardInterrupt:
        return 0
