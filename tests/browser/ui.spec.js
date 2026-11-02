import {test,expect} from '@playwright/test';
test('DOM updates retain focus, input, ordering and SVG',async({page})=>{
  await page.goto('/_qa/dom');await expect(page.locator('body')).toHaveAttribute('data-status','passed');
});
test('client filters survive detail and return',async({page})=>{
  await page.goto('/#/clients');const search=page.getByRole('searchbox');await search.fill('Kolida');
  await page.getByRole('combobox',{name:'Фильтр по серверу'}).selectOption('1');
  await page.locator('.client-card').filter({hasText:'Kolida1'}).click();await page.locator('.back-link').click();
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
  await page.goto('/#/stats');await expect(page.locator('#stats-month')).toBeVisible();
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
