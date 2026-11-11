import {test, expect} from '@playwright/test';

test('owner opens server and client traffic reports and switches the period', async ({page}) => {
  await page.setViewportSize({width: 320, height: 740});
  await page.goto('/#/server/1');
  const dialog = page.getByRole('dialog', {name: 'Трафик по сервисам'});
  const open = page.getByRole('button', {name: 'Трафик по сервисам', exact: true});
  await expect(page.getByRole('button', {name: 'Диагностика', exact: true})).toHaveCount(0);
  const comparison = page.getByRole('heading', {name: 'Сравнение трафика'}).locator('..');
  await expect(comparison).toHaveCSS('margin-block-start', '16px');
  await open.click();
  await expect(dialog.getByRole('heading', {name: 'Трафик по сервисам'})).toBeFocused();
  await expect(dialog.getByRole('button', {name: '24 часа'})).toHaveCSS('outline-style', 'none');
  await dialog.getByText('Видео и стримы', {exact: true}).click();
  await expect(dialog.getByText('YouTube', {exact: true})).toBeVisible();
  await expect(dialog.getByText('Без категории', {exact: true})).toBeVisible();
  const music = dialog.locator('details').filter({hasText:'Музыка и аудио'});
  await music.locator('summary').click();
  await expect(music.getByText('Spotify', {exact: true})).toBeVisible();
  await expect(music.getByText('Сервис не определён', {exact: true})).toBeVisible();
  const categoryTotal = await music.locator('summary .detail-row > b').last().boundingBox();
  const serviceTotal = await music.locator('li b').first().boundingBox();
  const chevron = await music.locator('.traffic-group-chevron svg').boundingBox();
  const scroll = await dialog.locator('.service-traffic-scroll').boundingBox();
  expect(serviceTotal.x + serviceTotal.width).toBeCloseTo(categoryTotal.x + categoryTotal.width);
  expect(scroll.x + scroll.width - chevron.x - chevron.width).toBeGreaterThanOrEqual(12);
  await expect(dialog.getByRole('button', {name: '24 часа'})).toHaveAttribute('aria-pressed', 'true');
  const report = dialog.locator('.service-traffic-report');
  const initial = await report.textContent();
  const initialBounds = await dialog.boundingBox();
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  await page.route('**/service-traffic?window=week', async route => {
    await gate;
    await route.continue();
  });
  const weekly = page.waitForResponse(response =>
    response.url().endsWith('/servers/1/service-traffic?window=week') && response.ok());
  await dialog.getByRole('button', {name: '7 дней'}).click();
  await expect(report).toContainText('Загружаю отчёт');
  expect(await dialog.boundingBox()).toEqual(initialBounds);
  release();
  await weekly;
  await expect(dialog.getByRole('button', {name: '7 дней'})).toHaveAttribute('aria-pressed', 'true');
  await expect(report).not.toHaveText(initial);
  await dialog.getByText('Видео и стримы', {exact: true}).click();
  await expect(dialog.getByText('YouTube', {exact: true})).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds).toEqual(initialBounds);
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(320);
  await dialog.getByRole('button', {name: 'Закрыть'}).click();
  await expect(open).toBeFocused();

  await page.goto('/#/client/1/9');
  const labels = await page.getByRole('button', {name: 'Имя и комментарий'}).boundingBox();
  const pause = await page.getByRole('button', {name: 'Доступ и ограничения'}).boundingBox();
  expect(pause.y - labels.y - labels.height).toBeGreaterThanOrEqual(8);
  const clientReport = page.waitForResponse(response =>
    response.url().endsWith('/servers/1/clients/9/service-traffic?window=day') && response.ok());
  await open.click();
  await clientReport;
  await dialog.getByText('Мессенджеры и звонки', {exact: true}).click();
  await expect(dialog.getByText('Telegram', {exact: true})).toBeVisible();
});

test('daily statistics screen offers the same service traffic card as the client screen', async ({page}) => {
  await page.setViewportSize({width: 320, height: 740});
  await page.goto('/#/daily/1/9');
  const open = page.getByRole('button', {name: 'Трафик по сервисам', exact: true});
  await expect(page.getByRole('heading', {name: 'Сравнение трафика'})).toBeVisible();
  await expect(open).toBeVisible();
  const report = page.waitForResponse(response =>
    response.url().endsWith('/servers/1/clients/9/service-traffic?window=day') && response.ok());
  await open.click();
  await report;
  const dialog = page.getByRole('dialog', {name: 'Трафик по сервисам'});
  await expect(dialog.getByText('Мессенджеры и звонки', {exact: true})).toBeVisible();
});

test('traffic settings toggle each installed collector independently', async ({page}) => {
  await page.setViewportSize({width: 320, height: 740});
  await page.goto('/#/settings');
  await page.getByRole('button', {name: 'Трафик по сервисам', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: 'Трафик по сервисам'});
  const installed = dialog.getByRole('switch').first();
  const missing = dialog.getByRole('switch').nth(1);
  await expect(installed).toBeChecked();
  await expect(missing).toBeEnabled();
  await expect(dialog.getByText('nDPI не установлен',{exact:true})).toBeVisible();
  await expect(dialog.getByText('nDPI установлен',{exact:true})).toBeVisible();
  await installed.click();
  await expect(dialog.getByText('nDPI остановлен',{exact:true})).toHaveAttribute('data-state','stopped');
  await expect(installed).not.toBeChecked();
  await installed.click();
  await expect(dialog.getByText('Сбор включён')).toBeVisible();
  await expect(installed).toBeChecked();

  const request = page.waitForRequest(req =>
    req.method() === 'POST' && req.url().endsWith('/servers/2/service-traffic/control'));
  await missing.click();
  expect((await request).postDataJSON()).toEqual({enable:true});
  await expect(dialog.getByText('nDPI установлен',{exact:true})).toHaveCount(2);
  await expect(missing).toBeEnabled();
  await expect(missing).toBeChecked();
  await expect(installed).toBeChecked();
});
