import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from manager.app import InstanceLock
from manager.domain import ManagerError, Peer, Server
from manager.store import Store
from manager.ui import AccessMiddleware, UI


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),edit_message_text=AsyncMock())
        self.service = SimpleNamespace(probe=AsyncMock(),snapshot=AsyncMock())
        self.ui = UI(self.bot,self.store,self.service,123)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self,text):
        return SimpleNamespace(text=text,delete=AsyncMock(),answer=AsyncMock())

    async def test_server_and_overview_use_client_directions(self):
        s=self.store.add_server(Server(0,"test",host="host"))
        with self.store.transaction() as c:
            for ts,rx,tx in [(100,100,1000),(105,200,2000)]:
                c.execute("INSERT INTO samples VALUES (?,?,?,?,?)",(s.id,ts,rx,tx,0))
        self.service.snapshot.return_value=SimpleNamespace(config=SimpleNamespace(peers=[]),timestamp=105)
        await self.ui.server(s.id)
        text=self.bot.edit_message_text.call_args.kwargs['text']
        self.assertIn('↓ На устройства 2.0 Кб',text)
        self.assertIn('↑ С устройств 200 Б',text)
        self.assertIn('Скорость: ↓ 200 Б/с  ↑ 20 Б/с',text)
        self.service.overview=AsyncMock(return_value={s.id:object()})
        await self.ui.home()
        self.assertIn('↓ 2.0 Кб · ↑ 200 Б',self.bot.edit_message_text.call_args.kwargs['text'])

    async def test_wizard_direct_password_does_not_require_extra_button(self):
        await self.ui.begin({"kind":"server","step":"name"})
        await self.ui.message(self.message("serv.host"))
        await self.ui.message(self.message("root@203.0.113.1:2222"))
        message = self.message("sensitive-password")
        await self.ui.message(message)
        message.delete.assert_awaited_once()
        wizard = self.store.wizard(123)
        self.assertEqual(wizard["step"],"endpoint")
        self.assertEqual(wizard["auth_type"],"password")
        self.assertEqual(wizard["name"],"serv.host")
        self.assertNotIn("sensitive-password",str(self.bot.edit_message_text.call_args_list))

    async def test_start_resumes_dialog(self):
        await self.ui.begin({"kind":"server","step":"name"})
        await self.ui.message(self.message("server"))
        await self.ui.start(self.message("/start"))
        self.assertEqual(self.store.wizard(123)["step"],"connect")

    async def test_cancel_clears_dialog(self):
        await self.ui.begin({"kind":"server","step":"name"})
        await self.ui.start(self.message("/cancel"))
        self.assertIsNone(self.store.wizard(123))

    async def test_servers_command_exits_wizard_to_overview(self):
        await self.ui.begin({"kind":"server","step":"name"})
        await self.ui.start(self.message("/servers"))
        self.assertIsNone(self.store.wizard(123))

    async def test_old_wizard_button_is_rejected(self):
        await self.ui.begin({"kind":"server","step":"name"})
        old = self.store.wizard(123)["nonce"]
        await self.ui.begin({"kind":"server","step":"name"})
        with self.assertRaises(ManagerError):
            await self.ui.wizard_action(["w",old,"cancel"])
        self.assertIsNotNone(self.store.wizard(123))

    async def test_back_restores_previous_step(self):
        await self.ui.begin({"kind":"server","step":"name"})
        await self.ui.message(self.message("server"))
        nonce = self.store.wizard(123)["nonce"]
        await self.ui.wizard_action(["w",nonce,"back"])
        self.assertEqual(self.store.wizard(123)["step"],"name")

    async def test_failed_probe_does_not_save_server(self):
        await self.ui.begin({"kind":"server","step":"review","name":"server","host":"host","port":22,"username":"root","auth_type":"password","password":"secret","endpoint":"host"})
        self.service.probe.side_effect = ManagerError("no connection")
        with self.assertRaises(ManagerError):
            await self.ui.save_wizard(self.store.wizard(123))
        self.assertEqual(self.store.servers(),[])
        self.assertEqual(self.store.wizard(123)["step"],"review")

    async def test_panel_reuses_message_without_pinning(self):
        await self.ui.panel("first",None)
        await self.ui.panel("second",None)
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()

    async def test_stale_slow_screen_cannot_replace_new_navigation(self):
        self.ui.revision = 2
        await self.ui.panel("stale",None,revision=1)
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()

    async def test_duplicate_names_have_distinct_short_callbacks(self):
        s = self.store.add_server(Server(0,"server",host="host"))
        self.store.sync(s.id,[Peer("one","same.name","10.0.0.2/32",""),Peer("two","same.name","10.0.0.3/32","")],"public")
        await self.ui.clients(s.id)
        markup = self.bot.send_message.call_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith("c:")]
        self.assertEqual(len(set(callbacks)),2)
        self.assertTrue(all(len(value.encode())<=64 for value in callbacks))

    async def test_access_rejected_before_handler(self):
        middleware = AccessMiddleware(123)
        handler = AsyncMock()
        event = SimpleNamespace(from_user=SimpleNamespace(id=999),chat=SimpleNamespace(id=999,type="private"))
        await middleware(handler,event,{})
        handler.assert_not_awaited()

    async def test_group_chat_rejected_even_for_admin(self):
        middleware = AccessMiddleware(123)
        handler = AsyncMock()
        event = SimpleNamespace(from_user=SimpleNamespace(id=123),chat=SimpleNamespace(id=-100,type="supergroup"))
        await middleware(handler,event,{})
        handler.assert_not_awaited()

    async def test_same_token_cannot_start_twice_locally(self):
        with InstanceLock("123:fake-token-for-test"):
            with self.assertRaises(ManagerError):
                with InstanceLock("123:fake-token-for-test"):
                    self.fail("duplicate instance")

    async def test_reissue_requires_confirmation_sends_file_and_can_redownload(self):
        s=self.store.add_server(Server(0,'server',host='test'))
        self.store.sync(s.id,[Peer('old-key','phone','10.0.0.2/32','')],'server-key')
        c=self.store.clients(s.id)[0]
        self.bot.send_document=AsyncMock()
        async def rotate(sid,cid,key):
            self.store.save_client_config(sid,key,'[Interface]\nPrivateKey = TEST\n')
            return self.store.client(sid,cid)
        self.service.reissue_client=AsyncMock(side_effect=rotate)
        await self.ui.callback(SimpleNamespace(data=f'reissue:{s.id}:{c["id"]}'))
        self.service.reissue_client.assert_not_awaited()
        markup=self.bot.send_message.call_args.kwargs['reply_markup']
        data=markup.inline_keyboard[0][0].callback_data
        await self.ui.callback(SimpleNamespace(data=data))
        self.service.reissue_client.assert_awaited_once_with(s.id,c['id'],'old-key')
        file=self.bot.send_document.call_args.args[1]
        self.assertEqual(file.data,b'[Interface]\nPrivateKey = TEST\n')
        await self.ui.callback(SimpleNamespace(data=data))
        self.assertEqual(self.service.reissue_client.await_count,1)
        await self.ui.callback(SimpleNamespace(data=f'conf:{s.id}:{c["id"]}'))
        self.assertEqual(self.bot.send_document.call_args.args[1].data,file.data)
