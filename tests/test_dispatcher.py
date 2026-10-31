import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from manager.app import ConflictGuard
from manager.store import Store
from manager.ui import UI


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in {"SendMessage","EditMessageText"}:
            return Message(message_id=100,date=datetime.now(timezone.utc),chat=Chat(id=123,type="private"),text=method.text)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        if False:
            yield b""


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.session = TelegramSession()
        self.bot = Bot("123456:FAKE_TOKEN_FOR_LOCAL_TESTS_ONLY",session=self.session)
        self.dispatcher = Dispatcher(disable_fsm=True)
        self.ui = UI(self.bot,self.store,SimpleNamespace(),123)
        self.dispatcher.include_router(self.ui.router)
        self.user = User(id=123,is_bot=False,first_name="Admin")

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.close()
        self.tmp.cleanup()

    def message(self,text,uid=123):
        return Message(message_id=10,date=datetime.now(timezone.utc),chat=Chat(id=uid,type="private"),from_user=User(id=uid,is_bot=False,first_name="User"),text=text)

    async def test_real_dispatcher_routes_start_callback_name_and_password(self):
        await self.dispatcher.feed_update(self.bot,Update(update_id=1,message=self.message("/start")))
        callback = CallbackQuery(id="test",from_user=self.user,chat_instance="test",message=self.message("menu"),data="newserver")
        await self.dispatcher.feed_update(self.bot,Update(update_id=2,callback_query=callback))
        for i,text in enumerate(["serv.host","root@203.0.113.1:22","secret-password"],3):
            await self.dispatcher.feed_update(self.bot,Update(update_id=i,message=self.message(text)))
        self.assertEqual(self.store.wizard(123)["step"],"endpoint")
        names = [type(call).__name__ for call in self.session.calls]
        self.assertEqual(names.count("AnswerCallbackQuery"),1)
        self.assertIn("DeleteMessage",names)
        self.assertNotIn("PinChatMessage",names)
        self.assertEqual(names.count("SendMessage"),1)

    async def test_real_dispatcher_rejects_unknown_admin(self):
        await self.dispatcher.feed_update(self.bot,Update(update_id=1,message=self.message("/start",uid=999)))
        self.assertEqual(self.session.calls,[])

    async def test_conflict_triggers_shutdown_event(self):
        import asyncio
        event = asyncio.Event()
        method = GetUpdates()
        request = AsyncMock(side_effect=TelegramConflictError(method=method,message="another getUpdates"))
        with self.assertRaises(TelegramConflictError):
            await ConflictGuard(event)(request,self.bot,method)
        self.assertTrue(event.is_set())
