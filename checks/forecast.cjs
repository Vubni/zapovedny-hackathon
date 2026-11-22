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
  const forecastHour = new Intl.DateTimeFormat('sv-SE', {
    hour: '2-digit', hourCycle: 'h23', timeZone: 'Europe/Moscow',
  }).format(new Date(saved.retrieved_utc));
  const firstHour = saved.response.daily.time[0] + 'T' + forecastHour + ':00';
  const start = saved.response.hourly.time.indexOf(firstHour);
  assert.ok(start >= 0 && saved.response.hourly.time.length >= start + 48);
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const evidence = {};
    const waitForForecast = () => page.waitForFunction(() => !document.querySelector('#refresh').disabled);
    const hourlyRows = page.locator('#forecast-hourly-rows tr');
    if (process.env.METEO_LIVE_FORECAST === '1') {
      await page.goto(base + '/forecast');
      await waitForForecast();
      assert.match(await page.locator('#forecast-status').textContent(), /Получен актуальный/);
      assert.equal(await page.locator('#forecast-rows tr').count(), 3);
      assert.equal(await hourlyRows.count(), 24);
      evidence.live = {
        verifiedAt: new Date().toISOString(),
        columns: await page.getByRole('table', {name: 'Суточный прогноз', includeHidden: true}).locator('thead th').allTextContents(),
        rows: await page.locator('#forecast-rows tr').evaluateAll(rows =>
          rows.map(row => Array.from(row.cells, cell => cell.textContent))),
        hourlyPoints: await hourlyRows.count(),
      };
    }
    await page.clock.setFixedTime(new Date(saved.retrieved_utc));
    for (const scenario of ['fresh', 'offline', 'stale', 'malformed', 'daily-only']) {
      await page.route(api, route => {
        if (scenario === 'offline') return route.abort();
        const data = structuredClone(saved.response);
        if (scenario === 'stale') data.daily.time = ['2010-01-01', '2010-01-02', '2010-01-03'];
        if (scenario === 'malformed') data.daily.temperature_2m_min.pop();
        if (scenario === 'daily-only') {
          delete data.hourly;
          delete data.hourly_units;
          delete data.daily.weather_code;
        }
        return route.fulfill({json: data});
      });
      await page.goto(base + '/forecast');
      await waitForForecast();
      const status = await page.locator('#forecast-status').textContent();
      const isFresh = scenario === 'fresh' || scenario === 'daily-only';
      assert.match(status, isFresh ? /Получен актуальный/ : /сохранённая копия/);
      assert.equal(await page.locator('#forecast-rows tr').count(), 3);
      assert.equal(await page.locator('#forecast-days time').count(), 3);
      assert.equal(await page.locator('#forecast-source').textContent(), isFresh ? 'Open-Meteo' : 'Сохранённая копия');
      if (scenario === 'daily-only') {
        assert.equal(await page.locator('#forecast-hourly-empty').isVisible(), true);
        assert.equal(await page.locator('#forecast-hours').isHidden(), true);
      } else {
        assert.equal(await hourlyRows.count(), 24);
        assert.equal(await page.locator('#forecast-charts svg').count(), 2);
        const temperatureLegend = page.getByRole('button', {name: 'Температура', exact: true});
        await temperatureLegend.click();
        assert.equal(await page.locator('#forecast-temperature .forecast-series').isHidden(), true);
        await page.getByRole('button', {name: '48 часов'}).click();
        assert.equal(await page.locator('#forecast-temperature .forecast-series').isHidden(), true);
        await temperatureLegend.click();
        assert.equal(await page.locator('#forecast-temperature .forecast-series').isVisible(), true);
        assert.equal(await hourlyRows.count(), 48);
        assert.equal(await page.getByRole('button', {name: '48 часов'}).getAttribute('aria-pressed'), 'true');
        const expected = saved.response.hourly.temperature_2m.slice(start, start + 48);
        const format = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
        assert.deepEqual(await hourlyRows.locator('td:first-of-type').allTextContents(),
          expected.map(value => value === null ? '—' : format.format(value)));
        await page.getByRole('button', {name: '24 часа'}).click();
        assert.equal(await hourlyRows.count(), 24);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      evidence[scenario] = status;
      await page.unroute(api);
    }
    await page.route(api, route => route.abort());
    await page.locator('#refresh').click();
    await waitForForecast();
    assert.match(await page.locator('#forecast-status').textContent(), /сохранённая копия/);
    assert.equal(await hourlyRows.count(), 24);
    await page.locator('.forecast-details summary').click();
    assert.equal(await page.locator('#forecast-rows').isVisible(), true);
    for (const width of [320, 1280]) {
      await page.setViewportSize({width, height: 844});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `Горизонтальная прокрутка при ширине ${width} px`);
    }
    assert.equal(await page.locator('.weather-actions a[href="/model"]').count(), 1);
    assert.equal(await page.locator('.weather-actions a[href="/precip"]').count(), 1);
    assert.deepEqual(errors, []);
    const report = path.join(root, 'results/browser/forecast.json');
    await fs.writeFile(report, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Проверены суточные карточки, графики на 24 и 48 часов, повторы и датированная копия.');
  } finally {
    await browser.close();
  }
}
check().catch(error => { console.error(error); process.exitCode = 1; });
