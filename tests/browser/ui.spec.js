import {test,expect} from '@playwright/test';
test('DOM updates retain focus, input, ordering and SVG',async({page})=>{
  await page.goto('/_qa/dom');await expect(page.locator('body')).toHaveAttribute('data-status','passed');
});
test('client filters survive detail and return',async({page})=>{
  await page.goto('/#/clients');const search=page.getByRole('searchbox');await search.fill('Kolida');
  await page.locator('#client-server-menu summary').click();
  await page.locator('#client-server-menu [data-filter-option="1"]').click();
  await page.locator('.client-card').filter({hasText:'Kolida1'}).click();
  await expect(page.locator('.daily-card .daily-line-chart')).toBeVisible();
  await expect(page.locator('.daily-card #daily-month')).toBeVisible();
  await expect(page.locator('a[href^="#/daily/"]')).toHaveCount(0);
  await page.locator('.back-link').click();
  await expect(search).toHaveValue('Kolida');await expect(page.locator('#client-server')).toHaveValue('1');
});
test('settings keep focus and honor reduced motion',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await page.goto('/#/settings');
  const dark=page.getByRole('button',{name:'Тёмная',exact:true});await dark.click();await expect(dark).toBeFocused();
  await expect(page.locator('html')).toHaveAttribute('data-motion','off');
});
for(const width of [320,393])test(`layout and targets at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:852});await page.goto('/#/clients');
  await expect(page.locator('#search')).toBeVisible();
  expect(await page.locator('#search').evaluate(e=>e.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44);
  for(const chip of await page.locator('.chip').all())expect((await chip.boundingBox()).height).toBeGreaterThanOrEqual(44);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
});
test('late monthly response cannot replace a different route',async({page})=>{
  let release;const gate=new Promise(r=>release=r);
  await page.route('**/api/servers/*/monthly?**',async route=>{await gate;await route.continue()});
  await page.goto('/#/stats');await expect(page.locator('#stats-month-menu summary')).toBeVisible();
  await page.locator('[data-tab="clients"]').click();release();
  await expect(page.getByRole('heading',{name:'Клиенты',exact:true})).toBeVisible();
  await expect(page.locator('#search')).toBeVisible();
});
test('network error leaves navigation and retry available',async({page})=>{
  await page.goto('/#/clients');await expect(page.locator('#search')).toBeVisible();
  await page.route('**/api/overview',route=>route.abort());await page.locator('#refresh').click();
  await expect(page.locator('#notice')).toHaveClass(/error/);await expect(page.locator('#refresh')).toBeEnabled();
  await page.locator('[data-tab="settings"]').click();await expect(page.getByRole('heading',{name:'Настройки',exact:true})).toBeVisible();
});
test('monthly summary shows download and upload for the selected month',async({page})=>{
  await page.route('**/api/servers/*/monthly?**',route=>route.fulfill({json:{clients:[
    {id:1,name:'One',ip:'10.0.0.2',rx:1024,tx:2048,deleted:false},
    {id:2,name:'Two',ip:'10.0.0.3',rx:3072,tx:1024,deleted:false}
  ]}}));
  await page.goto('/#/stats/1');
  await expect(page.locator('.monthly-directions dd').nth(0)).toHaveText('4 Кб');
  await expect(page.locator('.monthly-directions dd').nth(1)).toHaveText('3 Кб');
  await expect(page.locator('.stats-summary .big-number')).toHaveText('7Кб');
  expect((await page.locator('.stats-summary').boundingBox()).height).toBeLessThan(350);
});
test('all servers monthly scope aggregates distinct peers',async({page})=>{
  await page.route('**/api/servers/*/monthly?**',route=>route.fulfill({json:{clients:[{id:1,name:'Same',ip:'10.0.0.2',rx:1024,tx:2048,deleted:false}]}}));
  await page.goto('/#/stats');
  await expect(page.locator('#stats-server')).toHaveValue('all');
  await expect(page.locator('.stats-summary .big-number')).toHaveText('6Кб');
  await expect(page.locator('[data-report-client]')).toHaveCount(2);
  await page.locator('#stats-server-menu summary').click();
  await page.locator('#stats-server-menu [data-filter-option="1"]').click();
  await expect(page.locator('.stats-summary .big-number')).toHaveText('3Кб');
});
for(const width of [320,393])test(`daily line chart drilldown at ${width}px`,async({page})=>{
 await page.setViewportSize({width,height:852});
 await page.goto('/#/stats/1');
 await page.locator('[data-report-client]').first().click();
 await expect(page.locator('.daily-card')).toBeVisible();
 await expect(page.locator('#daily-month')).toBeVisible();
 const picker=page.locator('[data-daily-picker]');await picker.focus();await picker.press('Home');
 await expect(picker).toHaveValue('0');
 await picker.press('ArrowRight');await expect(picker).toHaveValue('1');
 await expect(page.locator('.daily-detail h3')).toContainText('2');
 await expect(page.locator('.daily-line-chart')).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
 await page.locator('.back-link').click();
 await expect(page.locator('#stats-server')).toHaveValue('1');
 await page.goto('/#/server/1');
 await expect(page.locator('.server-daily .daily-card')).toBeVisible();
 await expect(page.locator('a[href="#/daily/1"]')).toHaveCount(0);
 expect(await page.locator('.server-daily').evaluate(el=>el.previousElementSibling.textContent.includes('Скорость трафика'))).toBeTruthy();
});

test('daily lines leave unobserved gaps and allow selecting them',async({page})=>{
 await page.route('**/daily?month=*',route=>route.fulfill({json:{name:'Demo',server_name:'Server',since:Date.now()/1000,days:[
 {day:'2026-11-01',rx:10,tx:2,observed:true},
 {day:'2026-11-02',rx:20,tx:3,observed:true},
 {day:'2026-11-03',rx:0,tx:0,observed:false},
 {day:'2026-11-04',rx:30,tx:5,observed:true},
 {day:'2026-11-05',rx:40,tx:8,observed:true}]}}));
 await page.goto('/#/daily/1');
 await expect(page.locator('.daily-line.rx')).toHaveCount(2);
 await expect(page.locator('.daily-area.rx')).toHaveCount(2);
 await expect(page.locator('.daily-area.tx')).toHaveCount(2);
 const picker=page.locator('[data-daily-picker]');await picker.focus();await picker.press('Home');await picker.press('ArrowRight');await picker.press('ArrowRight');
 await expect(page.locator('.daily-detail')).toContainText('Нет замеров');
 await expect(page.locator('.daily-selected')).toHaveCount(0);
});

test('server inline chart hover shows traffic tooltip and respects motion preference',async({page})=>{
 await page.goto('/#/server/1');
 const picker=page.locator('[data-daily-picker]');await expect(picker).toBeVisible();
 await picker.scrollIntoViewIfNeeded();const rect=await picker.boundingBox();await page.mouse.move(rect.x+rect.width*.5,rect.y+rect.height*.5);
 await expect(page.locator('.server-daily .daily-tooltip')).toBeVisible();
 await expect(page.locator('.daily-detail h3')).toHaveText(await page.locator('.daily-tooltip strong').innerText());
 await page.mouse.move(0,0);await expect(page.locator('.daily-tooltip')).toBeHidden();
 await page.goto('/#/stats');const card=page.locator('.report-link').first();await card.hover();
 expect(await card.evaluate(el=>getComputedStyle(el).transform)).not.toBe('none');
 await page.emulateMedia({reducedMotion:'reduce'});
 await expect.poll(()=>card.evaluate(el=>getComputedStyle(el).transform)).toBe('none');
});
test('month switcher navigates without allowing future months',async({page})=>{
 await page.goto('/#/server/1');
 const picker=page.locator('#daily-month');await expect(picker).toBeVisible();
 const current=await picker.inputValue();
 await expect(page.getByRole('button',{name:'Следующий месяц',exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'Предыдущий месяц',exact:true}).click();
 await expect(picker).not.toHaveValue(current);
 await page.getByRole('button',{name:'Следующий месяц',exact:true}).click();
 await expect(picker).toHaveValue(current);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
});

test('card hover moves without changing surface color or shadow',async({page})=>{
 for(const [route,selector] of [['/','a.server-card'],['/#/clients','.client-card'],['/#/stats','.report-link']]){
  await page.goto(route);const card=page.locator(selector).first();await expect(card).toBeVisible();
  await page.mouse.move(0,0);
  const before=await card.evaluate(el=>{const s=getComputedStyle(el);return {bg:s.backgroundColor,shadow:s.boxShadow}});
  await card.hover();
  await expect.poll(()=>card.evaluate(el=>getComputedStyle(el).transform)).toBe('matrix(1, 0, 0, 1, 0, -3)');
  const after=await card.evaluate(el=>{const s=getComputedStyle(el);return {bg:s.backgroundColor,shadow:s.boxShadow}});
  expect(after).toEqual(before);
 }
});

test('daily totals follow the chart and navigation uses the accent',async({page})=>{
 await page.goto('/#/daily/1/9');await expect(page.locator('.daily-card .daily-line-chart')).toBeVisible();
 await expect(page.locator('.daily-card .metric-pair')).toHaveCount(1);
 await expect(page.locator('.daily-detail .metric-pair')).toBeVisible();
 await page.goto('/#/settings');await page.locator('[data-pref=mode][data-value=dark]').click();
 await expect(page.locator('.bottom-nav a.active')).toHaveCSS('color','rgb(255, 67, 2)');
 await expect(page.locator('.preference-segment [aria-pressed=true]').first()).toHaveCSS('color','rgb(255, 255, 255)');
});
test('owner update form has real stages and follows reduced motion',async({page})=>{
 await page.goto('/#/settings');await expect(page.locator('.update-section')).toBeVisible();
 await page.getByRole('button',{name:'Обновить',exact:true}).click();
 await expect(page.locator('#modal')).toContainText('Обновить менеджер?');
 await page.locator('#modal').getByRole('button',{name:'Обновить',exact:true}).click();
 await expect(page.locator('.update-steps')).toBeVisible();
 await expect(page.locator('.update-motion')).toHaveClass(/running/);
 await page.emulateMedia({reducedMotion:'reduce'});
 await expect(page.locator('.update-motion svg')).toHaveCSS('animation-name','none');
 await page.getByRole('button',{name:'Свернуть',exact:true}).click();
 await expect(page.locator('#modal')).not.toBeVisible();
});

test('what is new stays in app and returns to the update confirmation',async({page})=>{
 await page.goto('/#/settings');await expect(page.locator('.update-section')).toBeVisible();
 await page.getByRole('button',{name:'Обновить',exact:true}).click();
 await page.locator('#modal').getByRole('button',{name:'Что нового?',exact:true}).click();
 await expect(page.locator('#modal h2')).toHaveText('Что нового?');
 await expect(page.locator('.changelog-content')).toContainText('Changelog');
 await page.locator('#modal').getByRole('button',{name:'Назад',exact:true}).click();
 await expect(page.locator('#modal h2')).toHaveText('Обновить менеджер?');
 await expect(page.locator('#modal').getByRole('button',{name:'Обновить',exact:true})).toBeEnabled();
 await expect(page).toHaveURL(/#\/settings$/);
});
