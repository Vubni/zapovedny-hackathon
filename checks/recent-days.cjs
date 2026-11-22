// The five-day table follows real station/parameter observations, including an empty new station.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {chromium} = require('playwright');
const {withIsolatedMeteo, loginAccount} = require('./isolated-meteo.cjs');

async function verify(base, {globalAdmin}) {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}, acceptDownloads: true});
    await page.goto(base + '/?stations=ame60&start=2005-01-01&end=2005-01-31');
    const panel = page.locator('#recent-days');
    await panel.locator('summary').click();
    await panel.getByText('06.11.2021', {exact: true}).waitFor();
    assert.equal(await panel.locator('tbody tr').count(), 5);
    const processing = new URL(await panel.getByRole('link', {name: 'Обработать этот период'}).getAttribute('href'), base);
    assert.equal(processing.pathname, '/files');
    assert.deepEqual(Object.fromEntries(processing.searchParams), {
      source: 'observations', station: 'ame60', param: 't_air', start: '2021-11-02', end: '2021-11-06',
    });
    const [download] = await Promise.all([
      page.waitForEvent('download'), panel.getByRole('link', {name: 'Скачать CSV'}).click(),
    ]);
    assert.equal(await download.failure(), null);
    assert.match(await fs.readFile(await download.path(), 'utf8'), /2021-11-06/);
    await panel.getByLabel('Показатель').selectOption('wind_dir');
    await panel.getByText('01.11.2020', {exact: true}).waitFor();
    assert.equal(await panel.getByLabel('Станция').locator('option[value="fresh"]').count(), 0);
    await loginAccount(page.request, base, globalAdmin);
    await page.goto(base + '/?stations=ame60&start=2005-01-01&end=2005-01-31');
    await panel.locator('summary').click();
    await panel.getByLabel('Станция').locator('option[value="fresh"]').waitFor({state: 'attached'});
    await panel.getByLabel('Станция').selectOption('fresh');
    await panel.getByText('Измерений этого показателя пока нет.').waitFor();
    assert.equal(await panel.locator('tbody tr').count(), 0);
    assert.equal(await panel.getByRole('link', {name: 'Скачать CSV'}).count(), 0);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    console.log('Таблица пяти суток, CSV, смена показателя, новая станция и мобильная ширина прошли проверку.');
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(verify, {accounts: true, stations: [
  {id: 'fresh', label: 'Новый пост', latitude: 58, longitude: 33, params: ['rh']},
]})
  .catch(error => {console.error(error); process.exitCode = 1;});
