import {test,expect} from '@playwright/test';
const routes=['/','/server/1','/clients','/client/1/9','/stats','/daily/1/9','/settings','/admins','/audit'];
const fit=async page=>{
 const bad=await page.locator('body').evaluate(b=>[...b.querySelectorAll('main,main .surface,main .field,main .filter-menu,dialog[open],dialog[open] .field')].filter(e=>e.getClientRects().length).filter(e=>{const r=e.getBoundingClientRect();return r.left<-.5||r.right>innerWidth+.5}).map(e=>e.className||e.id));expect(bad).toEqual([]);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
};
for(const width of [320,393,520])for(const mode of ['light','dark'])test(`all screens ${width}px ${mode}, large type`,async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/preferences',r=>r.fulfill({json:{mode,fontSize:16,motion:false}}));await page.setViewportSize({width,height:852});
 for(const path of routes){await page.goto('/#'+path);await expect(page.locator('#app h1')).toBeVisible();if(path==='/stats')await expect(page.locator('.report-link').first()).toBeVisible();if(['/daily/','/client/','/server/'].some(p=>path.includes(p)))await expect(page.locator('.daily-line-chart')).toBeVisible();await fit(page);await expect(page.locator('.bottom-nav [aria-current=page]')).toHaveCount(1)}expect(errors).toEqual([]);
});
test('form dropdown keyboard, payload and cancel',async({page})=>{
 await page.setViewportSize({width:320,height:740});await page.goto('/#/clients');const add=page.getByRole('button',{name:'Добавить клиента',exact:true});await add.click();const d=page.locator('#modal');await fit(page);await expect(d.locator('select:visible')).toHaveCount(0);
 const server=d.locator('#form-server-menu summary');await server.focus();await server.press('ArrowDown');await page.keyboard.press('End');await page.keyboard.press('Enter');await expect(d.locator('select[name=server]')).toHaveValue('2');await expect(server).toContainText('Skystark_awg');await d.getByLabel('Имя',{exact:true}).fill('Audit client');await d.locator('#form-duration-menu summary').click();await d.locator('[data-filter-option="604800"]').click();
 expect(await d.locator('form').evaluate(f=>Object.fromEntries(new FormData(f)))).toMatchObject({server:'2',duration:'604800',name:'Audit client'});
 await d.locator('#form-duration-menu summary').focus();await page.keyboard.press('ArrowDown');await page.keyboard.press('Escape');await expect(d).toBeVisible();await expect(d.locator('#form-duration-menu')).not.toHaveAttribute('open');await d.getByRole('button',{name:'Отмена',exact:true}).click();await expect(add).toBeFocused();
});
test('server, backup, policy and admin forms fit narrow screen',async({page})=>{
 await page.setViewportSize({width:320,height:740});await page.goto('/#/');await page.getByRole('button',{name:'Добавить сервер',exact:true}).click();const d=page.locator('#modal');await d.locator('#server-auth-menu summary').click();await d.locator('[data-filter-option="key"]').click();await expect(d.locator('#server-key')).toBeVisible();await expect(d.locator('#server-password')).toBeHidden();await fit(page);await d.getByRole('button',{name:'Отмена',exact:true}).click();
 for(const [path,name] of [['settings','Резервные копии'],['client/1/9','Изменить ограничения'],['admins','Добавить администратора']]){await page.goto('/#/'+path);await page.getByRole('button',{name,exact:true}).click();await expect(d).toBeVisible();await expect(d.locator('select:visible')).toHaveCount(0);await fit(page);await d.getByRole('button',{name:'Отмена',exact:true}).click()}
});
test('daily error retry and return preserve report context',async({page})=>{
 await page.route('**/daily?month=*',r=>r.fulfill({status:503,json:{error:'Тест: нет связи'}}));await page.goto('/#/client/1/9');await expect(page.locator('.report-error')).toContainText('Тест: нет связи');await expect(page.locator('.daily-loading')).toHaveCount(0);await page.unroute('**/daily?month=*');await page.getByRole('button',{name:'Повторить загрузку'}).click();await expect(page.locator('.daily-line-chart')).toBeVisible();
 await page.goto('/#/stats/1');await page.locator('[data-sort=tx]').click();await expect(page.locator('[data-sort=tx]')).toHaveAttribute('aria-pressed','true');const month=await page.locator('#stats-month').inputValue();await page.locator('.report-link').first().click();await page.getByRole('button',{name:'Предыдущий месяц',exact:true}).click();await page.locator('.back-link').click();await expect(page.locator('#stats-month')).toHaveValue(month);await expect(page.locator('[data-sort=tx]')).toHaveAttribute('aria-pressed','true');
});
test('monthly retry and invalid route return',async({page})=>{
 await page.route('**/monthly?**',r=>r.fulfill({status:503,json:{error:'Нет отчёта'}}));await page.goto('/#/stats');await expect(page.locator('.report-error')).toBeVisible();await page.unroute('**/monthly?**');await page.getByRole('button',{name:'Повторить загрузку'}).click();await expect(page.locator('.report-link').first()).toBeVisible();await page.goto('/#/not-a-page');await expect(page.getByRole('heading',{name:'Страница не найдена'})).toBeVisible();await page.getByRole('link',{name:'В обзор',exact:true}).click();await expect(page.locator('.server-card').first()).toBeVisible();
});
test('manual update check gives busy, success and failure',async({page})=>{
 await page.goto('/#/settings');const check=page.locator('[data-action=update-check]');await expect(check).toBeEnabled();let release;const gate=new Promise(r=>release=r);await page.route('**/updates/check',async r=>{await gate;await r.fulfill({json:{checked:Date.now()/1000,release:null,enabled:true}})});await check.click();await expect(check).toBeDisabled();await expect(check.locator('.speed-spinner')).toBeVisible();release();await expect(page.locator('.update-section [role=status]').first()).toContainText('Обновлений нет');await page.unroute('**/updates/check');await page.route('**/updates/check',r=>r.fulfill({status:503,json:{error:'GitHub недоступен'}}));await check.click();await expect(page.locator('.update-section [role=status]').first()).toContainText('GitHub недоступен');await expect(check).toBeEnabled();
});
test('dropdown has no persistent fill and stays open during polling',async({page})=>{
 await page.goto('/#/clients');const menu=page.locator('#client-server-menu');await menu.locator('summary').click();await page.mouse.move(0,0);for(const b of await menu.locator('button').all())await expect(b).toHaveCSS('background-color','rgba(0, 0, 0, 0)');await menu.locator('[data-filter-option="2"]').hover();await expect(menu.locator('[data-filter-option="all"]')).toHaveCSS('background-color','rgba(0, 0, 0, 0)');await page.waitForTimeout(5200);await expect(menu).toHaveAttribute('open');
});
test('visual record in light and dark',async({page})=>{
 for(const mode of ['light','dark']){await page.goto('about:blank');await page.route('**/api/preferences',r=>r.fulfill({json:{mode,fontSize:14,motion:false}}));for(const [name,path] of [['overview','/'],['server','/server/1'],['clients','/clients'],['stats','/stats'],['settings','/settings']]){await page.goto('/#'+path);await expect(page.locator('#app h1')).toBeVisible();await expect(page.locator('html')).toHaveAttribute('data-theme',mode);if(name==='server')await expect(page.locator('.daily-line-chart')).toBeVisible();if(name==='stats')await expect(page.locator('.report-link').first()).toBeVisible();await page.screenshot({path:`../../audit/2026-11-04/${name}-${mode}.png`,fullPage:true})}await page.goto('/#/clients');await page.getByRole('button',{name:'Добавить клиента',exact:true}).click();await page.locator('#form-duration-menu summary').click();await page.screenshot({path:`../../audit/2026-11-04/form-${mode}.png`});await page.unroute('**/api/preferences')}
});

