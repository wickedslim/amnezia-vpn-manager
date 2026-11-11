#!/usr/bin/env python3
"""Writes an interactive test stand for the Telegram server card, with made-up data.

The card is the real widget code; the stand feeds it state the way the bot does. A browser
draws more than Telegram does and enforces none of its limits, so this checks behaviour
and layout only: python scripts/widget_preview.py /tmp/card-stand.html
"""
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "awg"))
from manager.widgets import CARD_HTML, MAX_STATE_BYTES  # noqa: E402

STUB = """<!doctype html><meta charset=utf-8><style>body{font:15px/1.35 system-ui,sans-serif;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color)}</style>
<script>window.TelegramWidget={chatData:null,h:{},onEvent(n,f){(this.h[n]=this.h[n]||[]).push(f)},fire(n){(this.h[n]||[]).forEach(f=>f())},ready(){}}</script>"""

PAGE = r"""<!doctype html>
<html lang="ru" data-theme="light">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Стенд карточки</title>
<style>
:root{--bg:#f4f4f7;--panel:#fff;--text:#16181d;--hint:#6b7280;--line:#dfe1e7;--accent:#3390ec;--chat:#9db4c4;--bubble:#fff;--danger:#e53935}
[data-theme=dark]{--bg:#141518;--panel:#1f2125;--text:#f1f2f4;--hint:#9aa0aa;--line:#33363d;--accent:#8774e1;--chat:#0f1a24;--bubble:#212121;--danger:#ff5c5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;max-width:980px;margin:0 auto;padding:16px}
h1{grid-column:1/-1;margin:0;font-size:20px}
h1 small{display:block;margin-top:2px;color:var(--hint);font-size:13px;font-weight:400}
section{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px}
h2{margin:18px 0 8px;font-size:13px;color:var(--hint);font-weight:600}
h2:first-child{margin-top:0}
.seg{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:6px}
button{min-height:44px;padding:0 10px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit;font:inherit;cursor:pointer}
button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
button:focus-visible,input:focus-visible{outline:2px solid var(--hint);outline-offset:1px}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;column-gap:10px;min-height:44px}
.row b{font-weight:600;font-variant-numeric:tabular-nums}
.row input[type=range]{grid-column:1/-1;width:100%;margin:0;accent-color:var(--accent)}
.check{display:flex;align-items:center;gap:10px;min-height:44px}
.check input{width:20px;height:20px;accent-color:var(--accent)}
.chat{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-height:440px;padding:14px;border-radius:12px;background:var(--chat)}
.bubble{width:100%;max-width:320px;overflow:hidden;border-radius:14px;background:var(--bubble);transition:max-width .25s}
iframe{display:block;width:100%;height:220px;border:0}
.keys{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;width:100%;max-width:320px;transition:max-width .25s}
.keys button{border:0;background:rgba(40,60,80,.45);color:#fff}
.note{margin:8px 0 0;color:var(--hint);font-size:13px}
.gone{margin:auto;text-align:center;color:#fff}
pre{margin:8px 0 0;padding:10px;overflow:hidden;white-space:pre-wrap;overflow-wrap:anywhere;border-radius:10px;background:var(--bg);font:12px/1.4 ui-monospace,monospace}
.over{color:var(--danger)}
#debug{grid-column:2}
@media (max-width:720px){main{grid-template-columns:minmax(0,1fr)}#view{order:-1}h1{order:-2}#debug{grid-column:auto}.chat{min-height:0}}
@media (max-width:720px) and (min-height:640px){#view{position:sticky;top:0;z-index:2;padding:8px}.chat{padding:8px}}
</style>
<main>
<h1>Стенд карточки сервера<small>Настоящий код виджета, вымышленные данные. Кнопки и пузырь сообщения — имитация Telegram.</small></h1>
<section id="ctl">
<h2>Готовые состояния</h2>
<div class="seg" id="presets">
<button data-p="normal">Обычное</button><button data-p="hot">Перегрузка</button><button data-p="down">Нет связи</button><button data-p="revoked">Доступ отозван</button>
</div>
<h2>Сервер</h2>
<div id="sliders"></div>
<label class="check"><input type="checkbox" id="live" checked>Живая симуляция, раз в 2 с (в боте раз в минуту)</label>
<h2>Карточка</h2>
<div class="seg"><button id="remove">Убрать карточку (кнопка в панели бота)</button></div>
<h2>Вид</h2>
<div class="seg" id="theme"><button data-v="light">Светлая</button><button data-v="dark">Тёмная</button></div>
<div class="seg" id="width" style="margin-top:6px"><button data-v="240">240 px</button><button data-v="320">320 px</button><button data-v="430">430 px</button></div>
</section>
<section id="view">
<div class="chat">
<div class="bubble" id="bubble"><iframe id="frame" title="Карточка"></iframe></div>
<div class="keys" id="keys" hidden></div>
<div class="gone" id="gone" hidden><p>Карточка убрана</p><button id="back">Живая карточка</button></div>
</div>
<p class="note" id="toast" role="status">Правок сообщения: 0</p>
</section>
<section id="debug">
<h2>Что бот отправил бы</h2>
<div class="row"><span>Состояние виджета</span><b id="bytes"></b></div>
<div class="row"><span>Код виджета, сжатый</span><b>__ZIP__ / 4096 байт</b></div>
<pre id="json"></pre>
</section>
</main>
<script>
const $=id=>document.getElementById(id),frame=$('frame');
const MAX=__MAX__,enc=new TextEncoder();
const FIELDS=[['cpu','CPU',0,100,1,'%'],['mem','Память',0,100,1,'%'],['disk','Диск',0,100,1,'%'],['on','Клиентов в сети',0,50,1,''],['down','Скорость ↓',0,100,.1,' Мб/с'],['up','Скорость ↑',0,100,.1,' Мб/с'],['age','Данные получены',0,10,1,' мин назад']];
const NORMAL={cpu:12,mem:47,disk:63,on:4,down:2.4,up:.3,age:0,ok:true,blank:false};
const PRESETS={normal:NORMAL,hot:{...NORMAL,cpu:94,mem:91,disk:96,on:37,down:78,up:21},down:{...NORMAL,ok:false,on:0,age:6},revoked:{...NORMAL,blank:true}};
const THEMES={light:{bg:'#fff',text:'#000',hint:'#707579',button:'#3390ec',second:'#e9e9ee',danger:'#e53935'},dark:{bg:'#212121',text:'#fff',hint:'#aaa',button:'#8774e1',second:'#3a3a3a',danger:'#ff5c5c'}};
let online=[3,3,4,4,5,4,4,6,7,6,5,5,4,4,5,4,4,3,4,4],S={...NORMAL},speeds=[.4,.7,.6,1.2,1.7,1.5,2.2,2.5,1.9,1.6,1.4,2.1,2.8,3.5,4,3,2.4,1.8,2,1.6],edits=0,preset='normal';
const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
const W=()=>frame.contentWindow.TelegramWidget;
function state(j){
 const k=S.ok?1:0,total=speeds.at(-1),top=Math.max(...speeds);
 const o={n:'Амстердам-1',k,on:S.on,all:50,d:k?Math.round((S.down+j)*1048576):0,up:k?Math.round(S.up*1048576):0,h:speeds.map(v=>top?Math.round(v/top*100):0),t:Math.floor(Date.now()/1e3-S.age*60),
  ca:Math.min(50,S.on+9),cn:3,cp:1,ce:S.on>30?2:0,cl:S.on>30?1:0,q:online.slice(),td:7.3*1073741824,tu:1.1*1073741824,md:184*1073741824,mu:31*1073741824,pk:Math.round(top*1048576),w:20};
 if(k)Object.assign(o,{c:clamp(Math.round(S.cpu+j),0,100),m:S.mem,x:S.disk});
 return o;
}
function publish(j=0){
 const text=S.blank?'{}':JSON.stringify(state(j)),w=W();
 if(!w)return;
 w.chatData=text;w.fire('chatDataChanged');edits++;
 const n=enc.encode(text).length;
 $('bytes').textContent=n+' / '+MAX+' байт';$('bytes').className=n>MAX?'over':'';
 $('json').textContent=text;$('toast').textContent='Правок сообщения: '+edits;
 fit();
}
function fit(){const d=frame.contentDocument;if(d&&d.documentElement)frame.style.height=clamp(d.documentElement.offsetHeight,50,600)+'px'}
function pressed(id,v,a='v'){document.querySelectorAll('#'+id+' button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset[a]===String(v))))}
function sync(){FIELDS.forEach(([k,,,,,u])=>{$('r-'+k).value=S[k];$('v-'+k).textContent=S[k]+u});pressed('presets',preset,'p')}
function theme(v){
 document.documentElement.dataset.theme=v;pressed('theme',v);
 const t=THEMES[v],s=frame.contentDocument.documentElement.style;
 Object.entries({bg:'bg',text:'text',hint:'hint',button:'button',second:'secondary-bg',danger:'destructive-text'}).forEach(([k,n])=>s.setProperty('--tg-theme-'+n+'-color',t[k]));
}
function width(v){$('bubble').style.maxWidth=$('keys').style.maxWidth=v+'px';pressed('width',v);setTimeout(fit,300)}
$('sliders').innerHTML=FIELDS.map(([k,l,a,b,s])=>`<label class="row"><span>${l}</span><b id="v-${k}"></b><input type="range" id="r-${k}" min="${a}" max="${b}" step="${s}"></label>`).join('');
FIELDS.forEach(([k])=>$('r-'+k).addEventListener('input',e=>{S[k]=+e.target.value;preset='';sync();publish()}));
$('presets').addEventListener('click',e=>{const p=e.target.dataset.p;if(!p)return;S={...PRESETS[p]};preset=p;sync();show(true);publish()});
$('theme').addEventListener('click',e=>{if(e.target.dataset.v)theme(e.target.dataset.v)});
$('width').addEventListener('click',e=>{if(e.target.dataset.v)width(e.target.dataset.v)});
function show(on){$('bubble').hidden=!on;$('gone').hidden=on;if(on)fit()}
$('remove').onclick=()=>{show(false);$('toast').textContent='Бот удалил сообщение и больше не обновляет карточку'};
$('back').onclick=()=>{show(true);publish()};
setInterval(()=>{if(!$('live').checked||$('bubble').hidden||!S.ok)return;speeds=[...speeds.slice(1),Math.max(0,S.down+S.up)*(.75+Math.random()*.5)];online=[...online.slice(1),S.on];publish(Math.round((Math.random()-.5)*6))},2000);
frame.addEventListener('load',()=>{theme('light');width(320);sync();publish()},{once:true});
frame.srcdoc=__DOC__;
</script>
</html>
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    document = json.dumps(STUB + CARD_HTML, ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE.replace("__ZIP__", str(len(zlib.compress(CARD_HTML.encode(), 9))))
            .replace("__MAX__", str(MAX_STATE_BYTES))
            .replace("__DOC__", document))
    Path(sys.argv[1]).write_text(page, encoding="utf-8")
    print(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
