#!/usr/bin/env python3
"""Offline preview using the production UI renderers; no Telegram credentials needed."""
import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "awg"))
from manager.domain import Peer, Server, WGConfig
from manager.store import Store
from manager.ui import UI


class PreviewBot:
    def __init__(self):
        self.screen = None

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.screen = {"text": text, "rows": [[{"label": b.text, "route": b.callback_data} for b in row] for row in reply_markup.inline_keyboard] if reply_markup else []}
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, *, text, reply_markup=None, **kwargs):
        await self.send_message(1, text, reply_markup)


async def build():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory))
        servers = [store.add_server(Server(0,"Europe-02",host="203.0.113.10")), store.add_server(Server(0,"Europe-01",host="203.0.113.20"))]
        now = int(time.time())
        peers = [Peer(f"demo-{i}",name,f"10.8.1.{i+2}/32","",handshake=now - delta,received=rx,sent=tx) for i,(name,delta,rx,tx) in enumerate([
            ("myRouter",32,2_700_000_000,18_100_000_000),
            ("office401",76,800_000_000,4_200_000_000),
            ("office401",7200,600_000_000,1_700_000_000),
            ("papa",400000,120_000_000,800_000_000),
            ("sklad",now,0,0),
        ])]
        snapshots = {}
        for server in servers:
            selected = peers if server.id == servers[0].id else peers[:2]
            store.sync(server.id,selected,"demo-server")
            snapshots[server.id] = SimpleNamespace(config=WGConfig("",{},selected),timestamp=now)
            with store.transaction() as conn:
                for i, online in enumerate([1,2,2,1,3,2,4,3,2,2,3,2]):
                    conn.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)", (server.id,now-(12-i)*60,20_000_000_000+i*800000,30_000_000_000+i*2400000,online))
                conn.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)",(server.id,now,20_010_000_000,30_030_000_000,2))
        async def snapshot(sid,*args,**kwargs):
            return snapshots[sid]
        async def overview(*args,**kwargs):
            return snapshots
        bot = PreviewBot()
        ui = UI(bot,store,SimpleNamespace(snapshot=snapshot,overview=overview),123)
        screens = {}
        await ui.home()
        screens["home"] = bot.screen
        for server in servers:
            await ui.server(server.id)
            screens[f"s:{server.id}"] = bot.screen
            await ui.clients(server.id)
            screens[f"list:{server.id}:0"] = bot.screen
            await ui.months(server.id)
            screens[f"months:{server.id}:0"] = bot.screen
            month = datetime.now(ZoneInfo("Europe/Samara")).strftime("%Y-%m")
            for sort in ("total", "rx", "tx"):
                await ui.report(server.id, month, sort)
                screens[f"report:{server.id}:{month}:{sort}:0"] = bot.screen
            for client in store.clients(server.id):
                store.set_policy(server.id,client["id"],now+7*86400,30_000_000_000)
                await ui.client(server.id,client["id"])
                screens[f"c:{server.id}:{client['id']}"] = bot.screen
        await ui.begin({"kind":"server","step":"name"})
        screens["newserver"] = bot.screen
        await ui.begin({"kind":"client","sid":servers[0].id,"step":"name"})
        screens[f"newclient:{servers[0].id}"] = bot.screen
        store.close()
        return screens


