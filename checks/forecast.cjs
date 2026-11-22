// Optional browser checks. Run with the same Chromium setup as checks/browser.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const base = 'http://127.0.0.1:' + (process.env.METEO_TEST_PORT || '8765');
const api = 'https://api.open-meteo.com/**';

async function check() {
  const saved = JSON.parse(await fs.readFile(path.join(root, 'web/forecast-example.json'), 'utf8'));
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const evidence = {};
    if (process.env.METEO_LIVE_FORECAST === '1') {
      await page.goto(base + '/forecast');
      await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
      assert.match(await page.locator('#forecast-status').textContent(), /Получен актуальный/);
      assert.equal(await page.locator('#forecast-rows tr').count(), 3);
      evidence.live = {
        verifiedAt: new Date().toISOString(),
        columns: await page.locator('#forecast-data thead th').allTextContents(),
        rows: await page.locator('#forecast-rows tr').evaluateAll(rows =>
          rows.map(row => Array.from(row.cells, cell => cell.textContent))),
      };
    }
    await page.clock.setFixedTime(new Date(saved.retrieved_utc));
    for (const scenario of ['fresh', 'offline', 'stale', 'malformed']) {
      await page.route(api, route => {
        if (scenario === 'offline') return route.abort();
        const data = structuredClone(saved.response);
        if (scenario === 'stale') data.daily.time = ['2010-01-01', '2010-01-02', '2010-01-03'];
        if (scenario === 'malformed') data.daily.temperature_2m_min.pop();
        return route.fulfill({json: data});
      });
      await page.goto(base + '/forecast');
      await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
      const status = await page.locator('#forecast-status').textContent();
      assert.match(status, scenario === 'fresh' ? /Получен актуальный/ : /сохранённая копия/);
      assert.equal(await page.locator('#forecast-rows tr').count(), 3);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      evidence[scenario] = status;
      await page.unroute(api);
    }
    assert.deepEqual(errors, []);
    const report = path.join(root, 'results/browser/forecast.json');
    await fs.writeFile(report, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Проверен прогноз, переход на датированную копию и мобильная таблица.');
  } finally {
    await browser.close();
  }
}
check().catch(error => { console.error(error); process.exitCode = 1; });