test('every rendered internal link resolves, assets load, modal closes on route change',async({page})=>{
 const links=new Set(),broken=[];page.on('response',r=>{if(r.url().includes('/assets/')&&r.status()>=400)broken.push(r.url())});
 for(const path of routes){await page.goto('/#'+path);await expect(page.locator('#app h1')).toBeVisible();if(path==='/stats')await expect(page.locator('.report-link').first()).toBeVisible();for(const href of await page.locator('a[href^="#/"]').evaluateAll(as=>as.map(a=>a.getAttribute('href'))))links.add(href)}
 for(const href of links){await page.goto('/'+href);await expect(page.locator('#app h1')).toBeVisible();await expect(page.locator('#app')).not.toContainText('Конфиг не найден');await expect(page.locator('#app')).not.toContainText('Сервер не найден');await expect(page.locator('#app')).not.toContainText('Страница не найдена')}
 expect(links.size).toBeGreaterThan(20);expect(broken).toEqual([]);
 await page.goto('/#/clients');await page.getByRole('button',{name:'Добавить клиента',exact:true}).click();await page.evaluate(()=>location.hash='/stats');await expect(page.locator('#modal')).not.toBeVisible();await expect(page.locator('#app')).toBeFocused();
});
test('compact layout and administrator screens are usable',async({page})=>{
 await page.setViewportSize({width:320,height:740});await page.route('**/api/preferences',r=>r.fulfill({json:{mode:'dark',fontSize:16,density:'compact',motion:false}}));
 await page.route('**/api/overview',async r=>{const response=await r.fetch();const data=await response.json();data.access={owner:false};await r.fulfill({json:data})});
 for(const path of ['/','/server/1','/clients','/client/1/9','/stats','/settings']){await page.goto('/#'+path);await expect(page.locator('#app h1')).toBeVisible();await fit(page)}
 await expect(page.locator('[data-action=backup],[data-action=add-server],[data-action=update-open]')).toHaveCount(0);await page.goto('/#/admins');await expect(page.locator('#app')).toContainText('Доступ только владельцу');
});
test('failed notification preference restores the saved value',async({page})=>{
 await page.goto('/#/settings');const toggle=page.locator('#update-notifications');await expect(toggle).toBeChecked();await page.route('**/updates/preferences',r=>r.fulfill({status:503,json:{error:'Не сохранено'}}));await toggle.uncheck();await expect(toggle).toBeChecked();await expect(toggle).toBeEnabled();await expect(page.locator('#toast')).toHaveText('Не сохранено');
});