HTML = '''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amnezia · Новый интерфейс бота</title>
<style>
:root{color-scheme:dark;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a1018;color:#edf2f7}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 10% 0%,#15353d88,transparent 50%)}
main{max-width:1120px;margin:auto;padding:65px 28px;display:grid;grid-template-columns:1fr 440px;gap:80px;align-items:start}
.eyebrow{color:#6edcbe;font-size:11px;letter-spacing:3px;font-weight:700;text-transform:uppercase}h1{font-size:54px;line-height:1.08;font-weight:600;letter-spacing:-2px;margin:22px 0}h1 span{color:#6edcbe}
.lead{color:#a6b4c3;line-height:1.7;font-size:17px;max-width:440px}.links{display:flex;flex-direction:column;gap:9px;margin:35px 0}.links button{background:none;border:1px solid #263547;border-radius:12px;color:#cbd7e4;text-align:left;padding:15px 18px;cursor:pointer;font-size:14px}.links button:hover,.links button.active{border-color:#5cc7aa;color:#77e3c3;background:#163029}
.note{font-size:12px;color:#718398;line-height:1.7;max-width:400px}.phone{border:1px solid #2b3a48;border-radius:25px;background:#111e29;overflow:hidden;box-shadow:0 35px 100px #0008}.top{padding:18px 20px;display:flex;align-items:center;gap:12px;background:#1c2b38;border-bottom:1px solid #2c3c48}.avatar{display:grid;place-items:center;background:linear-gradient(135deg,#71dcc0,#328877);border-radius:50%;width:40px;height:40px;color:#09221b;font-size:20px;font-weight:700}.top b{font-size:15px}.top small{display:block;color:#8aa4b9;margin-top:3px;font-size:12px}.chat{padding:18px 13px 24px;min-height:610px;background:radial-gradient(ellipse at top,#1d374144,transparent)}.bubble{background:#253745;border-radius:15px 15px 15px 4px;padding:17px 16px;color:#edf2f8;font-size:14px;line-height:1.65;overflow-wrap:anywhere}.bubble b{font-weight:650}.bubble code{color:#76d4e5;font-family:ui-monospace,monospace;font-size:13px}.time{text-align:right;color:#91a5b4;font-size:10px;margin-top:5px}.keys{display:flex;flex-direction:column;gap:5px;margin-top:6px}.row{display:flex;gap:5px}.row button{flex:1;min-width:0;padding:12px 5px;border:0;border-radius:7px;color:#e5edf4;background:#354a5b;font-family:inherit;font-size:13px;font-weight:600;line-height:1.35;cursor:pointer}.row button:hover{background:#436176}.footer{text-align:center;padding:13px;color:#728a9c;background:#1c2b38;font-size:12px}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#2c4b43;padding:15px 22px;border-radius:14px;font-size:13px;display:none;max-width:90vw;box-shadow:0 8px 30px #0006}
@media(max-width:850px){main{grid-template-columns:1fr;gap:25px;padding:32px 18px}.intro{max-width:500px;margin:auto}h1{font-size:42px}.phone{max-width:440px;width:100%;margin:auto}.links{flex-direction:row;flex-wrap:wrap;margin:22px 0}.links button{padding:10px 12px}.lead{font-size:15px}.chat{min-height:450px}}
</style><main><section class="intro"><div class="eyebrow">Amnezia Manager · v2</div><h1>Все серверы.<br><span>Одно понятное меню.</span></h1><p class="lead">Новая навигация, живая статистика и спокойный чат — без закреплений и потерянных шагов.</p><div class="links"><button data-go="home">01 · Обзор серверов</button><button data-go="s:1">02 · Метрики сервера</button><button data-go="list:1:0">03 · Список клиентов</button><button data-go="c:1:1">04 · Карточка клиента</button><button data-go="newserver">05 · Подключение сервера</button></div><p class="note">Интерактивный предпросмотр на демонстрационных данных. Тексты и кнопки получены из кода нового бота. В Telegram цвета и форма элементов зависят от вашей темы. Сеть и реальные серверы здесь не используются.</p></section><section class="phone"><div class="top"><div class="avatar">A</div><div><b>AmneziaVPN</b><small>бот · управление серверами</small></div></div><div class="chat"><div class="bubble"><div id="text"></div><div class="time">сейчас ✓✓</div></div><div class="keys" id="keys"></div></div><div class="footer">/servers · обзор &nbsp; /start · продолжить &nbsp; /cancel · отменить</div></section></main><div class="toast" id="toast"></div><script>
const screens=__SCREENS__;
function show(route){route=route.replace(/^sr:/,'s:').replace(/^cr:/,'c:').replace(/^lr:/,'list:');if(route==='refreshhome')route='home';const s=screens[route];if(!s){const t=document.querySelector('#toast');t.textContent='Это предпросмотр. Действие будет доступно в Telegram после установки.';t.style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>t.style.display='none',2800);return;}document.querySelector('#text').innerHTML=s.text.replaceAll('\\n','<br>');const keys=document.querySelector('#keys');keys.replaceChildren();for(const row of s.rows){const el=document.createElement('div');el.className='row';for(const b of row){const btn=document.createElement('button');btn.textContent=b.label;btn.onclick=()=>show(b.route);el.append(btn);}keys.append(el);}document.querySelectorAll('[data-go]').forEach(b=>b.classList.toggle('active',b.dataset.go===route));}
document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>show(b.dataset.go));show('home');
</script></html>'''


if __name__ == "__main__":
    screens = asyncio.run(build())
    output = ROOT / "preview" / "index.html"
    output.parent.mkdir(exist_ok=True)
    output.write_text(HTML.replace("__SCREENS__", json.dumps(screens,ensure_ascii=False).replace("</", "<\\/")))
    print(output)
