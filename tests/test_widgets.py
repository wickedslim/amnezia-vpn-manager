import json
import time
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestServer

from manager.access import Access
from manager.domain import Server
from manager.store import Store
from manager.ui import UI
from manager.widgets import CARD_HTML, MAX_CARDS, WidgetCards, card_state, encode_state, history

OWNER = 123
STRANGER = 456
MEASURED = {"updated": 1, "cpu": 12.4, "interval": 5, "stale": False,
            "memory": {"used": 512, "total": 1024}, "disk": {"used": 950, "total": 1000}}


class FakeTelegram:
    """A real HTTP endpoint, so the requests aiogram actually builds are what gets checked."""

    def __init__(self):
        self.calls, self.errors, self.next_id = [], {}, 100

    async def handle(self, request):
        method = request.match_info["method"]
        fields = dict(await request.post())
        self.calls.append((method, fields))
        if method in self.errors:
            return web.json_response({"ok": False, "error_code": 400, "description": self.errors[method]}, status=400)
        if method == "sendWidget":
            self.next_id += 1
            return web.json_response({"ok": True, "result": {
                "message_id": self.next_id, "date": 1, "chat": {"id": int(fields["chat_id"]), "type": "private"},
                "widget": {"html": fields["html"]}}})
        return web.json_response({"ok": True, "result": True})

    def of(self, method):
        return [fields for name, fields in self.calls if name == method]


