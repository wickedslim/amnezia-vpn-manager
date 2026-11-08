import {dailyView} from './daily.js';
import {patchHTML,patchElement} from './dom.js';
import {applyPreferences,loadPreferences,beginPreferences,cancelPreferences,settingsScreen,changePreference,resetPreferences,savePreferences,motionAllowed} from './preferences.js';
const tg = window.Telegram?.WebApp;
const $ = s => document.querySelector(s);
const icons = {settings:'<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915" />   <circle cx="12" cy="12" r="3" />',refresh:'<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />   <path d="M21 3v5h-5" />   <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />   <path d="M8 16H3v5" />',server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M15 6.5h3M15 17.5h3"/>',grid:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',users:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.9"/><circle cx="9" cy="7" r="4"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',chart:'<path d="M3 3v18h18M7 14l4-5 4 3 6-8"/>',arrow:'<path d="m9 5 7 7-7 7"/>',back:'<path d="m15 5-7 7 7 7"/>',down:'<path d="M12 3v18m-7-7 7 7 7-7"/>',up:'<path d="M12 21V3m-7 7 7-7 7 7"/>',plus:'<path d="M12 5v14M5 12h14"/>',search:'<circle cx="10.5" cy="10.5" r="7.5"/><path d="m16 16 5 5"/>',sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1"/>',shield:'<path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7Z"/><path d="m8 12 3 3 5-6"/>',download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>'};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.grid}</svg>`;
const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => {let n=Number(value)||0,i=0;const u=['Б','Кб','Мб','Гб','Тб'];while(n>=1024&&i<4){n/=1024;i++}return `${n.toLocaleString('ru-RU',{maximumFractionDigits:i?1:0})} ${u[i]}`};
const splitSize = n => {const [a,b]=fmt(n).split(' ');return `${a}<span>${b}</span>`};
const ago = ts => !ts?'Не подключался': Date.now()/1000-ts<60?'Только что':Date.now()/1000-ts<3600?`${Math.floor((Date.now()/1000-ts)/60)} мин назад`:`${Math.floor((Date.now()/1000-ts)/3600)} ч назад`;
const currentMonth = () => new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Samara',year:'numeric',month:'2-digit'}).format(new Date());
const state = {updates:null,daily:null,dailyDay:null,dailyReturn:'#/stats',data:null,report:null,route:[],filter:'all',serverFilter:'all',search:'',sort:'total',metricMode:localStorage.getItem('amnezia-metrics')||'speed',speedtests:{},reboots:{},clientReturn:'#/clients',clientScroll:0,month:currentMonth(),busy:false,loading:false,revision:0,settingsReturn:'#/',admins:null,audit:null};
let active=true,timer,toastTimer;
function theme(){applyPreferences();safeArea()}
function safeArea(){for(const [key,obj] of [['safe',tg?.safeAreaInset],['content-safe',tg?.contentSafeAreaInset]])for(const edge of ['top','bottom','left','right'])document.documentElement.style.setProperty(`--tg-${key}-area-inset-${edge}`,`${obj?.[edge]||0}px`)}
function haptic(kind='selectionChanged'){tg?.HapticFeedback?.[kind]?.()}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,3500)}
async function api(path,options={},timeoutMs=20000){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),timeoutMs);try{const response=await fetch(`/api${path}`,{...options,signal:controller.signal,headers:{'Authorization':`tma ${tg?.initData||''}`,'Content-Type':'application/json',...options.headers}});if(!response.ok){let body;try{body=await response.json()}catch{}throw new Error(body?.error||'Не удалось получить данные. Повторите попытку.')}return await response.json()}catch(error){if(error.name==='AbortError')throw new Error('Сервер не ответил вовремя. Обновите данные перед повтором действия: операция могла выполниться.');throw error}finally{clearTimeout(timeout)}}
function download(name,text,type='text/plain'){const u=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=u;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),30000)}
function notice(text,error=false){$('#notice').textContent=text;$('#notice').classList.toggle('error',error);$('#notice').hidden=!text}
const total = s => s.rx+s.tx;
const servers = () => state.data?.servers||[];
const server = id => servers().find(s=>s.id===Number(id));
const allClients = () => servers().flatMap(s=>s.clients.map(c=>({...c,sid:s.id,server:s.name,reachable:s.ok})));
function hasHistory(c){return Boolean(c.has_history||c.last_handshake||c.handshake||c.rx||c.tx)}
function status(c){if(c.paused)return 'Доступ приостановлен';if(c.status_known===false)return 'Статус неизвестен';return c.online?'В сети':hasHistory(c)?'Не в сети':'Нет истории подключения'}
function statusClass(c){if(c.paused)return 'off';return c.status_known===false?'off':c.online?'':hasHistory(c)?'error':'off'}
function empty(title,text,action=''){return `<div class="surface empty">${icon('shield')}<h2>${esc(title)}</h2><p>${esc(text)}</p>${action}</div>`}
function back(href,label){return `<a class="back-link" href="${href}">${icon('back')}${esc(label)}</a>`}
function heading(title,subtitle='',action=''){return `<div class="page-heading"><div><h1>${esc(title)}</h1>${subtitle?`<p class="subtitle">${esc(subtitle)}</p>`:''}</div>${action}</div>`}
function trafficMetrics(rx,tx){return `<div class="server-metrics"><dl><dt>↓ На устройства</dt><dd>${fmt(rx)}</dd></dl><dl><dt>↑ С устройств</dt><dd>${fmt(tx)}</dd></dl><dl><dt>Σ Всего</dt><dd class="accent">${fmt(rx+tx)}</dd></dl></div>`}
function liveMetrics(s){return `<div class="server-metrics"><dl><dt>↓ На устройства</dt><dd class="accent">${s.ok&&!s.stale?fmt(s.rate_rx)+'/с':'—'}</dd></dl><dl><dt>↑ С устройств</dt><dd>${s.ok&&!s.stale?fmt(s.rate_tx)+'/с':'—'}</dd></dl><dl><dt>Σ Трафик</dt><dd>${fmt(total(s))}</dd></dl></div>`}
function resourceCard(s){const r=s.resources,stale=r?.stale||!s.ok;const gauge=(key,label,value,detail)=>{const valid=Number.isFinite(value),n=valid?Math.max(0,Math.min(100,value)):0;return `<div class="resource-gauge" data-resource="${key}" data-value="${valid?n:''}"><div class="resource-ring" role="img" aria-label="${esc(label)}: ${valid?n.toLocaleString('ru-RU',{maximumFractionDigits:1})+'%':'нет замера'}"><svg viewBox="0 0 100 100" aria-hidden="true"><circle class="ring-track" cx="50" cy="50" r="40"/><circle class="ring-fill" cx="50" cy="50" r="40" pathLength="100" stroke-dasharray="100 100" stroke-dashoffset="${100-n}"/></svg><b>${valid?n.toLocaleString('ru-RU',{maximumFractionDigits:1}):'—'}${valid?'<small>%</small>':''}</b></div><h3>${label}</h3><p>${detail}</p></div>`};return `<section class="surface chart-card resources-card${stale?' resources-stale':''}"><div class="hero-top"><h2>Ресурсы сервера</h2><small>${r?stale?'Нет свежих данных':r.interval?'За '+r.interval+' сек':'Новый замер':'Нет замера'}</small></div><div class="resource-grid">${gauge('cpu','Процессор',r?.cpu,r?.cpu==null?'Нужны два замера':'Все ядра')}${gauge('memory','Память',r?100*r.memory.used/r.memory.total:null,r?`${fmt(r.memory.used)}<span>из ${fmt(r.memory.total)}</span>`:'Нет данных')}${gauge('disk','Диск',r?100*r.disk.used/r.disk.total:null,r?`${fmt(r.disk.used)}<span>из ${fmt(r.disk.total)}</span>`:'Нет данных')}</div><p class="footnote">Весь VPS · диск /. ЦП — средняя между замерами; память — без освобождаемого кеша.${r?` Замер: ${ago(r.updated).toLowerCase()}.`:' Данные появятся после успешного подключения к серверу.'}${stale&&r?' Показаны последние успешные значения.':''}</p></section>`}
function hostTrafficCard(s){const h=s.host_traffic;if(!h)return '';return `<section class="surface chart-card host-traffic"><h2>Весь трафик VPS</h2><div class="big-number">${splitSize(h.rx+h.tx)}</div><div class="server-metrics"><dl><dt>↓ VPS получил</dt><dd>${fmt(h.rx)}</dd></dl><dl><dt>↑ VPS отправил</dt><dd>${fmt(h.tx)}</dd></dl><dl><dt>Интерфейс</dt><dd>${esc(h.interface)}</dd></dl></div><p class="footnote">Системные счётчики с ${new Date(h.since*1000).toLocaleDateString('ru-RU',{timeZone:'Europe/Samara'})} + дальнейший учёт. Включает VPN и остальной трафик VPS. Более ранней истории на сервере нет. Обновление раз в минуту. Замер: ${ago(h.updated)}.${h.stale?' Нет свежих данных; показано последнее значение.':''}</p></section>`}
let changelogReturn=null;
function changelogHTML(text){return String(text).split('\n').map(line=>{const value=esc(line.replace(/\*\*/g,''));return /^#{1,3} /.test(line)?`<h3>${value.replace(/^#{1,3} /,'')}</h3>`:/^- /.test(line)?`<p class="changelog-item">${value.slice(2)}</p>`:line.trim()?`<p>${value}</p>`:''}).join('')}
async function openChangelog(){const d=$('#modal');changelogReturn=d.open?{nodes:[...d.childNodes],progress:d.dataset.updateProgress,focus:document.activeElement}:null;d.replaceChildren();delete d.dataset.updateProgress;d.dataset.changelog='true';d.innerHTML='<h2>Что нового?</h2><div class="changelog-content" role="region" aria-label="Changelog"><p>Загружаю изменения…</p></div><button type="button" class="secondary full" data-action="changelog-back">Назад</button>';if(!d.open)d.showModal();try{const text=state.updates?.release?.notes||(await api('/updates/changelog')).text;if(d.dataset.changelog==='true')$('.changelog-content').innerHTML=changelogHTML(text)}catch(e){if(d.dataset.changelog==='true')$('.changelog-content').textContent='Не удалось загрузить список изменений. Повторите позже.'}}
function closeChangelog(){const d=$('#modal');delete d.dataset.changelog;if(changelogReturn){d.replaceChildren(...changelogReturn.nodes);if(changelogReturn.progress)d.dataset.updateProgress=changelogReturn.progress;changelogReturn.focus?.focus();changelogReturn=null;if(d.dataset.updateProgress==='true')patchHTML(d,updateProgress())}else d.close()}
const updatePhases={queued:'Подготовка',downloading:'Скачивание релиза',checking:'Проверка файлов',installing:'Резервная копия и установка',verifying:'Проверка запуска',complete:'Обновление завершено',failed:'Не удалось обновить',interrupted:'Обновление прервано'};
const updateActive=()=>['queued','downloading','checking','installing','verifying'].includes(state.updates?.job?.phase);
let updatePoll,updateLoading=false;
let updateCheckResult='';
function updateSection(){if(!state.data?.access?.owner)return '';const u=state.updates,r=u?.release,j=u?.job;return `<section class="surface settings-section update-section"><div class="hero-top"><h2>Обновления</h2>${r?'<span class="update-badge">Новая версия</span>':''}</div><p>${u?.error?'Не удалось проверить GitHub. Повторите позже.':r?`Доступна версия <b>${esc(r.version)}</b>`:u?.checked?'Установлена актуальная версия':'Проверьте наличие новой версии'}</p><button type="button" class="text-button whats-new" data-action="changelog">Что нового?</button><p class="footnote">Установлена ${esc(state.data.build?.version||'—')}${u?.checked?' · проверено '+new Date(u.checked*1000).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'}):''}</p><label class="motion-row"><span><b>Уведомлять о новых версиях</b><small>Проверка при открытии панели, не чаще раза в 6 часов. Включая бета-версии.</small></span><input id="update-notifications" type="checkbox" role="switch" ${u?.enabled!==false?'checked':''}></label><div class="toolbar"><button type="button" class="secondary" data-action="update-check" ${updateLoading?'disabled':''}>${updateLoading?'<span class="speed-spinner" aria-hidden="true"></span> Проверяю…':'Проверить'}</button>${r||updateActive()?`<button type="button" class="primary" data-action="update-open">${updateActive()?'Ход обновления':'Обновить'}</button>`:''}</div><p class="footnote" role="status">${esc(updateLoading?'Проверяю наличие обновлений…':updateCheckResult)}</p>${j&&j.phase!=='idle' ?`<p class="footnote" role="status">${esc(updatePhases[j.phase]||j.phase)}${j.phase==='failed'||j.phase==='interrupted'?' · проверьте журнал на сервере':''}</p>`:''}<p class="footnote">Уведомление появляется здесь и на вкладке «Настройки». Обновление запускает только владелец после подтверждения.</p></section>`}
function updateProgress(){const j=state.updates?.job||{},phase=j.phase,steps=['downloading','checking','installing','verifying'];return `<h2>${esc(updatePhases[phase]||'Обновление')}</h2><p class="subtitle">Версия ${esc(j.version||'—')}</p><div class="update-motion ${updateActive()?'running':''}" aria-hidden="true">${icon('refresh')}</div><ol class="update-steps">${steps.map((key,i)=>`<li class="${steps.indexOf(phase)>i||phase==='complete'?'done':key===phase?'current':''}"><span>${i+1}</span>${esc(updatePhases[key])}</li>`).join('')}</ol><p class="footnote" role="status">${phase==='complete'?'Новая версия запущена. Переоткройте панель, чтобы загрузить свежий интерфейс.':phase==='failed'||phase==='interrupted'?esc(j.reason||'Проверьте состояние службы и журнал установщика на сервере.'):'Панель может временно потерять связь при перезапуске бота. VPN продолжает работать. Не запускайте обновление повторно.'}</p><button type="button" class="secondary full" data-action="close">Свернуть</button>`}
function showUpdateProgress(){const d=$('#modal');d.dataset.updateProgress='true';d.innerHTML=updateProgress();if(!d.open)d.showModal()}
async function loadUpdates(force=false){if(updateLoading||!state.data?.access?.owner)return;updateLoading=true;if(force)updateCheckResult='';if(state.route[0]==='settings')render();try{state.updates=await api(force?'/updates/check':'/updates',force?{method:'POST',body:'{}'}:{});if(force)updateCheckResult=state.updates.error?'Не удалось проверить обновления. Попробуйте ещё раз.':state.updates.release?'Доступно обновление '+state.updates.release.version:'Обновлений нет. Установлена актуальная версия.';document.querySelector('[data-tab=settings]').classList.toggle('has-update',Boolean(state.updates.enabled&&state.updates.release));if(state.route[0]==='settings')render();if($('#modal').open&&$('#modal').dataset.updateProgress==='true')patchHTML($('#modal'),updateProgress());if(updateActive()){clearTimeout(updatePoll);updatePoll=setTimeout(()=>loadUpdates(),3000)}}catch(e){if(force)updateCheckResult=e.message;if(updateActive()){clearTimeout(updatePoll);updatePoll=setTimeout(()=>loadUpdates(),5000)}}finally{updateLoading=false;if(state.route[0]==='settings')render()}}
async function openUpdate(){if(updateActive())return showUpdateProgress();try{const p=await api('/updates/prepare',{method:'POST',body:'{}'});modal('Обновить менеджер?',`<p>Установить версию <b>${esc(p.version)}</b>?</p><button type="button" class="text-button whats-new" data-action="changelog">Что нового?</button><p class="footnote">Сначала проверим файлы и подготовим зависимости. Установщик сохранит резервную копию кода, настроек и базы, затем перезапустит только бота. При ошибке замены выполнит откат. Панель временно отключится.</p>`,async()=>{state.updates.job=await api('/updates/start',{method:'POST',body:JSON.stringify({nonce:p.nonce})});setTimeout(()=>{showUpdateProgress();loadUpdates()},0)},'Обновить')}catch(e){toast(e.message)}}
function managementSettings(){return updateSection()+`<p class="footnote">Версия ${esc(state.data?.build?.version||'—')} · сборка ${esc(state.data?.build?.id||'—')}</p><section class="surface settings-section"><h2>Управление</h2>${state.data?.access?.owner?`<a class="management-link" href="#/admins"><span>${icon('users')} Telegram-администраторы</span>${icon('arrow')}</a><a class="management-link" href="#/audit"><span>${icon('chart')} Журнал действий</span>${icon('arrow')}</a><button class="management-link full" data-action="add-server"><span>${icon('plus')} Добавить сервер</span>${icon('arrow')}</button><button class="management-link full" data-action="backup-schedule"><span>Резервные копии</span>${icon('arrow')}</button><button class="management-link full" data-action="resource-alerts"><span>Оповещения по ресурсам</span>${icon('arrow')}</button><button class="management-link full" data-action="service-traffic-settings"><span>Трафик по сервисам</span>${icon('arrow')}</button><p class="footnote">Вы — владелец. Только вы управляете доступом, подключениями серверов и резервными копиями.</p>`:'<p class="footnote">Вы — администратор. Доступ к клиентам, статистике, Speedtest и перезагрузке всех подключённых серверов. Доступом и подключениями управляет владелец.</p>'}</section>`}
function adminsScreen(){if(!state.data?.access?.owner)return empty('Доступ только владельцу','Управлять администраторами может владелец бота.');return back('#/settings','Настройки')+heading('Администраторы','Доступ к боту и Mini App',`<button class="primary icon-button" data-action="add-admin" aria-label="Добавить администратора">${icon('plus')}</button>`)+`<p class="footnote">Добавляйте только доверенных людей по числовому Telegram ID. Для каждого можно выбрать доступные серверы и разрешённые действия. Менять состав администраторов, SSH-подключения и скачивать полную резервную копию может только владелец.</p><section class="surface detail-list"><div class="detail-row"><span>Владелец</span><b>${esc(state.admins?.owner_id??'—')} · постоянный доступ</b></div>${state.admins?state.admins.admins.map(a=>`<div class="detail-row"><div><b>${esc(a.name)}</b><p class="subtitle">ID ${a.id}</p></div><div><button class="text-button" data-action="admin-scope" data-uid="${a.id}">Права</button><button class="text-button danger" data-action="remove-admin" data-uid="${a.id}">Отозвать</button></div></div>`).join(''):'<p class="footnote">Загружаю список…</p>'}</section><p class="footnote">Чтобы узнать свой ID, отправьте этому боту /id. После добавления администратор открывает бота и нажимает /start. @username не заменяет Telegram ID.</p>`}
async function loadAudit(before=0){try{const data=await api('/audit'+(before?'?before='+before:''));if(state.route[0]!=='audit')return;state.audit=before&&state.audit?{...data,events:[...state.audit.events,...data.events]}:data;render()}catch(e){notice(e.message,true)}}
function auditScreen(){if(!state.data?.access?.owner)return empty('Доступ только владельцу','Журнал содержит историю административных действий.');return back('#/settings','Настройки')+heading('Журнал действий','Кто, когда и что изменил')+`<p class="footnote">Последние 90 дней, до 10 000 записей. Запрос и операция могут иметь отдельные записи с одним ID. «accepted» — задача принята, не подтверждение её завершения.</p>`+(state.audit?state.audit.events.map(r=>`<section class="surface report-card"><b>${esc(r.action)}</b><p>${esc(r.result)} · ${esc(r.source)} · ${r.actor??'Система'}</p><p class="footnote">${new Date(r.ts*1000).toLocaleString('ru-RU')} · Сервер ${r.sid??'—'} · Клиент ${r.cid??'—'}<br>ID ${esc(r.request_id)} · ${r.elapsed} мс${r.error?' · '+esc(r.error):''}</p></section>`).join('')+(state.audit.next?`<button class="secondary full" data-action="audit-more" data-before="${state.audit.next}">Ещё записи</button>`:'')||empty('Записей пока нет','Новые действия появятся здесь.'):'<p role="status">Загружаю журнал…</p>')}
async function loadAdmins(){if(!state.data?.access?.owner)return;try{state.admins=await api('/admins');if(state.route[0]==='admins')render()}catch(e){toast(e.message)}}
function speedtestCard(s){const t=state.speedtests[s.id]||{phase:'idle'},r=t.result;const busy=['checking','installing','running'].includes(t.phase);const phase={checking:'Проверяю наличие…',installing:'Устанавливаю…',running:'Измеряю скорость…'}[t.phase];return `<section class="surface chart-card speedtest-card" data-speedtest="${s.id}"><div class="hero-top"><h2>Speedtest</h2><span class="hero-icon">${icon('chart')}</span></div><p class="subtitle">Пропускная способность канала сервера</p>${busy?`<div class="speed-activity" role="status"><span class="speed-spinner"></span><span>${phase}<small>Результат появится после завершения</small></span><div class="speed-wave"><i></i><i></i><i></i><i></i><i></i></div></div>`:''}${r?`${busy?'<p class="footnote">Предыдущий результат</p>':''}<div class="metric-pair"><div><p class="eyebrow">↓ Загрузка VPS</p><h2 class="accent">${r.download.toFixed(1)} <small>Мбит/с</small></h2></div><div><p class="eyebrow">↑ Отдача VPS</p><h2>${r.upload.toFixed(1)} <small>Мбит/с</small></h2></div></div><p class="footnote">Ping ${r.ping.toFixed(1)} мс · ${esc(r.target)}<br>${new Date(r.ts*1000).toLocaleString('ru-RU',{timeZone:'Europe/Samara'})}</p>`:''}${t.error?`<p class="footnote danger" role="alert">${esc(t.error)}</p>`:''}${t.tool==='missing'?'<p class="footnote">Speedtest на этом сервере не установлен.</p>':''}<button class="${t.tool==='missing'?'secondary':'primary'} full" data-action="${t.tool==='missing'?'speed-install':'speed-run'}" ${busy?'disabled':''}>${phase||(t.tool==='missing'?'Установить Speedtest':'Запустить Speedtest')}</button><p class="footnote">Тест запускается вручную и временно нагружает канал. Это скорость самого сервера до тестового узла; скорость через VPN на устройстве может отличаться.</p></section>`}
async function loadSpeedtest(){const sid=state.route[1];if(state.route[0]!=='server'||!sid)return;try{state.speedtests[sid]=await api(`/servers/${sid}/speedtest`);if(state.route[0]==='server'&&state.route[1]===sid&&!$('#modal').open){const card=document.querySelector('[data-speedtest]');if(card)patchElement(card,speedtestCard(server(sid)))}}catch(e){toast(e.message)}}
function overview(){const ss=servers(),online=ss.reduce((a,s)=>a+s.online,0),clients=ss.reduce((a,s)=>a+s.clients.length,0),traffic=ss.reduce((a,s)=>a+total(s),0);return heading('Всё под контролем','Ваши серверы. В одном месте.',state.data?.access?.owner?`<button class="primary icon-button" data-action="add-server" aria-label="Добавить сервер">${icon('plus')}</button>`:'')+`<section class="surface hero"><div class="hero-top"><span class="eyebrow">Общий трафик</span><span class="hero-icon">${icon('chart')}</span></div><div class="big-number">${splitSize(traffic)}</div><p class="hero-caption">Накоплено с начала учёта</p><div class="hero-footer"><span><b class="online">${online}</b> в сети</span><span><b>${clients}</b> клиентов</span><span><b>${ss.length}</b> сервера</span></div></section><div class="section-heading"><h2>Серверы</h2><small>${ss.filter(s=>s.ok).length} из ${ss.length} доступны</small></div><div class="chips" aria-label="Показатели серверов">${[['speed','Скорость'],['traffic','Трафик']].map(([key,label])=>`<button class="chip" data-metric="${key}" aria-pressed="${state.metricMode===key}">${label}</button>`).join('')}</div>`+(ss.length?ss.map(s=>`<a class="surface server-card" href="#/server/${s.id}"><div class="server-head"><span class="server-icon">${icon('server')}</span><div class="server-name"><h3>${esc(s.name)}</h3><p class="status"><i class="dot ${s.ok?'':'error'}"></i>${s.ok?`${s.online} из ${s.clients.length} в сети`:'Нет связи · последние данные'}</p></div>${icon('arrow').replace('<svg','<svg class="chevron"')}</div>${state.metricMode==='speed'?liveMetrics(s):trafficMetrics(s.rx,s.tx)}</a>`).join(''):empty('Серверов пока нет','Нажмите плюс, чтобы подключить сервер по SSH.'))+`<p class="footnote">Направления со стороны клиентов: ↓ загрузка на устройства, ↑ отдача с устройств. Общий трафик — за весь период учёта, включая начальные счётчики VPN.</p>`}
function graph(samples){const points=samples.slice(-30),rates=[];for(let i=1;i<points.length;i++){const dt=points[i].ts-points[i-1].ts;if(dt>0)rates.push({ts:points[i].ts,rx:Math.max(0,points[i].rx-points[i-1].rx)/dt,tx:Math.max(0,points[i].tx-points[i-1].tx)/dt})}if(rates.length<2)return `<div class="chart-placeholder" role="status"><p>График появится после нескольких замеров.</p></div>`;const max=Math.max(1,...rates.flatMap(p=>[p.rx,p.tx])),start=rates[0].ts,end=rates.at(-1).ts;const coords=k=>rates.map(p=>`${((p.ts-start)/(end-start)*320).toFixed(1)},${(115-p[k]/max*100).toFixed(1)}`).join(' ');return `<svg class="chart" viewBox="0 0 320 150" preserveAspectRatio="none" role="img" aria-label="Скорость VPN: на устройства и с устройств"><path class="grid" d="M0 15H320M0 65H320M0 115H320"/><polygon class="area" points="0,115 ${coords('rx')} 320,115"/><polyline class="download" points="${coords('rx')}"/><polyline class="upload" points="${coords('tx')}"/><text x="0" y="10">${esc(fmt(max))}/с</text><text x="0" y="140">${new Date(start*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Samara'})}</text><text x="320" y="140" text-anchor="end">${Date.now()/1000-end<=20?'Сейчас':new Date(end*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Samara'})}</text></svg>`}
const trafficGroups = {
  video: ['Видео и стримы', {youtube:'YouTube', netflix:'Netflix', twitch:'Twitch', tiktok:'TikTok', vk_video:'VK Видео'}],
  music: ['Музыка и аудио', {spotify:'Spotify', youtube_music:'YouTube Music', yandex_music:'Яндекс Музыка', soundcloud:'SoundCloud'}],
  entertainment: ['Прочие развлечения', {}],
  messages: ['Мессенджеры и звонки', {telegram:'Telegram', whatsapp:'WhatsApp', discord:'Discord'}],
  social: ['Соцсети', {instagram:'Instagram', vk:'VK', facebook:'Facebook', x:'X / Twitter'}],
  games: ['Игры', {steam:'Steam'}],
  work: ['Работа и почта', {zoom:'Zoom'}],
  files: ['Облака и загрузки', {}],
  updates: ['Обновления ПО', {}],
  shopping: ['Покупки', {}],
  news: ['Новости', {}],
  web: ['Прочий веб', {}],
  unknown: ['Не определено', {}],
};

const trafficServices = Object.fromEntries(Object.entries(trafficGroups).flatMap(([group,[,services]]) =>
  Object.entries(services).map(([key,label])=>[key,{group,label}])));

function groupServiceTraffic(rows){
  const grouped=new Map();
  for(const row of rows){
    const service=Object.hasOwn(trafficServices,row.category)?trafficServices[row.category]:null;
    const key=service?.group||row.category.replace(/^group_/,'');
    if(!Object.hasOwn(trafficGroups,key))continue;
    if(!grouped.has(key))grouped.set(key,{key,rx:0,tx:0,details:[]});
    const group=grouped.get(key);
    group.rx+=row.rx;
    group.tx+=row.tx;
    if(key!=='web'&&key!=='unknown')group.details.push({...row,label:service?.label||'Сервис не определён'});
  }
  return [...grouped.values()].sort((a,b)=>total(b)-total(a));
}

function openServiceTrafficSettings(){
  const d=$('#modal');
  delete d.dataset.updateProgress;
  d.setAttribute('aria-label','Трафик по сервисам');
  d.innerHTML=`<h2 class="dialog-title" tabindex="-1" autofocus>Трафик по сервисам</h2>
    <p class="footnote">Включите сбор на нужном VPS. При первом включении nDPI установится автоматически. Сбор повышает нагрузку на процессор и расход памяти. При выключении статистика сохраняется.</p>
    <div class="service-traffic-controls"></div>
    <button type="button" class="secondary full" data-action="close">Закрыть</button>`;
  const list=d.querySelector('.service-traffic-controls');
  const hosts=servers();
  if(!hosts.length)list.textContent='Подключите VPN-сервер, чтобы настроить сбор.';
  d.showModal();
  for(const host of hosts){
    const row=document.createElement('div');
    row.innerHTML=`<label class="motion-row"><span><b>${esc(host.name)}</b>
      <small role="status"><span class="service-traffic-status">Проверяю состояние…</span><span class="service-traffic-detail" hidden></span></small>
      <small class="service-traffic-error" hidden></small></span>
      <input type="checkbox" role="switch" aria-label="Сбор трафика — ${esc(host.name)}" disabled></label>`;
    list.append(row);
    const toggle=row.querySelector('input'), message=row.querySelector('.service-traffic-status');
    const detail=row.querySelector('.service-traffic-detail');
    const errorMessage=row.querySelector('.service-traffic-error');
    const path=`/servers/${host.id}/service-traffic/control`;
    let current;
    const describe=(status,poll=true)=>{
      current=status.state;
      toggle.checked=status.enabled===true;
      toggle.disabled=!['ready','missing'].includes(current);
      message.dataset.state=current==='ready'&&!status.running?'stopped':current;
      message.textContent={
        ready:status.running?'nDPI установлен':'nDPI остановлен',
        missing:'nDPI не установлен',
        installing:'Устанавливаю nDPI…',
        requires_root:'nDPI не установлен',
        unmanaged:'Служба не привязана к этому интерфейсу или недоступна для управления',
        unavailable:'Не удалось проверить состояние службы'
      }[current];
      detail.textContent=current==='ready'?(status.running?'Сбор включён':status.enabled?'Автозапуск включён':''):'';
      detail.hidden=!detail.textContent;
      errorMessage.textContent=status.error||(current==='requires_root'?'Для установки из панели нужен root на VPS':'');
      errorMessage.hidden=!errorMessage.textContent;
      if(status.error&&d.open&&row.isConnected)toast(status.error);
      if(poll&&status.state==='installing'){
        setTimeout(()=>{if(d.open&&row.isConnected)read()},2000);
      }
    };
    const read=async()=>{
      try{describe(await api(path))}
      catch{describe({state:'unavailable'})}
    };
    toggle.addEventListener('change',async()=>{
      const enabled=toggle.checked;
      const needsInstall=current==='missing'&&enabled;
      toggle.disabled=true;
      if(needsInstall)describe({state:'installing'},false);
      else detail.textContent=enabled?'Запускаю сборщик…':'Останавливаю сборщик…';
      try{
        const options=needsInstall
          ? {method:'POST',body:JSON.stringify({enable:true})}
          : {method:'PATCH',body:JSON.stringify({enabled})};
        describe(await api(path,options,40000));
      }catch(error){
        await read();
        if(d.open&&row.isConnected)toast(error.message);
      }
    });
    read();
  }
}

function serviceTrafficButton(){
  return state.data?.access?.owner
    ? '<div class="service-traffic-entry"><button class="secondary full" data-action="service-traffic">Трафик по сервисам</button></div>'
    : '';
}

function serviceTrafficReport(report){
  if(report.state==='disabled')return '<p class="footnote">На этом сервере сбор ещё не запущен.</p>';
  if(report.state==='unavailable')return '<p class="hint danger">Не удалось прочитать данные. Повторите запрос чуть позже.</p>';
  const groups=groupServiceTraffic(report.rows);
  const bytes=groups.reduce((sum,group)=>sum+total(group),0);
  const named=report.rows.filter(row=>Object.hasOwn(trafficServices,row.category)).reduce((sum,row)=>sum+total(row),0);
  const unknown=groups.find(group=>group.key==='unknown');
  const directions=row=>`<span class="detail-row footnote"><span>↓ ${fmt(row.rx)}</span><span>↑ ${fmt(row.tx)}</span></span>`;
  const share=value=>(bytes?value/bytes:0).toLocaleString('ru-RU',{style:'percent',maximumFractionDigits:1});
  const date=stamp=>new Date(stamp*1000).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'});
  return [
    report.state==='stale'?'<p class="hint danger">Сборщик сейчас не обновляется. Показаны последние данные.</p>':'',
    bytes?'<div class="detail-row"><span>Учтено</span><b>'+fmt(bytes)+'</b></div>'+
      '<div class="detail-row"><span>Распознаны сервисы</span><b>'+share(named)+'</b></div>'+
      '<div class="detail-row"><span>Без категории</span><b>'+share(unknown?total(unknown):0)+'</b></div>':
      '<p class="footnote">За этот период пока нет учтённого трафика.</p>',
    ...groups.map(group=>{
      const label=trafficGroups[group.key][0];
      const title=`<span class="detail-row"><b>${label}</b><b>${fmt(total(group))}</b></span>`;
      const breakdown=`<span class="traffic-bar" aria-hidden="true"><span style="width:${100*total(group)/bytes}%"></span></span>${directions(group)}`;
      if(!group.details.some(row=>Object.hasOwn(trafficServices,row.category)))
        return `<section class="service-traffic-row">${title}${breakdown}</section>`;
      return `<details class="service-traffic-row">
        <summary><span class="traffic-group-overview">${title}${breakdown}</span><span class="traffic-group-chevron">${icon('arrow')}</span></summary>
        <ul class="traffic-service-details">${group.details.sort((a,b)=>total(b)-total(a)).map(row=>
          `<li><div class="detail-row"><span>${row.label}</span><b>${fmt(total(row))}</b></div>${directions(row)}</li>`).join('')}</ul>
      </details>`;
    }),
    `<p class="footnote">Начало сбора: ${date(report.started)} · Последняя запись: ${date(report.updated)}</p>`
  ].join('');
}

function openServiceTraffic(sid,cid){
  const d=$('#modal');
  const s=server(sid), client=cid?s?.clients.find(item=>item.id===Number(cid)):null;
  const scope=client?`${client.name} · ${s.name}`:s?.name;
  const path=`/servers/${sid}${cid?`/clients/${cid}`:''}/service-traffic`;
  delete d.dataset.updateProgress;
  d.setAttribute('aria-label','Трафик по сервисам');
  d.innerHTML=`<div class="service-traffic-view">
    <h2 class="dialog-title" tabindex="-1" autofocus>Трафик по сервисам</h2><p class="subtitle">${esc(scope)}</p>
    <div class="chips" role="group" aria-label="Период">
      <button type="button" class="chip" data-traffic-window="day">24 часа</button>
      <button type="button" class="chip" data-traffic-window="week">7 дней</button>
    </div>
    <div class="service-traffic-scroll">
      <div class="service-traffic-report" aria-live="polite"></div>
      <p class="footnote">Оценка по TCP и UDP внутри VPN. Раскройте категорию, чтобы увидеть сервисы. ↓ На устройства · ↑ С устройств. Объёмы могут отличаться от счётчиков VPN.</p>
    </div>
    <button type="button" class="secondary full" data-action="close">Закрыть</button>
  </div>`;
  const result=d.querySelector('.service-traffic-report');
  const scroll=d.querySelector('.service-traffic-scroll');
  const buttons=[...d.querySelectorAll('[data-traffic-window]')];
  let revision=0;
  async function load(window){
    const attempt=++revision;
    for(const button of buttons){
      const active=button.dataset.trafficWindow===window;
      button.setAttribute('aria-pressed',String(active));
    }
    scroll.scrollTop=0;
    result.textContent='Загружаю отчёт…';
    try{
      const report=await api(path+'?window='+window);
      if(attempt===revision&&d.open&&result.isConnected)result.innerHTML=serviceTrafficReport(report);
    }catch(e){
      if(attempt===revision&&d.open&&result.isConnected)result.textContent=e.message;
    }
  }
  buttons.forEach(button=>button.addEventListener('click',()=>load(button.dataset.trafficWindow)));
  d.showModal();
  load('day');
}

function serverScreen(id){const s=server(id);if(!s)return empty('Сервер не найден','Вернитесь в обзор.');return back('#/','Все серверы')+heading(s.name,s.ok&&!s.stale?'Сервер доступен':'Нет связи · показаны последние данные')+`<section class="surface chart-card"><div class="hero-top"><h2>Скорость трафика</h2><small>5 сек</small></div><div class="metric-pair"><div><p class="eyebrow">↓ На устройства</p><h2 class="accent">${s.ok&&!s.stale?fmt(s.rate_rx):'—'}<small>/с</small></h2></div><div><p class="eyebrow">↑ С устройств</p><h2>${s.ok&&!s.stale?fmt(s.rate_tx):'—'}<small>/с</small></h2></div></div>${graph(s.samples)}<div class="chart-legend"><span><i></i>На устройства</span><span><i class="upload"></i>С устройств</span></div></section>${embeddedDaily()}${serviceTrafficButton()}${resourceCard(s)}<div class="metric-pair"><a class="surface metric metric-link" href="#/clients/${s.id}" data-online-link aria-label="Клиенты в сети: ${s.name}"><small>${icon('users')}В сети ${icon('arrow')}</small><b>${s.online}<span> / ${s.clients.length}</span></b></a><a class="surface metric metric-link" href="#/stats/${s.id}" aria-label="Статистика трафика: ${s.name}"><small>${icon('chart')}Всего трафика ${icon('arrow')}</small><b>${fmt(total(s))}</b></a></div><div class="surface server-card"><h3>Трафик за всё время учёта</h3><p class="subtitle">Включая начальные счётчики VPN</p>${trafficMetrics(s.rx,s.tx)}</div>${hostTrafficCard(s)}${speedtestCard(s)}<section class="surface chart-card reboot-card"><h2>Управление сервером</h2><p class="footnote">Перезагрузка всего VPS временно отключит VPN всех его клиентов. Если бот работает здесь, панель тоже ненадолго отключится.</p>${state.reboots[s.id]?'<p class="footnote" role="status">Перезагрузка запрошена. Дождитесь восстановления связи.</p>':''}<button class="secondary danger full" data-action="reboot" ${s.permissions?.reboot===false||!s.ok||state.reboots[s.id]&&Date.now()-state.reboots[s.id]<120000?'disabled':''}>Перезагрузить VPS</button></section><div class="toolbar"><a class="primary full" style="text-align:center" href="#/clients/${s.id}">Клиенты сервера</a><a class="secondary full" style="text-align:center" href="#/stats/${s.id}">Статистика по месяцам</a></div><p class="footnote">↓ На устройства — сервер → устройства, ↑ отдача — устройства → сервер. Средняя скорость между успешными замерами. Пока приложение открыто, запрос — каждые 5 секунд; время ответа сервера может увеличить интервал. Последний замер: ${ago(s.updated)}.</p>`}
function clientCard(c){return `<a class="surface client-card" href="#/client/${c.sid}/${c.id}"><i class="dot client-status ${statusClass(c)}" aria-label="${status(c)}"></i><span class="avatar">${esc(c.name.slice(0,2).toUpperCase())}</span><span class="client-title">${esc(c.labels?.device||c.name)}</span>${c.status_known===false?'<small>Статус неизвестен</small>':''}<div class="client-volume">${fmt(c.rx+c.tx)}</div><small>${esc([c.server,c.labels?.group,c.labels?.owner].filter(Boolean).join(" · "))}</small></a>`}
function clientGrid(){let list=allClients();const id=state.route[1];if(id)list=list.filter(c=>c.sid===Number(id));else if(state.serverFilter!=='all')list=list.filter(c=>c.sid===Number(state.serverFilter));if(state.filter==='online')list=list.filter(c=>c.online);if(state.filter==='offline')list=list.filter(c=>!c.online&&c.status_known!==false&&hasHistory(c));if(state.filter==='unknown')list=list.filter(c=>c.status_known===false||!hasHistory(c));const query=state.search.toLocaleLowerCase();list=list.filter(c=>[c.name,c.ip,c.server,...Object.values(c.labels||{})].join(' ').toLocaleLowerCase().includes(query));return list.length?`<div class="client-grid">${list.map(clientCard).join('')}</div>`:empty('Никого не нашли','Попробуйте другое имя, VPN-адрес или фильтр.')}
function clientsScreen(){const id=state.route[1],s=id?server(id):null;if(id&&!s)return back('#/clients','Все клиенты')+empty('Сервер не найден','Возможно, он удалён. Выберите другой сервер.');return (s?back(`#/server/${s.id}`,s.name):'')+heading('Клиенты',s?s.name:'Все конфиги ваших серверов',`<button class="primary icon-button" data-action="create" aria-label="Добавить клиента">${icon('plus')}</button>`)+(!id?filterMenu('client-server','Фильтр по серверу',serverFilterItems(),state.serverFilter):'')+`<div class="search">${icon('search')}<input class="field" id="search" type="search" placeholder="Имя или VPN-адрес" aria-label="Поиск клиентов" value="${esc(state.search)}"></div><div class="chips" aria-label="Статус клиента">${[['all','Все'],['online','В сети'],['offline','Не в сети'],['unknown','Нет данных']].map(([key,label])=>`<button class="chip" data-filter="${key}" aria-pressed="${state.filter===key}">${label}</button>`).join('')}</div><div id="client-results">${clientGrid()}</div>`}
function clientTrafficSummary(c){return `<section class="surface hero"><p class="status"><i class="dot ${statusClass(c)}"></i>${status(c)}</p><div class="big-number">${splitSize(c.rx+c.tx)}</div><p class="hero-caption">Трафик клиента · накопительный</p>${trafficMetrics(c.rx,c.tx)}</section>`}
function filterMenu(id,label,items,value){
  const selected=items.find(([key])=>key===value)||items[0];
  const open=document.getElementById(id+'-menu')?.open;
  return `<details class="filter-menu" id="${id}-menu" ${open?'open':''}>
    <summary class="field" aria-label="${esc(label)}: ${esc(selected[1])}">${esc(selected[1])}</summary>
    <div class="filter-options">${items.map(([key,name])=>`<button type="button" data-filter-option="${esc(key)}" aria-pressed="${key===selected[0]}">${esc(name)}${key===selected[0]?'<span aria-hidden="true">✓</span>':''}</button>`).join('')}</div>
    <select hidden id="${id}">${items.map(([key,name])=>`<option value="${esc(key)}" ${key===selected[0]?'selected':''}>${esc(name)}</option>`).join('')}</select>
  </details>`;
}
function serverFilterItems(){
  return [['all','Все серверы'],...servers().map(s=>[String(s.id),s.name])];
}
document.addEventListener('click',event=>{
  const menu=event.target.closest('.filter-menu');
  for(const opened of document.querySelectorAll('.filter-menu[open]')){
    if(opened!==menu)opened.open=false;
  }
  const option=event.target.closest('[data-filter-option]');
  if(!option)return;
  const select=menu.querySelector('select');if(option.disabled)return;
  menu.open=false;
  menu.querySelector('summary').focus();
  select.value=option.dataset.filterOption;
  if(menu.dataset.formSelect){const name=select.selectedOptions[0].textContent;menu.querySelector('summary').textContent=name;menu.querySelector('summary').setAttribute('aria-label',menu.dataset.label+': '+name);for(const b of menu.querySelectorAll('[data-filter-option]')){b.setAttribute('aria-pressed',String(b===option));b.querySelector('span')?.remove()}option.insertAdjacentHTML('beforeend','<span aria-hidden="true">✓</span>')}
  select.dispatchEvent(new Event('change',{bubbles:true}));
});
document.addEventListener('keydown',event=>{
  const menu=event.target.closest('.filter-menu');if(!menu)return;
  if(event.key==='Escape'&&menu.open){event.preventDefault();event.stopPropagation();menu.open=false;menu.querySelector('summary').focus();return}
  if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
  event.preventDefault();menu.open=true;
  const items=[...menu.querySelectorAll('[data-filter-option]:not(:disabled)')],index=items.indexOf(document.activeElement);
  const next=event.key==='Home'?0:event.key==='End'?items.length-1:event.key==='ArrowDown'?Math.min(items.length-1,index+1):index<0?items.length-1:Math.max(0,index-1);
  items[next]?.focus();
});
function enhanceFormSelects(dialog){
  for(const select of dialog.querySelectorAll('select.field')){
    const label=select.closest('label'),text=label?.firstChild?.textContent.trim()||'Выберите значение';
    const id=select.id||`form-${select.name}`;
    const wrapper=document.createElement('div');
    wrapper.innerHTML=filterMenu(id,text,[...select.options].map(o=>[o.value,o.textContent]),select.value);
    const menu=wrapper.firstElementChild;
    select.replaceWith(menu);select.id=id;select.hidden=true;select.classList.remove('field');menu.querySelector('select').replaceWith(select);
    menu.dataset.formSelect='true';menu.dataset.label=text;
    if(label){const field=document.createElement('div');field.className='form-field';while(label.firstChild)field.append(label.firstChild);label.replaceWith(field)}
  }
}
function clientScreen(sid,cid){const s=server(sid),c=s?.clients.find(c=>c.id===Number(cid));if(!c)return empty('Конфиг не найден','Возможно, он уже удалён.');return back(state.clientReturn,state.clientReturn===`#/clients/${sid}`?s.name+' · Клиенты':'Клиенты')+heading(c.labels?.device||c.name,[c.ip,c.labels?.owner,c.labels?.group].filter(Boolean).join(' · '))+`${clientTrafficSummary(c)}${c.labels?.note?`<p class="footnote">${esc(c.labels.note)}</p>`:''}<div class="client-actions"><button class="secondary full" data-action="client-labels">Описание клиента</button>${s.permissions?.pause!==false?`<button class="secondary full" data-action="pause-client">${c.paused?'Возобновить доступ':'Приостановить доступ'}</button>`:''}</div>${embeddedDaily()}${serviceTrafficButton()}<div class="surface detail-list"><div class="detail-row"><span>Сервер</span><b>${esc(s.name)}</b></div><div class="detail-row"><span>Последнее соединение</span><b>${c.last_handshake||c.handshake?ago(c.last_handshake||c.handshake):'Нет данных'}</b></div><div class="detail-row"><span>Лимит трафика</span><b>${c.limit?fmt(c.limit):'Без ограничения'}</b></div><div class="detail-row"><span>Действует до</span><b>${c.expires?new Date(c.expires*1000).toLocaleDateString('ru-RU',{timeZone:'Europe/Samara'}):'Без срока'}</b></div></div><div class="toolbar"><button class="primary full" data-action="config" ${!c.has_config?'disabled':''}>Скачать конфиг</button><button class="secondary full" data-action="policy" ${s.permissions?.delete===false?'disabled':''}>Изменить ограничения</button></div><button class="secondary full reissue-button" data-action="reissue" ${c.paused||s.permissions?.reissue===false?'disabled':''}>Перевыпустить конфиг</button>${!c.has_config?'<p class="footnote">Сохранённого файла пока нет. Перевыпустите конфиг: новый файл останется доступен для повторного скачивания.</p>':''}<p class="footnote">Здесь ↓ — загрузка на устройство, ↑ — отдача с устройства. Начальные счётчики VPN включены в накопительный трафик.</p><button class="text-button danger full" data-action="delete" ${s.permissions?.delete===false?'disabled':''}>Удалить конфиг</button>`}
function monthlyDirections(clients){const rx=clients.reduce((n,c)=>n+c.rx,0),tx=clients.reduce((n,c)=>n+c.tx,0);return `<div class="monthly-directions"><dl><dt>↓ На устройства</dt><dd>${fmt(rx)}</dd></dl><dl><dt>↑ С устройств</dt><dd>${fmt(tx)}</dd></dl></div>`}
function statsScreen(){const sid=state.route[1]||'all',s=sid==='all'?{name:'Все серверы'}:server(sid),report=state.report;const months=[];const now=new Date();const year=Number(currentMonth().slice(0,4)),m=Number(currentMonth().slice(5));for(let i=0;i<24;i++){const d=new Date(Date.UTC(year,m-1-i,1));months.push([d.toISOString().slice(0,7),d.toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'})])}if(state.month&&!months.some(([key])=>key===state.month))months.push([state.month,state.month]);return heading('Статистика','Трафик по каждому конфигу')+`<div class="month-controls">${filterMenu('stats-server','Сервер',serverFilterItems(),sid)}${filterMenu('stats-month','Месяц отчёта',months.map(([key,label])=>[key,label.replace(' г.','')]),state.month)}</div>`+(!s||!servers().length?empty('Нет серверов',state.data?.access?.owner?'Подключите сервер кнопкой плюс в обзоре.':'Попросите владельца подключить сервер.'):!report&&state.reportError?reportFailure('report',state.reportError):!report?'<section class="surface hero stats-summary stats-loading" role="status" aria-label="Загрузка отчёта"><div class="eyebrow">Загрузка месяца</div><div class="big-number">—<span>Гб</span></div><p class="hero-caption">Загружаю данные конфигов</p><div class="monthly-directions"><dl><dt>↓ На устройства</dt><dd>—</dd></dl><dl><dt>↑ С устройств</dt><dd>—</dd></dl></div></section>':`<section class="surface hero stats-summary"><div class="eyebrow">${esc(new Date(state.month+'-01T12:00:00Z').toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}))}</div><div class="big-number">${splitSize(report.clients.reduce((n,c)=>n+c.rx+c.tx,0))}</div><p class="hero-caption">${esc(s.name)} · ${report.clients.length} конфигов</p>${state.month===new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Samara',year:'numeric',month:'2-digit'}).format(new Date(state.data.monthly_since*1000))?`<p class="hero-caption">Учёт с ${new Date(state.data.monthly_since*1000).toLocaleString('ru-RU',{timeZone:'Europe/Samara',dateStyle:'short',timeStyle:'short'})}</p>`:''}${monthlyDirections(report.clients)}</section><div class="section-heading"><h2>По конфигам</h2><button class="secondary compact" data-action="csv">CSV ↓</button></div><div class="chips" aria-label="Сортировка">${[['total','Σ Всего'],['rx','↓ Получено'],['tx','↑ Отправлено']].map(([key,label])=>`<button class="chip" data-sort="${key}" aria-pressed="${state.sort===key}">${label}</button>`).join('')}</div>${report.clients.length?report.clients.map((c,i)=>`<a href="#/daily/${c.server_id||sid}/${c.id}" class="surface report-card report-link" data-report-client="${c.server_id||sid}:${c.id}"><div class="report-title"><span class="rank">${String(i+1).padStart(2,'0')}</span><span class="report-name">${esc(c.name)}${sid==='all'?` <small>· ${esc(c.server_name)}</small>`:''}${c.deleted?' <small>· удалён</small>':''}</span><span class="report-total">${fmt(c.rx+c.tx)}</span></div><div class="traffic-bar"><div style="width:${Math.max(0,(c.rx+c.tx)/Math.max(1,...report.clients.map(c=>c.rx+c.tx))*100)}%"></div></div><div class="report-directions"><span>↓ ${fmt(c.rx)}</span><span>↑ ${fmt(c.tx)}</span></div></a>`).join(''):empty('Нет данных за этот месяц','Выберите другой месяц. История до включения учёта недоступна.')}<p class="footnote">Самара · UTC+4. Учёт начат ${new Date(state.data.monthly_since*1000).toLocaleString('ru-RU',{timeZone:'Europe/Samara',dateStyle:'short',timeStyle:'short'})}. Удалённые конфиги остаются в отчётах. Интервал на границе месяцев относится к месяцу завершающего замера.</p>`)}
function dailyMonthControl(){const label=new Date(state.month+'-01T12:00:00Z').toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).replace(' г.','');return `<div class="month-switcher" role="group" aria-label="Месяц дневного отчёта"><button type="button" data-month-step="-1" aria-label="Предыдущий месяц" ${state.month==='0001-01'?'disabled':''}>${icon('back')}</button><label class="month-switcher-date"><span>${esc(label)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/></svg><input id="daily-month" type="month" min="0001-01" value="${state.month}" max="${currentMonth()}" aria-label="Месяц дневного отчёта"></label><button type="button" data-month-step="1" aria-label="Следующий месяц" ${state.month>=currentMonth()?'disabled':''}>${icon('arrow')}</button></div>`}

function reportFailure(kind,message){return `<section class="surface chart-card report-error" role="alert"><h2>Не удалось загрузить отчёт</h2><p class="footnote">${esc(message)}</p><button class="secondary full" data-action="retry-${kind}">Повторить загрузку</button></section>`}
let comparisonMode='week';
function dailyComparison(report){
  const previous=report.previous_days||[];
  const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Samara',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const completed=report.days.filter(day=>day.day<today);
  const recent=[...previous,...completed].slice(-14);
  const length=Math.min(completed.length,previous.length);
  const weekly=comparisonMode==='week';
  const current=weekly?recent.slice(-7):completed.slice(0,length);
  const earlier=weekly?recent.slice(0,-7):previous.slice(0,length);
  const size=weekly?7:length;
  const sum=days=>fmt(days.reduce((total,day)=>total+day.rx+day.tx,0));
  const date=day=>day.day.slice(5).split('-').reverse().join('.');
  const range=days=>`${date(days[0])}–${date(days.at(-1))}`;
  const available=completed.length&&size&&current.length===size&&earlier.length===size
    &&[...current,...earlier].every(day=>day.observed);
  const summary=available
    ?`${range(current)}: ${sum(current)} · ${range(earlier)}: ${sum(earlier)}`
    :'За оба периода пока недостаточно данных.';
  return `<section class="surface chart-card traffic-comparison"><h2>Сравнение трафика</h2><div class="toolbar" role="group" aria-label="Период сравнения">
    <button type="button" class="secondary" data-comparison="week" aria-pressed="${weekly}">С прошлой неделей</button>
    <button type="button" class="secondary" data-comparison="month" aria-pressed="${!weekly}">С прошлым месяцем</button>
    </div><p class="footnote" aria-live="polite">${summary}</p></section>`;
}
function embeddedDaily(){return `<div class="server-daily">${state.daily?dailyView(state.daily,state.dailyDay,{esc,fmt,splitSize,compact:true,controls:dailyMonthControl()})+dailyComparison(state.daily):state.dailyError?reportFailure('daily',state.dailyError):`<section class="surface chart-card daily-card daily-loading" role="status"><h2>Трафик по дням</h2>${dailyMonthControl()}<div class="daily-loading-chart">Загружаю дневную статистику…</div></section>`}</div>`}
function dailyScreen(){const report=state.daily;const [,sid,cid]=state.route;const client=server(sid)?.clients.find(c=>c.id===Number(cid));return back(state.dailyReturn,'Назад')+heading(report?report.name:'Статистика по дням',report?report.server_name:'Загрузка отчёта')+(client?clientTrafficSummary(client):'')+(report?dailyView(report,state.dailyDay,{esc,fmt,splitSize,controls:dailyMonthControl()})+dailyComparison(state.daily):state.dailyError?reportFailure('daily',state.dailyError):`<section class="surface chart-card" role="status">${dailyMonthControl()}Загружаю дневную статистику…</section>`)}
async function loadDaily(){const [name,sid,cid]=state.route;if(!['daily','server','client'].includes(name)||!sid)return;const rev=state.revision,month=state.month;state.dailyError='';try{const report=await api(`/servers/${sid}${name!=='server'&&cid?`/clients/${cid}`:''}/daily?month=${encodeURIComponent(month)}`);if(rev===state.revision&&month===state.month){state.daily=report;render()}}catch(e){if(rev===state.revision&&month===state.month){state.dailyError=e.message;render();notice(e.message,true)}}}
let lastVisualKey='';
function animateDashboard(previousBars,previousResources){const key=state.route.join('/')+(['stats','daily'].includes(state.route[0])?':'+state.month+':'+state.sort+':'+Boolean(state.route[0]==='daily'?state.daily:state.report):'');const entered=key!==lastVisualKey;lastVisualKey=key;if(!motionAllowed())return;const options={duration:420,easing:'cubic-bezier(.2,.7,.2,1)'};if(entered)$('#app').animate([{opacity:.35},{opacity:1}],{duration:180,easing:'ease-out'});document.querySelectorAll('[data-report-client]').forEach(el=>{const bar=el.querySelector('.traffic-bar>div'),next=parseFloat(bar.style.width),old=entered?0:previousBars.get(el.dataset.reportClient);if(next>0&&old!==undefined&&old!==next)bar.animate([{transform:`scaleX(${old/next})`},{transform:'scaleX(1)'}],options)});document.querySelectorAll('[data-resource]').forEach(el=>{const value=el.dataset.value;if(value==='')return;const next=Number(value),old=entered?0:(previousResources.get(el.dataset.resource)??0);if(old!==undefined&&old!==next)el.querySelector('.ring-fill').animate([{strokeDashoffset:100-old},{strokeDashoffset:100-next}],{...options,duration:600})});if(entered)document.querySelectorAll('.daily-line').forEach(el=>{const length=el.getTotalLength();el.animate([{strokeDasharray:`${length} ${length}`,strokeDashoffset:length},{strokeDasharray:`${length} ${length}`,strokeDashoffset:0}],{...options,duration:600})});if(entered)document.querySelectorAll('.chart polyline').forEach(el=>{const length=el.getTotalLength();el.animate([{strokeDasharray:`${length} ${length}`,strokeDashoffset:length},{strokeDasharray:`${length} ${length}`,strokeDashoffset:0}],{...options,duration:600})})}
function render(animate=false){if(!state.data)return;const name=state.route[0]||'home';const previousBars=new Map([...document.querySelectorAll('[data-report-client]')].map(e=>[e.dataset.reportClient,parseFloat(e.querySelector('.traffic-bar>div').style.width)]));const previousResources=new Map([...document.querySelectorAll('[data-resource]')].filter(e=>e.dataset.value!=='').map(e=>[e.dataset.resource,Number(e.dataset.value)]));const views={home:overview,server:()=>serverScreen(state.route[1]),clients:clientsScreen,client:()=>clientScreen(...state.route.slice(1)),stats:statsScreen,daily:dailyScreen,settings:()=>settingsScreen().replace('<form id="preferences-form">',managementSettings()+'<form id="preferences-form">'),admins:adminsScreen,audit:auditScreen};patchHTML($('#app'),(views[name]||(()=>empty('Страница не найдена','Ссылка устарела. Выберите раздел внизу или вернитесь в обзор.', '<a class="secondary full" href="#/">В обзор</a>')))());animateDashboard(previousBars,previousResources);document.querySelectorAll('[data-tab]').forEach(a=>{const selected=a.dataset.tab===({server:'home',client:'clients',admins:'settings',audit:'settings',daily:'stats'}[name]||name);a.classList.toggle('active',selected);if(selected)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});if(tg?.initData){if(name==='home')tg.BackButton.hide();else tg.BackButton.show()}}
async function loadReport(){const sid=state.route[1]||'all';if(!servers().length)return;const rev=state.revision,month=state.month,sort=state.sort,key=`${sid}:${month}:${sort}`;state.reportError='';try{const selected=sid==='all'?servers():servers().filter(s=>String(s.id)===sid);const reports=await Promise.all(selected.map(async s=>{const report=await api(`/servers/${s.id}/monthly?month=${encodeURIComponent(month)}&sort=${sort}`);return report.clients.map(c=>({...c,server_id:s.id,server_name:s.name}))}));const clients=reports.flat().sort((a,b)=>(sort==='rx'?b.rx-a.rx:sort==='tx'?b.tx-a.tx:(b.rx+b.tx)-(a.rx+a.tx)));if(rev===state.revision&&key===`${state.route[1]||'all'}:${state.month}:${state.sort}`){state.report={clients};render()}}catch(e){if(rev===state.revision&&month===state.month){state.reportError=e.message;render();notice(e.message,true)}}}
async function refresh(manual=false){if(state.loading)return;state.loading=true;$('#refresh').disabled=true;try{const hadData=Boolean(state.data);state.data=await api('/overview');if(!state.updates&&state.data.access?.owner)loadUpdates();const stale=state.data.servers.some(s=>s.stale||!s.ok);$('#live-label').textContent=stale?'Есть старые данные':'Live · 5 сек';document.querySelector('.live-pill')?.classList.toggle('disconnected',stale);notice(state.data.demo?'Демонстрационный режим · данные вымышлены':'');const focused=document.activeElement;const typing=focused?.matches('input,select')||$('#modal').open||state.route[0]==='settings';if(!typing||!hadData)render();if(state.route[0]==='admins'&&!$('#modal').open)await loadAdmins();if(state.route[0]==='stats'&&!typing)await loadReport();if(['daily','server','client'].includes(state.route[0])&&!typing)await loadDaily();if(state.route[0]==='server'&&!typing)await loadSpeedtest();if(manual&&state.route[0]==='audit')await loadAudit();if(manual)toast('Данные обновлены')}catch(e){$('#live-label').textContent='Нет связи';document.querySelector('.live-pill')?.classList.add('disconnected');notice(e.message,true);if(!state.data)$('#app').innerHTML=empty('Не удалось открыть панель',e.message,'<button class="primary" data-action="retry">Повторить</button>')}finally{state.loading=false;$('#refresh').disabled=false}}
async function route(){if($('#modal').open&&!state.busy){$('#modal').close();delete $('#modal').dataset.changelog;changelogReturn=null}const previous=state.route;const fragment=location.hash.slice(1);const next=/^tgWebApp[A-Za-z]+=/.test(fragment)?[]:fragment.split('?')[0].split('/').filter(Boolean);if(next[0]==='settings'&&previous[0]!=='settings'){if(!['admins','audit'].includes(previous[0]))state.settingsReturn='#/'+previous.join('/');beginPreferences()}if(previous[0]==='settings'&&next[0]!=='settings')cancelPreferences();if(previous[0]==='clients'&&next[0]==='client'){state.clientReturn='#/'+previous.join('/');state.clientScroll=window.scrollY}if(next[0]==='daily'&&!['daily','settings'].includes(previous[0])){state.dailyReturn='#/'+previous.join('/');state.dailyOrigin={month:state.month,sort:state.sort,scroll:window.scrollY}}if(previous[0]==='daily'&&'#/'+next.join('/')===state.dailyReturn&&state.dailyOrigin){state.month=state.dailyOrigin.month;state.sort=state.dailyOrigin.sort}state.daily=null;state.dailyError='';state.reportError='';state.dailyDay=null;state.revision++;state.route=next;state.report=null;render(true);if(state.data)$('#app').focus({preventScroll:true});window.scrollTo({top:state.route[0]==='clients'&&previous[0]==='client'?state.clientScroll:0,behavior:'instant'});if(state.route[0]==='stats'){await loadReport();if(previous[0]==='daily'&&state.dailyReturn==='#/'+state.route.join('/'))window.scrollTo({top:state.dailyOrigin?.scroll||0,behavior:'instant'})}if(['daily','server','client'].includes(state.route[0]))await loadDaily();if(state.route[0]==='admins')await loadAdmins();if(state.route[0]==='audit')await loadAudit();if(state.route[0]==='settings')loadUpdates();if(state.route[0]==='server')await loadSpeedtest()}
function goBack(){const [name,sid]=state.route;location.hash=name==='daily'?state.dailyReturn:['admins','audit'].includes(name)?'#/settings':name==='settings'?state.settingsReturn:name==='client'?state.clientReturn:name==='clients'&&sid?`/server/${sid}`:'/';haptic()}
function modal(title,content,submit,button='Сохранить',danger=false){const d=$('#modal');delete d.dataset.updateProgress;d.innerHTML=`<form id="dialog-form"><h2>${esc(title)}</h2>${content}<div id="form-error" class="hint danger" role="alert"></div><div class="toolbar"><button type="button" class="secondary" data-action="close">Отмена</button><button type="submit" class="${danger?'secondary danger':'primary'}">${esc(button)}</button></div></form>`;enhanceFormSelects(d);d.setAttribute('aria-label',title);d.showModal();$('#dialog-form').onsubmit=async e=>{e.preventDefault();if(state.busy)return;state.busy=true;const buttons=[...d.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);try{const next=await submit(new FormData(e.currentTarget));d.close();if(typeof next==='function'){next();return;}if(!updateActive())await refresh();toast(updateActive()?'Обновление запущено':'Готово');tg?.HapticFeedback?.notificationOccurred?.('success')}catch(e){$('#form-error').textContent=e.message;tg?.HapticFeedback?.notificationOccurred?.('error')}finally{state.busy=false;buttons.forEach(b=>b.disabled=false)}}}
function policyFields(c={}){return `<label>Срок действия<select name="duration" class="field"><option value="keep">${c.id?'Оставить текущий':'Без срока'}</option><option value="86400">1 день</option><option value="604800">7 дней</option><option value="2592000">30 дней</option>${c.id?'<option value="none">Без срока</option>':''}</select></label><label>Лимит трафика, Гб<input name="limit" type="number" min="0" max="1048576" step="0.1" class="field" placeholder="Без ограничения" value="${c.limit?c.limit/1024**3:''}"></label><p class="hint">Лимит применяется к накопленному трафику. При исчерпании лимита или срока конфиг удаляется.</p>`}
function values(form,c={}){const duration=form.get('duration'),limit=form.get('limit');return {duration:duration==='keep'?null:duration==='none'?null:Number(duration),keep_expiry:duration==='keep'&&!!c.id,limit:limit?Math.round(Number(limit)*1024**3):null}}
async function action(name,element){if(name==='changelog')return openChangelog();if(name==='changelog-back')return closeChangelog();if(name==='update-check')return loadUpdates(true);if(name==='update-open')return openUpdate();const [page,sid,cid]=state.route;const c=server(sid)?.clients.find(c=>c.id===Number(cid));if(name==='service-traffic')return openServiceTraffic(sid,page==='client'?cid:null);if(name==='retry-daily')return loadDaily();if(name==='retry-report')return loadReport();if(name==='audit-more')return loadAudit(Number(element.dataset.before));if(name==='retry')return refresh();if(name==='add-admin'){modal('Добавить администратора',`<label>Имя<input class="field" name="name" maxlength="64" required autocomplete="off"></label><label>Telegram ID<input class="field" name="id" inputmode="numeric" pattern="[0-9]+" required autocomplete="off"></label><p class="hint">Доступ к клиентам и управлению всеми VPS. Пользователь сможет скачивать конфиги и перезагружать серверы. Добавляйте только доверенных людей.</p>`,async form=>{await api('/admins',{method:'POST',body:JSON.stringify({id:Number(form.get('id')),name:form.get('name')})});await loadAdmins()},'Дать доступ');return}if(name==='remove-admin'){const uid=Number(element.dataset.uid),admin=state.admins?.admins.find(a=>a.id===uid);if(!admin)return;modal('Отозвать доступ?',`<p>${esc(admin.name)} · ID ${uid} потеряет доступ к боту и панели. Его VPN-конфиги продолжат работать.</p>`,async()=>{await api(`/admins/${uid}`,{method:'DELETE'});await loadAdmins()},'Отозвать',true);return}if(name==='admin-scope'){
  const admin=state.admins?.admins.find(a=>a.id===Number(element.dataset.uid));
  if(!admin)return;
  const operations=[['delete','Удалять клиентов и менять ограничения'],['reissue','Перевыпускать конфиги'],['reboot','Перезагружать сервер'],['pause','Приостанавливать и возобновлять доступ']];
  const check=(key,value,label,checked)=>`<label><input type="checkbox" name="${key}" value="${value}" ${checked?'checked':''}> ${esc(label)}</label>`;
  modal('Права администратора',`<p>${esc(admin.name)}</p><fieldset><legend>Серверы</legend>
    ${check('all','1','Все серверы, включая новые',admin.servers==null)}
    ${servers().map(s=>check('servers',s.id,s.name,admin.servers?.includes(s.id))).join('')}
    </fieldset><fieldset><legend>Действия</legend>${operations.map(([key,label])=>check('operations',key,label,admin.operations==null||admin.operations.includes(key))).join('')}</fieldset>`,async form=>{
    await api(`/admins/${admin.id}`,{method:'PATCH',body:JSON.stringify({
      servers:form.has('all')?null:form.getAll('servers').map(Number),operations:form.getAll('operations')
    })});
    await loadAdmins();
  });
  return;
}
if(name==='pause-client'&&c){
  const paused=!c.paused;
  modal(paused?'Приостановить доступ?':'Возобновить доступ?',`<p>${esc(c.name)}. Конфиг и статистика сохранятся.</p>`,async()=>{
    await api(`/servers/${sid}/clients/${cid}/pause`,{method:'PATCH',body:JSON.stringify({paused})});
  },paused?'Приостановить':'Возобновить',paused);
  return;
}
if(name==='service-traffic-settings')return openServiceTrafficSettings();
if(name==='resource-alerts'){
  try{
    const limits=await api('/resource-alerts');
    if(state.route[0]!=='settings')return;
    modal('Оповещения по ресурсам',`<label>Заполнение диска, %<input class="field" name="disk" type="number" min="0" max="100" required value="${limits.disk}"></label>
      <label>Нагрузка CPU, %<input class="field" name="cpu" type="number" min="0" max="100" required value="${limits.cpu}"></label>
      <p class="footnote">0 отключает оповещение. Бот пишет владельцу, когда диск достигает порога или CPU держится выше порога 3 минуты. Когда показатель снизится, бот сообщит об этом.</p>`,async form=>{
      await api('/resource-alerts',{method:'PATCH',body:JSON.stringify({disk:Number(form.get('disk')),cpu:Number(form.get('cpu'))})});
    });
  }catch(error){toast(error.message)}
  return;
}
if(name==='restore-open'){
  const options=servers().map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');
  $('#modal').close();
  modal('Восстановить клиентов из бэкапа',`<label>Сервер<select class="field" name="server">${options}</select></label>
    <label>Архив ZIP<input class="field" type="file" name="archive" accept=".zip" required></label>
    <p class="footnote">Из архива будут добавлены отсутствующие VPN-клиенты. База менеджера и действующие конфиги сохранятся.</p>
    `,async form=>{
    const target=Number(form.get('server'));
    const archive=form.get('archive');
    const preview=await api(`/servers/${target}/restore/preview`,{method:'POST',body:archive,headers:{'Content-Type':'application/zip'}},120000);
    return ()=>{
      if(state.route[0]!=='settings'||$('#modal').open)return;
      const list=items=>items.length?`<ul>${items.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>`:'<p>Нет</p>';
      const body=`<h3>Будут добавлены</h3>${list(preview.add)}<h3>Пропущены из-за конфликта адресов</h3>${list(preview.conflicts)}`;
      modal('Предпросмотр восстановления',body,async()=>{
        await api(`/servers/${target}/restore`,{method:'POST',body:JSON.stringify({token:preview.token})},120000);
      },'Восстановить',true);
      $('#dialog-form button[type="submit"]').disabled=!preview.add.length;
    };
  },'Проверить архив');
  return;
}
if(name==='client-labels'&&c){
  const fields=[['owner','Владелец'],['device','Устройство'],['group','Группа'],['note','Комментарий']];
  modal('Описание клиента',fields.map(([key,title])=>`<label>${title}<input class="field" name="${key}" maxlength="${key==='note'?250:64}" value="${esc(c.labels?.[key]||'')}"></label>`).join(''),async form=>{
    await api(`/servers/${sid}/clients/${cid}/labels`,{method:'PATCH',body:JSON.stringify(Object.fromEntries(form))});
  });
  return;
}
if(name==='backup-schedule'&&state.data?.access?.owner){
  try{
    const {schedule,history=[]}=await api('/backup-schedule');
    if(state.route[0]!=='settings')return;
    const ids=schedule?.server_ids??[schedule?.server_id];
    const options=[3,6,9,12,24].map(hours=>`<option value="${hours}" ${schedule?.hours===hours?'selected':''}>Каждые ${hours} ч</option>`).join('');
    const targets=servers().map(s=>`<label><input type="checkbox" name="servers" value="${s.id}" ${ids.includes(s.id)?'checked':''}> ${esc(s.name)}</label>`).join('');
    const manual=servers().map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');
    const stamp=at=>esc(new Date(at*1000).toLocaleString('ru-RU'));
    const next=schedule?`<p class="footnote">Следующая попытка: ${stamp(schedule.next_at)}</p>`:'';
    const events=history.slice(0,8).map(event=>`<li>${stamp(event.at)} · ${esc(server(event.server_id)?.name||'Сервер '+event.server_id)} · ${event.automatic?'По расписанию':'Вручную'} · ${event.status==='sent'?'Отправлен':'Ошибка отправки'}</li>`).join('');
    modal('Резервные копии',`<label>Отправить сейчас<select class="field" name="manual">${manual}</select></label><button type="button" class="secondary" data-action="backup-send" ${manual?'':'disabled'}>Отправить в Telegram</button><button type="button" class="secondary" data-action="restore-open" ${manual?'':'disabled'}>Восстановить из ZIP</button><label>Интервал<select class="field" name="hours"><option value="">Выключено</option>${options}</select></label><fieldset><legend>Серверы для автоотправки</legend>${targets||'Нет серверов'}</fieldset>${next}<p class="footnote">ZIP с ключами и SSH-реквизитами придёт в личный чат владельца с ботом.</p><h3>История отправок</h3>${events?`<ul>${events}</ul>`:'<p class="footnote">Отправок пока нет.</p>'}`,async form=>{
      const hours=Number(form.get('hours'))||null;
      await api('/backup-schedule',{method:'PATCH',body:JSON.stringify({hours,server_ids:form.getAll('servers').map(Number)})});
      toast(hours?'Расписание сохранено':'Автоотправка отключена');
    });
  }catch(error){toast(error.message)}
  return;
}
if(name==='backup-send'&&state.data?.access?.owner){
  const form=$('#dialog-form'), id=Number(new FormData(form).get('manual'));
  if(!id||state.busy)return;
  element.disabled=true;
  state.busy=true;
  try{
    await api(`/servers/${id}/backup`,{method:'POST'},120000);
    toast('Бэкап отправлен в Telegram');
  }catch(error){toast(error.message)}
  finally{state.busy=false;element.disabled=false}
  return;
}
if(name==='add-server'){modal('Подключить сервер',`<p class="hint">На сервере уже должен работать AmneziaWG. Вторая копия бота не нужна. Перед сохранением проверим SSH и контейнер.</p><label>Название<input class="field" name="name" maxlength="48" required placeholder="Например, Германия"></label><label>SSH-подключение<input class="field" name="connection" required placeholder="root@203.0.113.10:22" autocomplete="off" autocapitalize="off" spellcheck="false"></label><label>Авторизация<select class="field" name="auth_type" id="server-auth"><option value="password">Пароль SSH</option><option value="key">SSH-ключ на машине с ботом</option></select></label><label id="server-password">Пароль<input class="field" type="password" name="password" autocomplete="new-password"></label><label id="server-key" hidden>Путь к SSH-ключу<input class="field" name="key_path" placeholder="/root/.ssh/id_ed25519" autocomplete="off"></label><details class="server-advanced"><summary>Параметры AmneziaWG</summary><label>VPN-адрес<input class="field" name="endpoint" placeholder="По умолчанию SSH-адрес"></label><label>Контейнер<input class="field" name="container" value="amnezia-awg"></label><label>Путь к конфигу в контейнере<input class="field" name="config_path" value="/opt/amnezia/awg/wg0.conf"></label></details>`,async form=>{const result=await api('/servers',{method:'POST',body:JSON.stringify(Object.fromEntries(form))});location.hash=`/server/${result.id}`},'Проверить и подключить');return}if(name==='speed-check'||name==='speed-run'){try{state.speedtests[sid]=await api(`/servers/${sid}/speedtest`,{method:'POST',body:JSON.stringify({action:name==='speed-check'?'check':'run'})});render()}catch(e){toast(e.message)}return}if(name==='reboot'){try{const confirmation=await api(`/servers/${sid}/reboot/prepare`,{method:'POST',body:'{}'});modal('Перезагрузить VPS?',`<p>Сервер <b>${esc(confirmation.server)}</b> перезагрузится через 10 секунд. VPN всех его клиентов отключится до запуска сервера. Бот и панель тоже могут временно отключиться.</p>`,async form=>{await api(`/servers/${sid}/reboot`,{method:'POST',body:JSON.stringify({nonce:confirmation.nonce,name:confirmation.server})});state.reboots[sid]=Date.now()},'Перезагрузить',true)}catch(e){toast(e.message)}return}if(name==='speed-install'){modal('Установить Speedtest?',`<p>На ${esc(server(sid)?.name)} будет установлен пакет speedtest-cli из системного репозитория Ubuntu/Debian. VPN продолжит работу.</p>`,async()=>{state.speedtests[sid]=await api(`/servers/${sid}/speedtest`,{method:'POST',body:JSON.stringify({action:'install'})})},'Установить');return}if(name==='close'){if(!state.busy)$('#modal').close();return}if(name==='create'){if(!servers().length)return toast(state.data?.access?.owner?'Сначала подключите сервер: Обзор → плюс':'Попросите владельца подключить сервер');modal('Новый клиент',`<label>Сервер<select name="server" class="field">${servers().map(s=>`<option value="${s.id}" ${String(s.id)===(sid||state.serverFilter)?'selected':''}>${esc(s.name)}</option>`).join('')}</select></label><label>Имя<input class="field" name="name" placeholder="Например, MacBook" maxlength="64" required autocomplete="off"></label>${policyFields()}`,async form=>{await api(`/servers/${form.get('server')}/clients`,{method:'POST',body:JSON.stringify({name:form.get('name'),...values(form)})})},'Создать');return}if(name==='policy'&&c){modal('Ограничения',policyFields(c),async form=>{await api(`/servers/${sid}/clients/${cid}/policy`,{method:'PATCH',body:JSON.stringify(values(form,c))})});return}if(name==='delete'&&c){modal('Удалить конфиг?',`<p>${esc(c.name)} потеряет доступ к VPN. Статистика останется в месячных отчётах.</p>`,async()=>{await api(`/servers/${sid}/clients/${cid}`,{method:'DELETE'});location.hash=state.clientReturn},'Удалить',true);return}if(name==='reissue'&&c){try{const confirmation=await api(`/servers/${sid}/clients/${cid}/reissue/prepare`,{method:'POST',body:'{}'});modal('Перевыпустить конфиг?',`<p><b>${esc(confirmation.name)}</b> · ${esc(confirmation.server)}</p><p class="footnote">Старый конфиг перестанет работать. Имя, VPN-адрес, лимиты, срок и статистика сохранятся. Новый файл останется доступен для повторного скачивания.</p>`,async()=>{await api(`/servers/${sid}/clients/${cid}/reissue`,{method:'POST',body:JSON.stringify({nonce:confirmation.nonce})})},'Перевыпустить',true)}catch(e){toast(e.message)}return}if(name==='config'&&c){try{const r=await api(`/servers/${sid}/clients/${cid}/config`);download(`amnezia-${cid}.conf`,r.config)}catch(e){toast(e.message)}return}if(name==='csv'&&state.report){const cell=v=>'"'+String(v).replaceAll('"','""')+'"';const safe=v=>/^[=+\-@\t\r\n]/.test(v)?"'"+v:v;const lines=[['Сервер','Конфиг','VPN-адрес','Статус','Получено клиентом, байт','Отправлено клиентом, байт','Всего, байт'],...state.report.clients.map(c=>[safe(c.server_name||''),safe(c.name),safe(c.ip),c.deleted?'удалён':'действующий',c.rx,c.tx,c.rx+c.tx])];download(`traffic-${state.route[1]||'all'}-${state.month}.csv`,'\ufeff'+lines.map(row=>row.map(cell).join(';')).join('\r\n'),'text/csv;charset=utf-8')}}
document.addEventListener('click',e=>{const comparison=e.target.closest('[data-comparison]');if(comparison){comparisonMode=comparison.dataset.comparison;render();document.querySelector(`[data-comparison="${comparisonMode}"]`)?.focus({preventScroll:true});return}const step=e.target.closest('[data-month-step]');if(step&&!step.disabled){const [year,month]=state.month.split('-').map(Number),n=year*12+month-1+Number(step.dataset.monthStep),next=`${String(Math.floor(n/12)).padStart(4,'0')}-${String(n%12+1).padStart(2,'0')}`;if(next>='0001-01'&&next<=currentMonth()){state.month=next;state.daily=null;state.dailyDay=null;haptic();render();loadDaily()}return}const day=e.target.closest('[data-day]');if(day){state.dailyDay=day.dataset.day;haptic();render()}const metric=e.target.closest('[data-metric]');if(metric){state.metricMode=metric.dataset.metric;localStorage.setItem('amnezia-metrics',state.metricMode);haptic();render()}const a=e.target.closest('[data-action]'),filter=e.target.closest('[data-filter]'),sort=e.target.closest('[data-sort]');if(a)action(a.dataset.action,a);if(e.target.closest('[data-online-link]')){state.filter='online';state.search=''}if(filter){state.filter=filter.dataset.filter;haptic();render()}if(sort){state.sort=sort.dataset.sort;haptic();loadReport()}});
document.addEventListener('input',e=>{if(e.target.id==='search'){state.search=e.target.value;patchHTML($('#client-results'),clientGrid())}});
document.addEventListener('change',e=>{if(e.target.id==='update-notifications'){const toggle=e.target,previous=state.updates?.enabled!==false;toggle.disabled=true;api('/updates/preferences',{method:'PATCH',body:JSON.stringify({enabled:toggle.checked})}).then(u=>{state.updates=u;document.querySelector('[data-tab=settings]').classList.toggle('has-update',Boolean(u.enabled&&u.release))}).catch(err=>{toggle.checked=previous;toast(err.message)}).finally(()=>toggle.disabled=false)}if(e.target.id==='server-auth'){$('#server-password').hidden=e.target.value!=='password';$('#server-key').hidden=e.target.value!=='key'}if(e.target.id==='client-server'){state.serverFilter=e.target.value;render()}if(e.target.id==='stats-server'){location.hash=`/stats/${e.target.value}`}if(e.target.id==='daily-month'){if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(e.target.value)||e.target.value>currentMonth())return toast('Выберите месяц не позднее текущего');state.month=e.target.value;state.daily=null;state.dailyDay=null;render();loadDaily()}if(e.target.id==='stats-month'){if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(e.target.value)||e.target.value>currentMonth())return toast('Выберите месяц не позднее текущего');state.month=e.target.value;state.report=null;render();loadReport()}});
$('#refresh').innerHTML=icon('refresh');$('#refresh').onclick=()=>refresh(true);$('#theme').innerHTML=icon('settings');$('#theme').onclick=()=>{location.hash='/settings'};
for(const [tab,name,label] of [['home','grid','Обзор'],['clients','users','Клиенты'],['stats','chart','Статистика'],['settings','settings','Настройки']])$(`[data-tab=${tab}]`).innerHTML=icon(name)+`<span>${label}</span>`;
window.addEventListener('hashchange',route);document.addEventListener('visibilitychange',()=>{active=!document.hidden;if(active)refresh()});
if(tg?.initData){tg.ready();tg.expand();tg.onEvent('themeChanged',theme);tg.onEvent('safeAreaChanged',safeArea);tg.onEvent('contentSafeAreaChanged',safeArea);tg.onEvent('activated',()=>{active=true;refresh()});tg.onEvent('deactivated',()=>active=false);tg.BackButton.onClick(goBack)}else{document.body.classList.add('preview');$('#preview-bar').hidden=false}
// Edge gesture ignores forms and horizontal controls.
let swipe=null;document.addEventListener('touchstart',e=>{const t=e.touches[0];if(e.touches.length===1&&t.clientX<32&&state.route.length&&!e.target.closest('input,select,textarea,.chips,dialog'))swipe={x:t.clientX,y:t.clientY}}, {passive:true});document.addEventListener('touchmove',e=>{if(!swipe)return;const t=e.touches[0],dx=t.clientX-swipe.x,dy=Math.abs(t.clientY-swipe.y);if(dx>10&&dx>dy*1.5){const p=Math.min(dx,140)/140;Object.assign($('#swipe').style,{opacity:'1',width:`${12+30*p}px`,height:`${100+112*p}px`,top:`${t.clientY-(100+112*p)/2}px`});swipe.dx=dx}else if(dy>20)swipe=null},{passive:true});document.addEventListener('touchend',()=>{if(swipe?.dx>=72)goBack();swipe=null;$('#swipe').style.opacity='0'},{passive:true});
document.addEventListener('click',e=>{const b=e.target.closest('[data-pref]');if(b){changePreference(b.dataset.pref,b.dataset.value);render()}if(e.target.closest('[data-pref-reset]')){resetPreferences();render()}if(e.target.closest('[data-pref-cancel]')){cancelPreferences();location.hash=state.settingsReturn}});
document.addEventListener('change',e=>{if(e.target.id==='motion-toggle'){changePreference('motion',e.target.checked);render()}if(e.target.id==='accent-color'){changePreference('customAccent',e.target.value);render()}});
document.addEventListener('submit',async e=>{if(e.target.id!=='preferences-form')return;e.preventDefault();const form=e.target;form.querySelectorAll('button,input').forEach(b=>b.disabled=true);try{await savePreferences(api);location.hash=state.settingsReturn;toast('Настройки сохранены')}catch(error){$('#preferences-error').textContent=error.message}finally{form.querySelectorAll('button,input').forEach(b=>b.disabled=false)}});
$('#modal').addEventListener('cancel',e=>{if($('#modal').dataset.changelog==='true'){e.preventDefault();closeChangelog()}else if(state.busy)e.preventDefault()});theme();await loadPreferences(api);await route();await refresh();timer=setInterval(()=>{if(active&&!document.hidden)refresh()},5000);

document.addEventListener('input',e=>{if(e.target.matches('[data-daily-picker]')&&state.daily){state.dailyDay=state.daily.days[Number(e.target.value)]?.day;render()}});

// Hover selects the closest day. Touch and keyboard use the native range control.
document.addEventListener('pointermove',e=>{const picker=e.target.closest?.('[data-daily-picker]');if(!picker||e.pointerType!=='mouse'||!state.daily)return;const rect=picker.getBoundingClientRect();const index=Math.max(0,Math.min(state.daily.days.length-1,Math.round((e.clientX-rect.left)/rect.width*(state.daily.days.length-1))));const day=state.daily.days[index].day;if(day!==state.dailyDay){state.dailyDay=day;render()}});
