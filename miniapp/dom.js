// Patch existing nodes so focus, selection, scrolling and animations survive polling.
function key(node) {
  if (node.nodeType !== 1) return null;
  for (const attr of ['id','data-report-client','data-resource','href','data-action','data-filter','data-sort','data-metric']) {
    if (node.hasAttribute(attr)) return `${node.tagName}:${attr}:${node.getAttribute(attr)}`;
  }
  if (node.hasAttribute('data-pref')) return `${node.tagName}:pref:${node.dataset.pref}:${node.dataset.value}`;
  return null;
}
function compatible(a,b) { return a && a.nodeType===b.nodeType && a.nodeName===b.nodeName && key(a)===key(b); }
function patchNode(current,next) {
  if (current.nodeType !== 1) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue=next.nodeValue;
    return;
  }
  for (const attr of [...current.attributes]) if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
  for (const attr of next.attributes) if (current.getAttribute(attr.name)!==attr.value) current.setAttribute(attr.name,attr.value);
  patchChildren(current,next);
  if (current instanceof HTMLInputElement) {
    if (current.type==='checkbox' || current.type==='radio') current.checked=next.checked;
    else if (current!==document.activeElement && current.value!==next.value) current.value=next.value;
  }
  if (current instanceof HTMLSelectElement && current!==document.activeElement) current.value=next.value;
}
function patchChildren(current,next) {
  const keyed=new Map([...current.childNodes].filter(n=>key(n)).map(n=>[key(n),n]));
  let cursor=current.firstChild;
  for (const desired of [...next.childNodes]) {
    let node=key(desired)?keyed.get(key(desired)):cursor;
    if (!compatible(node,desired)) node=desired.cloneNode(true);
    if (node!==cursor) current.insertBefore(node,cursor);
    patchNode(node,desired);
    cursor=node.nextSibling;
  }
  while (cursor) { const nextNode=cursor.nextSibling; current.removeChild(cursor); cursor=nextNode; }
}
export function patchHTML(element,html) {
  const template=document.createElement('template'); template.innerHTML=html;
  patchChildren(element,template.content);
}
export function patchElement(element,html) {
  const template=document.createElement('template'); template.innerHTML=html;
  const next=template.content.firstElementChild;
  if (compatible(element,next)) patchNode(element,next);
  else element.replaceWith(next);
}
