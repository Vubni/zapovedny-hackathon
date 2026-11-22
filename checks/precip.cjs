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
    const slots = page.locator('#precip-map-points button');
    const shadedCells = page.locator('#precip-map-shading rect');
    const hour = page.locator('#precip-hour');
    const interval = page.locator('#precip-period');
    const waitForForecast = () => page.waitForFunction(() => !document.querySelector('#precip-refresh').disabled);

    if (process.env.METEO_LIVE_PRECIP === '1') {
      await page.goto(base + '/precip');
      await waitForForecast();
      const message = await status.textContent();
      assert.match(message, /^Прогноз получен/);
      assert.equal(await slots.count(), 25);
      assert.ok(await page.locator('#precip-map-places text').count() > 0);
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
      assert.equal(await shadedCells.count(), 25);
      assert.deepEqual(await slots.evaluateAll(buttons => buttons.map(button => button.title)), saved.response.map((cell, index) => {
        const value = cell.hourly.precipitation[0];
        return (scenario === 'missing' && index === 0) || value === null ? 'Нет данных' : number.format(value) + ' мм';
      }));
      assert.ok(await page.locator('#precip-map-places text').count() > 4);
      assert.match(await page.locator('#precip-map-boundary').getAttribute('d'), /^M.*Z/);
      assert.equal(await page.locator('#precip-map-marker').count(), 1);
      const [markerX, markerY] = await page.locator('#precip-map-marker').evaluate(marker => {
        const cell = document.querySelectorAll('#precip-map-shading rect')[12];
        return [
          (marker.cx.baseVal.value - cell.x.baseVal.value) / cell.width.baseVal.value,
          (marker.cy.baseVal.value - cell.y.baseVal.value) / cell.height.baseVal.value,
        ];
      });
      assert.ok(Math.abs(markerX - 0.46) < 0.001 && Math.abs(markerY - 0.18) < 0.001);
      assert.equal(await page.locator('#precip-prev').isDisabled(), true);
      assert.equal(await interval.textContent(), intervalText(times[0]));

      if (scenario === 'missing') {
        assert.equal(await slots.first().getAttribute('data-level'), 'missing');
        assert.equal(await shadedCells.first().evaluate(cell => getComputedStyle(cell).fill), 'url("#precip-nodata")');
        assert.ok(await page.locator('#precip-map-points button[data-level="0"]').count() > 0);
        await slots.first().click();
        assert.equal(await page.locator('#precip-cell').textContent(), 'Нет данных');
      }

      await page.locator('#precip-next').click();
      assert.equal(await interval.textContent(), intervalText(times[1]));
      assert.deepEqual(await shadedCells.evaluateAll(cells => cells.map(cell => cell.dataset.level)),
        saved.response.map(cell => {
          const mm = cell.hourly.precipitation[1];
          if (mm === null) return 'missing';
          if (mm === 0) return '0';
          return mm < 0.2 ? '1' : mm < 1 ? '2' : mm < 3 ? '3' : '4';
        }));
      await slots.last().click();
      const {latitude, longitude} = saved.response.at(-1);
      assert.equal(await page.locator('#precip-node').textContent(),
        `Узел модели ${number.format(latitude)}° с. ш., ${number.format(longitude)}° в. д.`);
      const overview = Number(await shadedCells.last().getAttribute('width'));
      await page.getByRole('button', {name: 'Увеличить карту'}).click();
      assert.ok(Number(await shadedCells.last().getAttribute('width')) > overview);
      await slots.last().focus();
      await slots.last().press('ArrowLeft');
      assert.equal(await slots.nth(23).getAttribute('aria-pressed'), 'true');
      assert.equal(await slots.nth(23).evaluate(node => node === document.activeElement), true);
      await page.getByRole('button', {name: 'Вся область'}).click();
      assert.equal(Number(await shadedCells.last().getAttribute('width')), overview);
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
      await page.waitForFunction(() =>
        Number(document.querySelector('#precip-map-drawing').getAttribute('viewBox').split(' ')[2]) ===
        document.querySelector('#precip-map').clientWidth);
      assert.equal(await slots.filter({visible: true}).count(), 25);
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
