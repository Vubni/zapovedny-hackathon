// Optional browser checks. Set METEO_LIVE_PRECIP=1 to also call Open-Meteo.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const base = 'http://127.0.0.1:' + (process.env.METEO_TEST_PORT || '8765');
const api = 'https://api.open-meteo.com/**';
const viewportWidths = [1120, 390, 320];
const moscow = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
});
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 4});

function intervalText(time) {
  return moscow.formatRange((time - 3600) * 1000, time * 1000) + ' МСК';
}

async function check() {
  const saved = JSON.parse(await fs.readFile(path.join(root, 'web/precip-example.json'), 'utf8'));
  const {time: times} = saved.response[0].hourly;
  const received = new Date(saved.retrieved_utc);
  const firstForecastHour = times.findIndex(time => time * 1000 > received.getTime());
  assert.ok(firstForecastHour >= 0);
  const evidence = {
    snapshotRetrievedUtc: saved.retrieved_utc,
    cells: saved.response.length,
    hourlyIntervals: times.length,
    testedViewportWidths: viewportWidths,
    scenarios: {},
  };
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: viewportWidths[0], height: 950}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const status = page.locator('#precip-status');
    const refresh = page.locator('#precip-refresh');
    const slots = page.locator('#precip-rows button');
    const hour = page.locator('#precip-hour');
    const interval = page.locator('#precip-period');
    const waitForForecast = () => page.waitForFunction(() => !document.querySelector('#precip-refresh').disabled);

    if (process.env.METEO_LIVE_PRECIP === '1') {
      await page.goto(base + '/precip');
      await waitForForecast();
      const message = await status.textContent();
      assert.match(message, /^Прогноз получен/);
      assert.equal(await slots.count(), 25);
      evidence.live = {verifiedUtc: new Date().toISOString(), status: message};
    }

    await page.clock.setFixedTime(received);
    for (const scenario of ['offline', 'fresh', 'stale', 'missing']) {
      await page.route(api, route => {
        if (scenario === 'offline') return route.abort();
        const data = structuredClone(saved.response);
        if (scenario === 'missing') data[0].hourly.precipitation[0] = null;
        if (scenario === 'stale') {
          for (const node of data) node.hourly.time = node.hourly.time.map(time => time - 172800);
        }
        return route.fulfill({json: data});
      });

      await page.goto(base + '/precip');
      await waitForForecast();
      const message = await status.textContent();
      const live = scenario === 'fresh' || scenario === 'missing';
      assert.match(message, live ? /^Прогноз получен/ : /сохранённый прогноз/);
      assert.equal(await interval.textContent(), intervalText(times[live ? firstForecastHour : 0]));
      await hour.press('Home');

      assert.equal(await slots.count(), 25);
      assert.deepEqual(await slots.allTextContents(), saved.response.map((cell, index) => {
        const value = cell.hourly.precipitation[0];
        return (scenario === 'missing' && index === 0) || value === null ? '—' : number.format(value);
      }));
      assert.equal(await page.locator('.precip-grid .precip-marker').count(), 1);
      assert.equal(await page.locator('#precip-prev').isDisabled(), true);
      assert.equal(await interval.textContent(), intervalText(times[0]));

      if (scenario === 'missing') {
        assert.equal(await slots.first().getAttribute('data-level'), 'missing');
        assert.ok(await page.locator('#precip-rows button[data-level="0"]').count() > 0);
      }

      await page.locator('#precip-next').click();
      assert.equal(await interval.textContent(), intervalText(times[1]));
      await slots.last().click();
      const {latitude, longitude} = saved.response.at(-1);
      assert.equal(await page.locator('#precip-node').textContent(),
        `Узел модели ${number.format(latitude)}° с. ш., ${number.format(longitude)}° в. д.`);
      await hour.press('End');
      assert.equal(await page.locator('#precip-next').isDisabled(), true);
      assert.equal(await interval.textContent(), intervalText(times.at(-1)));
      evidence.scenarios[scenario] = message;
      await page.unroute(api);
    }

    await page.route(api, route => route.abort());
    await refresh.click();
    await waitForForecast();
    evidence.scenarios.failedRefresh = await status.textContent();
    assert.match(evidence.scenarios.failedRefresh, /ранее полученный прогноз/);
    for (const width of viewportWidths) {
      await page.setViewportSize({width, height: 844});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `Горизонтальная прокрутка при ширине ${width} px`);
    }
    assert.deepEqual(errors, []);

    const report = path.join(root, 'results/browser/precip.json');
    await fs.mkdir(path.dirname(report), {recursive: true});
    await fs.writeFile(report, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Проверены карта осадков, шкала времени, выбор узла, резервный прогноз и ширина экрана.');
  } finally {
    await browser.close();
  }
}

check().catch(error => { console.error(error); process.exitCode = 1; });
