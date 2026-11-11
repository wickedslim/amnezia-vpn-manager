"""Live server cards as Telegram HTML widgets (Bot API 10.4). Enabled by AWG_WIDGETS=1.

A widget needs neither a domain nor HTTPS, so the card also works on --bot-only installs.
Its code and state are not secret: only a server's name and aggregate numbers go in here,
never a host address, key, config or client name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message

from .domain import ManagerError
from .resources import Resources
from .views import client_status, rates

log = logging.getLogger(__name__)

MAX_CARDS = 10
MAX_STATE_BYTES = 1024
HISTORY_POINTS = 20
NAME_CHARS = 48
# aiogram drops empty form fields, so chat_data="" would leave the old numbers on screen.
BLANK_STATE = "{}"


# aiogram 3.31 implements Bot API 10.3, so the two methods the card needs are declared here.
class SendWidget(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    chat_data: str | None = None
    caption: str | None = None
    parse_mode: str | None = None
    disable_notification: bool | None = None
    reply_markup: InlineKeyboardMarkup | None = None


class EditMessageWidgetData(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "editMessageWidgetData"

    chat_id: int
    message_id: int
    chat_data: str | None = None


CARD_HTML = """<style>
body{margin:0;padding:.7em .85em .75em}
#t{display:flex;justify-content:space-between;align-items:baseline;gap:.6em}
#n{min-width:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#o{flex:none;font-size:.85em}
.m{color:var(--tg-theme-hint-color);font-size:.8em}
.bad{color:var(--tg-theme-destructive-text-color)}
#tb{display:flex;gap:3px;margin:.6em 0 .7em;padding:3px;border-radius:10px;background:var(--tg-theme-secondary-bg-color)}
#tb button{flex:1;min-width:0;margin:0;padding:.4em .2em;border:0;border-radius:8px;background:transparent;color:var(--tg-theme-text-color);font:inherit;font-size:.8em}
#tb button[aria-pressed=true]{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
#r{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5em;margin-bottom:.7em;text-align:center}
.g{position:relative;width:3.6em;height:3.6em;margin:0 auto .2em}
.g svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}
circle{fill:none;stroke-width:4;stroke:var(--tg-theme-secondary-bg-color)}
circle.a{stroke:var(--tg-theme-button-color);stroke-linecap:round;stroke-dasharray:0 100;transition:stroke-dasharray .6s}
.hot circle.a{stroke:var(--tg-theme-destructive-text-color)}
.no circle.a{stroke:transparent}
.v{position:absolute;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:.85em;font-weight:600;font-variant-numeric:tabular-nums}
.l{display:flex;justify-content:space-between;gap:.6em;font-size:.9em;line-height:1.55;font-variant-numeric:tabular-nums}
.l b{font-weight:600}
.big{font-size:1.6em;font-weight:600;font-variant-numeric:tabular-nums}
.hb{display:flex;align-items:flex-end;gap:2px;height:2.6em;margin:.6em 0 .3em}
.hb span{flex:1;min-height:2px;border-radius:1px;background:var(--tg-theme-button-color);transition:height .6s}
#ps,#pc{min-height:15.2em}
#u{margin-top:.55em}
@media (prefers-reduced-motion:reduce){circle.a,.hb span{transition:none}}
</style>
<div id=t><div id=n></div><div id=o></div></div>
<div id=b>
<div id=tb><button data-t=s>Сервер</button><button data-t=c>Клиенты</button></div>
<div id=ps><div id=r></div><div class="hb" id=h></div><div class=m id=hw></div><div id=vl></div></div>
<div id=pc><div class=big id=co></div><div class=m>клиентов в сети</div>
<div class="hb" id=q></div><div class=m id=qw></div><div id=cl></div></div>
<div class=m id=u></div>
</div>
<script>
var W=window.TelegramWidget,$=function(i){return document.getElementById(i)},S=null,R={},T='s';
try{T=localStorage.getItem('t')=='c'?'c':'s'}catch(x){}
function sz(n){var u=['Б','Кб','Мб','Гб','Тб'],i=0;n=Math.max(0,n||0);while(n>=1024&&i<4){n/=1024;i++}return(i?n.toFixed(1):Math.round(n))+' '+u[i]}
function rows(id,a){$(id).innerHTML='';a.forEach(function(r){var d=document.createElement('div'),x=document.createElement('span'),y=document.createElement('b');d.className='l';x.textContent=r[0];y.textContent=r[1];if(r[2])y.className='bad';d.appendChild(x);d.appendChild(y);$(id).appendChild(d)})}
['c','m','d'].forEach(function(k,i){
var g=document.createElement('div');
g.innerHTML='<div class=g><svg viewBox="0 0 36 36"><circle cx=18 cy=18 r=15.915></circle><circle class=a cx=18 cy=18 r=15.915></circle></svg><div class=v></div></div><div class=m>'+['CPU','Память','Диск'][i]+'</div>';
$('r').appendChild(g);R[k]=g});
['h','q'].forEach(function(id){for(var i=0;i<20;i++){var b=document.createElement('span');b.style.height='0';$(id).appendChild(b)}});
function bars(id,h){var B=$(id).children;h=h||[];for(var i=0;i<B.length;i++)B[i].style.height=(h[i-B.length+h.length]||0)+'%'}
function ring(k,v){var g=R[k],n=typeof v=='number';
g.querySelector('circle.a').style.strokeDasharray=(n?v:0)+' 100';
g.querySelector('.v').textContent=n?Math.round(v)+'%':'—';
g.classList.toggle('hot',n&&v>=90);g.classList.toggle('no',!n)}
function tab(){['s','c'].forEach(function(k){$('p'+k).hidden=k!=T});
[].forEach.call($('tb').children,function(b){b.setAttribute('aria-pressed',b.getAttribute('data-t')==T)})}
[].forEach.call($('tb').children,function(b){b.addEventListener('click',function(){var t=b.getAttribute('data-t');if(t==T)return;T=t;try{localStorage.setItem('t',T)}catch(x){}
try{W.HapticFeedback.selectionChanged()}catch(x){}tab()})});
function age(){if(!S)return;var a=Date.now()/1e3-S.t,o=a>180;
$('u').textContent=o?'Данные устарели':a<60?'Обновлено только что':'Обновлено '+Math.floor(a/60)+' мин назад';
$('u').classList.toggle('bad',o)}
function render(){S=null;try{S=JSON.parse(W.chatData)}catch(x){}
if(S&&!S.n)S=null;
$('b').hidden=!S;
if(!S){$('n').textContent='Карточка отключена';$('o').textContent='';$('o').className='';W.ready();return}
var w=S.w?'за '+S.w+' мин':'';
$('n').textContent=S.n;
$('o').textContent=S.k?'Доступен':'Нет связи';
$('o').className=S.k?'':'bad';
ring('c',S.c);ring('m',S.m);ring('d',S.x);
bars('h',S.h);$('hw').textContent=w&&((S.h||[]).some(Number)?'Трафик ':'Трафика не было ')+w;
$('co').textContent=S.on+' из '+S.all;
var top=Math.max.apply(null,(S.q||[]).concat([1]));bars('q',(S.q||[]).map(function(v){return Math.round(v/top*100)}));
$('qw').textContent=w&&((S.q||[]).some(Number)?'В сети '+w+', максимум '+top:'Никого не было в сети '+w);
rows('cl',[['Были в сети за сутки',S.ca],['Ни разу не подключались',S.cn],['На паузе',S.cp],['Срок истекает за 3 дня',S.ce,S.ce>0],['Лимит почти исчерпан',S.cl,S.cl>0]]);
rows('vl',[['Сейчас',S.k?'↓ '+sz(S.d)+'/с ↑ '+sz(S.up)+'/с':'—'],['Пик '+w,sz(S.pk)+'/с'],['Сегодня','↓ '+sz(S.td)+' ↑ '+sz(S.tu)],['Этот месяц','↓ '+sz(S.md)+' ↑ '+sz(S.mu)]]);
tab();age();W.ready()}
W.onEvent('chatDataChanged',render);
setInterval(age,3e4);
render();
</script>"""


def speeds(samples: list[dict]) -> list[float]:
    """Total throughput per interval in bytes per second; a counter reset counts as zero."""
    result = []
    for first, last in zip(samples, samples[1:]):
        seconds = last["ts"] - first["ts"]
        if seconds > 0:
            result.append((max(0, last["rx"] - first["rx"]) + max(0, last["tx"] - first["tx"])) / seconds)
    return result


def history(samples: list[dict], points: int = HISTORY_POINTS) -> list[int]:
    """Total throughput per interval, scaled to 0-100 so the state stays small."""
    values = speeds(samples)[-points:]
    top = max(values, default=0)
    return [round(speed / top * 100) for speed in values] if top else [0] * len(values)


def percent(part: dict) -> int:
    return round(part["used"] / part["total"] * 100)


def card_state(store, resources, sid: int, now: float | None = None) -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = time.time() if now is None else now
    health = store.get_pref(f"health:{sid}") or {}
    clients = store.clients(sid)
    samples = store.samples(sid, limit=HISTORY_POINTS + 1)
    online = health.get("ok")
    # Same direction naming as the server screen: sample tx is what devices receive.
    up, down = rates(samples)
    local = datetime.fromtimestamp(now, ZoneInfo("Europe/Samara"))
    totals = store.server_totals(sid, local.strftime("%Y-%m-%d"), local.strftime("%Y-%m"))
    paused = store.get_pref(f"paused_clients:{sid}") or {}
    soon = now + 3 * 86400
    state = {
        "n": store.server(sid).name[:NAME_CHARS],
        "k": int(bool(online)),
        "on": sum(client_status(c)[1] == "В сети" for c in clients),
        "all": len(clients),
        "d": round(down) if online else 0,
        "up": round(up) if online else 0,
        "h": history(samples),
        "t": int(health.get("updated") or now),
        # Client monitoring: counts only, never names.
        "ca": sum(max(c["handshake"], c.get("last_handshake") or 0) >= now - 86400 for c in clients),
        "cn": sum(client_status(c)[1] == "Нет истории подключения" for c in clients),
        "cp": len(paused),
        "ce": sum(c["expires"] is not None and c["expires"] <= soon for c in clients),
        "cl": sum(bool(c["traffic_limit"]) and c["total_rx"] + c["total_tx"] >= 0.8 * c["traffic_limit"] for c in clients),
        "q": [int(s.get("online") or 0) for s in samples[1:]][-HISTORY_POINTS:],
        "td": totals["day_down"], "tu": totals["day_up"],
        "md": totals["month_down"], "mu": totals["month_up"],
        "pk": round(max(speeds(samples), default=0)),
        "w": round((samples[-1]["ts"] - samples[0]["ts"]) / 60) if len(samples) > 1 else 0,
    }
    measured = resources.get(sid)
    if measured and not measured.get("stale"):
        if measured.get("cpu") is not None:
            state["c"] = round(measured["cpu"])
        state["m"] = percent(measured["memory"])
        state["x"] = percent(measured["disk"])
    return state


def encode_state(state: dict) -> str:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode()) > MAX_STATE_BYTES:
        payload = json.dumps({**state, "h": [], "q": []}, ensure_ascii=False, separators=(",", ":"))
    return payload


class WidgetCards:
    """Keeps one self-updating card per user and server."""

    PREF = "widget_cards"
    sample_gap = 5.2  # Resources needs two reads at least 5 s apart to compute CPU.

    def __init__(self, bot, store, service, access):
        self.bot, self.store, self.access = bot, store, access
        self.resources = Resources(store, service)
        self.lock = asyncio.Lock()

    def items(self) -> list[dict]:
        return self.store.get_pref(self.PREF, []) or []

    def is_open(self, user: int, sid: int) -> bool:
        return any(i["u"] == user and i["s"] == sid for i in self.items())

    def save(self, items: list[dict]) -> None:
        self.store.set_pref(self.PREF, items)

    async def open(self, user: int, sid: int) -> None:
        self.store.server(sid)  # an unknown server stops here
        await self.resources.refresh()
        async with self.lock:
            items = self.items()
            previous = next((i for i in items if i["u"] == user and i["s"] == sid), None)
            if previous is None and len(items) >= MAX_CARDS:
                raise ManagerError("Открыто слишком много живых карточек. Уберите одну из них.")
            try:
                message = await self.bot(SendWidget(
                    chat_id=user,
                    html=CARD_HTML,
                    chat_data=encode_state(card_state(self.store, self.resources, sid)),
                    disable_notification=True,
                ))
            except TelegramAPIError as exc:
                log.warning("Widget card was not sent: server=%d type=%s", sid, type(exc).__name__)
                raise ManagerError("Telegram не принял карточку. Проверьте журнал службы.") from exc
            self.save([i for i in items if i is not previous] + [{"u": user, "s": sid, "m": message.message_id}])
        if previous:
            await self.delete(previous)

    async def close(self, user: int, sid: int) -> None:
        async with self.lock:
            items = self.items()
            found = [i for i in items if i["u"] == user and i["s"] == sid]
            self.save([i for i in items if i not in found])
        for item in found:
            await self.delete(item)

    async def delete(self, item: dict) -> None:
        try:
            await self.bot.delete_message(item["u"], item["m"])
        except TelegramAPIError as exc:
            log.info("Widget card was not deleted: type=%s", type(exc).__name__)

    async def update_all(self) -> None:
        if not self.items():
            return
        await self.resources.refresh()
        await asyncio.sleep(self.sample_gap)
        await self.resources.refresh()
        async with self.lock:
            items = self.items()
            known = {s.id for s in self.store.servers()}
            keep = []
            for index, item in enumerate(items):
                try:
                    if item["s"] not in known or not self.access.permits(item["u"], item["s"]):
                        await self.revoke(item)
                    elif await self.push(item):
                        keep.append(item)
                except TelegramRetryAfter:
                    keep.extend(items[index:])
                    log.warning("Widget card updates postponed: flood wait")
                    break
                except Exception as exc:
                    keep.append(item)
                    log.warning("Widget card was not updated: server=%d type=%s", item["s"], type(exc).__name__)
            self.save(keep)

    async def push(self, item: dict) -> bool:
        """Returns False when the card no longer exists and should be forgotten."""
        state = encode_state(card_state(self.store, self.resources, item["s"]))
        try:
            await self.bot(EditMessageWidgetData(chat_id=item["u"], message_id=item["m"], chat_data=state))
        except TelegramForbiddenError:
            return False
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "not modified" in text:
                return True
            if "not found" in text or "can't be edited" in text:
                return False
            raise
        return True

    async def revoke(self, item: dict) -> None:
        """Blanks a card whose server is gone or whose user lost access to it."""
        try:
            await self.bot(EditMessageWidgetData(chat_id=item["u"], message_id=item["m"], chat_data=BLANK_STATE))
        except TelegramRetryAfter:
            raise
        except TelegramAPIError as exc:
            log.info("Widget card was not cleared: type=%s", type(exc).__name__)