class CardDocumentTests(unittest.TestCase):
    def test_fits_server_limits(self):
        data = CARD_HTML.encode()
        self.assertLessEqual(len(data), 32768)
        self.assertLessEqual(len(zlib.compress(data, 9)), 4096)
        self.assertNotIn(b"\0", data)

    def test_uses_only_what_the_widget_profile_keeps(self):
        for banned in ("<input", "<select", "<textarea", "<iframe", "<link", "fetch(", "XMLHttpRequest", "http://", "https://",
                       "vh;", "oklch", "color-mix", ":has(", "location.", "alert("):
            self.assertNotIn(banned, CARD_HTML)
        self.assertNotRegex(CARD_HTML, r"<script\s+src")


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = self.store.add_server(Server(0, "Офис", host="203.0.113.7"))
        self.store.set_pref(f"health:{self.server.id}", {"ok": True, "updated": 1000})
        self.telegram = FakeTelegram()
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.telegram.handle)
        self.http = TestServer(app)
        await self.http.start_server()
        session = AiohttpSession(api=TelegramAPIServer.from_base(str(self.http.make_url("")).rstrip("/")))
        self.bot = Bot("42:TEST", session=session)
        self.cards = WidgetCards(self.bot, self.store, SimpleNamespace(), Access(self.store, OWNER))
        self.cards.sample_gap = 0
        self.cards.resources = SimpleNamespace(refresh=AsyncMock(), get=lambda sid: MEASURED)

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.http.close()
        self.store.close()
        self.tmp.cleanup()

    def state(self):
        return json.loads(self.telegram.of("sendWidget")[-1]["chat_data"])

    def stored(self):
        return self.store.get_pref(WidgetCards.PREF, [])

    def test_history_scales_and_ignores_counter_reset(self):
        samples = [{"ts": 0, "rx": 0, "tx": 0}, {"ts": 10, "rx": 500, "tx": 500}, {"ts": 20, "rx": 750, "tx": 750}, {"ts": 30, "rx": 0, "tx": 0}]
        self.assertEqual(history(samples), [100, 50, 0])

    def test_state_is_compact_even_for_the_longest_name(self):
        state = {"n": "👨" * 48, "k": 1, "on": 9999, "all": 9999, "d": 10**15, "up": 10**15, "h": [100] * 20, "t": 2**40, "c": 100, "m": 100, "x": 100,
                 "ca": 9999, "cn": 9999, "cp": 9999, "ce": 9999, "cl": 9999, "q": [9999] * 20, "td": 10**15, "tu": 10**15,
                 "md": 10**15, "mu": 10**15, "pk": 10**15, "w": 9999}
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()), 1024)
        self.assertLessEqual(len(encode_state(state).encode()), 1024)

    def test_stale_measurement_is_not_presented_as_current(self):
        self.cards.resources.get = lambda sid: {**MEASURED, "stale": True}
        state = card_state(self.store, self.cards.resources, self.server.id, now=5)
        self.assertFalse({"c", "m", "x"} & set(state))

    def test_unreachable_server_shows_no_old_speed(self):
        self.store.set_pref(f"health:{self.server.id}", {"ok": False, "updated": 1000})
        state = card_state(self.store, self.cards.resources, self.server.id, now=5)
        self.assertEqual((state["k"], state["d"], state["up"]), (0, 0, 0))

    def test_client_monitoring_counts_without_names(self):
        from manager.domain import Peer
        now = 1_000_000
        self.store.sync(self.server.id, [Peer("k1", "Секретное имя", "10.8.1.2/32", "", handshake=now - 10, received=1, sent=1),
                                         Peer("k2", "b", "10.8.1.3/32", "", handshake=now - 3600),
                                         Peer("k3", "c", "10.8.1.4/32", "")], "srv")
        ids = {c["public_key"]: c["id"] for c in self.store.clients(self.server.id)}
        self.store.set_policy(self.server.id, ids["k2"], now + 3600, None)
        self.store.set_policy(self.server.id, ids["k1"], None, 1)
        self.store.set_pref(f"paused_clients:{self.server.id}", {"k9": {}})
        with patch("manager.views.time.time", return_value=now):
            state = card_state(self.store, self.cards.resources, self.server.id, now=now)
        self.assertEqual((state["all"], state["on"], state["ca"], state["cn"], state["cp"], state["ce"], state["cl"]), (3, 1, 2, 1, 1, 1, 1))
        self.assertNotIn("Секретное", json.dumps(state, ensure_ascii=False))
        self.assertNotIn("10.8.1", json.dumps(state))

    def test_traffic_totals_use_device_directions_for_today_and_month(self):
        from manager.domain import Peer
        now = 1_000_000
        with patch("manager.store.time.time", return_value=now):
            self.store.sync(self.server.id, [Peer("k1", "a", "10.8.1.2/32", "", received=0, sent=0)], "srv")
        with patch("manager.store.time.time", return_value=now + 60):
            self.store.sync(self.server.id, [Peer("k1", "a", "10.8.1.2/32", "", received=100, sent=900)], "srv")
        state = card_state(self.store, self.cards.resources, self.server.id, now=now + 60)
        totals = (state["td"], state["tu"], state["md"], state["mu"])
        self.assertEqual(sum(totals), 2000)
        self.assertEqual(state["td"], state["md"])
        self.assertGreater(state["td"], state["tu"])

    async def test_open_sends_card_without_server_secrets(self):
        await self.cards.open(OWNER, self.server.id)
        sent = self.telegram.of("sendWidget")[0]
        self.assertEqual(sent["chat_id"], str(OWNER))
        self.assertEqual(sent["html"], CARD_HTML)
        self.assertNotIn("caption", sent)  # the card itself shows the server name
        self.assertNotIn("203.0.113.7", sent["chat_data"])
        self.assertNotIn("reply_markup", sent)
        self.assertEqual((self.state()["m"], self.state()["x"], self.state()["c"]), (50, 95, 12))
        self.assertEqual(self.stored(), [{"u": OWNER, "s": self.server.id, "m": 101}])

    async def test_reopening_replaces_the_previous_card(self):
        await self.cards.open(OWNER, self.server.id)
        await self.cards.open(OWNER, self.server.id)
        self.assertEqual(self.stored(), [{"u": OWNER, "s": self.server.id, "m": 102}])
        self.assertEqual(self.telegram.of("deleteMessage")[0]["message_id"], "101")

    async def test_number_of_cards_is_capped(self):
        self.store.set_pref(WidgetCards.PREF, [{"u": OWNER, "s": 1000 + i, "m": i} for i in range(MAX_CARDS)])
        with self.assertRaisesRegex(Exception, "слишком много"):
            await self.cards.open(OWNER, self.server.id)
        self.assertEqual(self.telegram.of("sendWidget"), [])

    async def test_rejected_card_is_reported_and_not_remembered(self):
        self.telegram.errors["sendWidget"] = "Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE"
        with self.assertRaisesRegex(Exception, "не принял"):
            await self.cards.open(OWNER, self.server.id)
        self.assertEqual(self.stored(), [])

    async def test_update_edits_card_silently_in_place(self):
        await self.cards.open(OWNER, self.server.id)
        await self.cards.update_all()
        edit = self.telegram.of("editMessageWidgetData")[0]
        self.assertEqual((edit["chat_id"], edit["message_id"]), (str(OWNER), "101"))
        self.assertEqual(json.loads(edit["chat_data"])["n"], "Офис")
        self.assertEqual(len(self.telegram.of("sendWidget")), 1)
        self.assertEqual(len(self.stored()), 1)

    async def test_no_cards_means_no_extra_host_reads(self):
        await self.cards.update_all()
        self.cards.resources.refresh.assert_not_awaited()

    async def test_unchanged_card_is_kept(self):
        await self.cards.open(OWNER, self.server.id)
        self.telegram.errors["editMessageWidgetData"] = "Bad Request: message is not modified"
        await self.cards.update_all()
        self.assertEqual(len(self.stored()), 1)

    async def test_card_deleted_by_user_is_forgotten(self):
        await self.cards.open(OWNER, self.server.id)
        self.telegram.errors["editMessageWidgetData"] = "Bad Request: message to edit not found"
        await self.cards.update_all()
        self.assertEqual(self.stored(), [])

    async def test_temporary_failure_keeps_card(self):
        await self.cards.open(OWNER, self.server.id)
        self.telegram.errors["editMessageWidgetData"] = "Bad Request: something new"
        await self.cards.update_all()
        self.assertEqual(len(self.stored()), 1)

    async def test_revoked_user_gets_blanked_card_and_no_more_updates(self):
        self.store.set_pref(WidgetCards.PREF, [{"u": STRANGER, "s": self.server.id, "m": 77}])
        await self.cards.update_all()
        edits = self.telegram.of("editMessageWidgetData")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].get("chat_data"), "{}", "an omitted field would leave the old numbers on screen")
        self.assertEqual(self.stored(), [])

    async def test_close_removes_only_own_card(self):
        self.store.set_pref(WidgetCards.PREF, [{"u": OWNER, "s": self.server.id, "m": 1}, {"u": STRANGER, "s": self.server.id, "m": 2}])
        await self.cards.close(STRANGER, self.server.id)
        self.assertEqual([i["u"] for i in self.stored()], [OWNER])
        self.assertEqual(self.telegram.of("deleteMessage")[0]["message_id"], "2")


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = self.store.add_server(Server(0, "Офис", host="h"))
        snapshot = SimpleNamespace(config=SimpleNamespace(peers=[]), timestamp=time.time())
        self.ui = UI(SimpleNamespace(), self.store, SimpleNamespace(snapshot=AsyncMock(return_value=snapshot)), OWNER)
        self.ui.panel = AsyncMock()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buttons(self):
        markup = self.ui.panel.await_args_list[-1].args[1]
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_button_hidden_without_flag(self):
        await self.ui.server(self.server.id)
        self.assertNotIn(f"widget:{self.server.id}", self.buttons())

    async def test_button_opens_card_for_the_actor(self):
        opened = set()
        async def open_card(user, sid): opened.add((user, sid))
        async def close_card(user, sid): opened.discard((user, sid))
        self.ui.widgets = SimpleNamespace(open=AsyncMock(side_effect=open_card), close=AsyncMock(side_effect=close_card),
                                          is_open=lambda user, sid: (user, sid) in opened)
        await self.ui.server(self.server.id)
        self.assertIn(f"widget:{self.server.id}", self.buttons())
        await self.ui.callback(SimpleNamespace(data=f"widget:{self.server.id}"))
        self.ui.widgets.open.assert_awaited_once_with(OWNER, self.server.id)
        # The panel now offers to remove the card instead of a button under the card itself.
        self.assertIn(f"widgetoff:{self.server.id}", self.buttons())
        self.assertNotIn(f"widget:{self.server.id}", self.buttons())
        await self.ui.callback(SimpleNamespace(data=f"widgetoff:{self.server.id}"))
        self.ui.widgets.close.assert_awaited_once_with(OWNER, self.server.id)
        self.assertIn(f"widget:{self.server.id}", self.buttons())

    async def test_user_without_access_cannot_open_card(self):
        self.ui.widgets = SimpleNamespace(open=AsyncMock(), close=AsyncMock(), is_open=lambda user, sid: False)
        token = self.ui.actor.set(STRANGER)
        try:
            await self.ui.callback(SimpleNamespace(data=f"widget:{self.server.id}"))
        finally:
            self.ui.actor.reset(token)
        self.ui.widgets.open.assert_not_awaited()

    async def test_stale_button_without_flag_does_nothing(self):
        await self.ui.callback(SimpleNamespace(data=f"widget:{self.server.id}"))
        self.assertIn("устарела", self.ui.panel.await_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
