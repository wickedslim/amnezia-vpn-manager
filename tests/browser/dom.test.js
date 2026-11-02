
import {patchHTML,patchElement} from '/assets/dom.js';
import {applyPreferences,defaults} from '/assets/preferences.js';
const box=document.querySelector('#fixture'),results=document.querySelector('#results');let failures=0;
function test(name,fn){const item=document.createElement('li');try{fn();item.textContent='OK · '+name}catch(e){failures++;item.textContent='FAIL · '+name+': '+e.message}results.append(item)}
function assert(condition,message){if(!condition)throw Error(message)}
test('Числа обновляются без замены кнопки и потери фокуса',()=>{patchHTML(box,'<button data-filter="all">Все: 1</button>');const button=box.firstChild;button.focus();patchHTML(box,'<button data-filter="all">Все: 2</button>');assert(box.firstChild===button,'заменён узел');assert(document.activeElement===button,'потерян фокус');assert(button.textContent==='Все: 2','число не обновлено')});
test('Перестановка и удаление клиентов сохраняют узлы',()=>{patchHTML(box,'<a href="#a">A</a><a href="#b">B</a>');const a=box.firstChild,b=box.lastChild;patchHTML(box,'<a href="#b">B2</a><a href="#a">A2</a>');assert(box.firstChild===b&&box.lastChild===a,'узлы заменены');patchHTML(box,'<a href="#b">B3</a>');assert(box.childNodes.length===1&&box.firstChild===b,'удаление неверно')});
test('Ввод и позиция курсора переживают обновление',()=>{patchHTML(box,'<input id="query" value="start">');const input=box.firstChild;input.focus();input.value='typing';input.setSelectionRange(2,2);patchHTML(box,'<input id="query" value="server">');assert(box.firstChild===input&&input.value==='typing'&&input.selectionStart===2,'потерян ввод');input.blur();patchHTML(box,'<input id="query" value="server">');assert(input.value==='server','контролируемое значение не обновилось')});
test('Переключатели, SVG и частичная карточка обновляются',()=>{patchHTML(box,'<section id="card"><input type="checkbox"><svg><circle r="10"/></svg></section>');const card=box.firstChild,circle=card.querySelector('circle');patchElement(card,'<section id="card"><input type="checkbox" checked><svg><circle r="20"/></svg></section>');assert(card===box.firstChild,'карточка заменена');assert(card.querySelector('input').checked,'checkbox не обновлён');assert(card.querySelector('circle')===circle&&circle.getAttribute('r')==='20','SVG заменён')});
test('Контраст кнопок во всех стандартных палитрах и темах',()=>{
 const lum=c=>{const v=c.match(/[\d.]+/g).slice(0,3).map(Number).map(n=>n/255).map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4);return v.reduce((n,x,i)=>n+x*[.2126,.7152,.0722][i],0)};
 patchHTML(box,'<button class="primary">Проверка</button>');
 for(const mode of ['light','dark'])for(const palette of ['brand','lime','pink']){
  applyPreferences({...defaults,mode,palette});const style=getComputedStyle(box.firstChild),a=lum(style.color),b=lum(style.backgroundColor);assert((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5,mode+' '+palette);
 }
});
box.replaceChildren();document.body.dataset.status=failures?'failed':'passed';const summary=document.createElement('p');summary.textContent=failures?'Есть ошибки':'Все 5 проверок пройдены';document.body.append(summary);
