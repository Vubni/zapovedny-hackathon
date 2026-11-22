const assert = require('node:assert/strict');
const { readFile } = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const webDirectory = path.join(__dirname, '..', 'web');
const origin = 'http://indicators.test';
const first = { start: '2024-01-02', end: '2024-01-09' };
const second = { start: '2025-02-05', end: '2025-02-13' };
const stations = [{ id: 'kr1', label: 'Кр1 · поляна' }, { id: 'kr2', label: 'Кр2 · поле' }];
const dates = range => `${range.start}:${range.end}`;
const card = (page, id) => page.locator(`[data-observation-indicator="${id}"]`);
const choice = (page, id) => page.locator(`label:has([data-toggle-indicator="${id}"])`);
const checkbox = (page, id) => page.locator(`[data-toggle-indicator="${id}"]`);

function sampleResponse(chart, ranges) {
  const parameter = chart === 'soil_05' ? 'soil_05' : 't_air';
  const statistics = ranges.flatMap((range, index) => stations.map(({ id, label }) => {
    const values = id === 'kr2' && index === 0
      ? { mean: null, min: null, max: null }
      : { mean: index ? 12.7 : 0, min: index ? -5 : -31.1, max: index ? 25 : 24 };
    return { station: id, label, parameter, unit: '°C',
      ...(chart === 'soil_05' ? { mean: null } : values),
      interval_start: range.start, interval_end: range.end };
  }));
  return { statistics, series: [{ station: 'kr1', points: [{ y: 999 }] }] };
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1240, height: 900 } });
    const paths = ['archive.css', 'observation-indicators.css', 'observation-indicators.js'];
    const files = new Map(await Promise.all(paths.map(async name => [`/${name}`, await readFile(path.join(webDirectory, name))])));
    const pending = [];
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(`${origin}/**`, async route => {
      const url = new URL(route.request().url());
      if (files.has(url.pathname)) {
        const contentType = url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css';
        return route.fulfill({ contentType, body: files.get(url.pathname) });
      }
      if (url.pathname === '/api/observations') {
        pending.push(url.searchParams);
        const ranges = url.searchParams.has('intervals')
          ? url.searchParams.get('intervals').split(',').map(value => {
            const [start, end] = value.split(':');
            return { start, end };
          })
          : [{ start: url.searchParams.get('start'), end: url.searchParams.get('end') }];
        return route.fulfill({ json: sampleResponse(url.searchParams.get('chart'), ranges) });
      }
      if (url.pathname !== '/') return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="ru">
        <meta charset="utf-8"><link rel="stylesheet" href="/archive.css">
        <link rel="stylesheet" href="/observation-indicators.css">
        <body style="margin:20px"><div id="catalog"></div><div id="board"></div>
        <script type="module">
          import { mountObservationIndicators } from '/observation-indicators.js';
          const catalog = document.querySelector('#catalog');
          const container = document.querySelector('#board');
          window.indicatorPanel = mountObservationIndicators({ catalog, container, onChange(count) { window.lastCount = count; } });
        </script></body></html>` });
    });

    await page.goto(origin);
    const catalog = page.locator('#catalog summary');
    await catalog.click();
    assert.equal(pending.length, 0);
    assert(await page.locator('#board').isHidden());
    await choice(page, 'air_mean').click();
    await card(page, 'air_mean').getByText('Выберите хотя бы одну станцию').waitFor();
    assert.equal(pending.length, 0);

    await page.evaluate(({ stations, first }) => window.indicatorPanel.update({ stations, ...first }), { stations, first });
    await card(page, 'air_mean').getByText('Кр1 · поляна').waitFor();
    assert.equal(await card(page, 'air_mean').locator('.observation-indicator__value strong').textContent(), '0');
    assert.equal(await card(page, 'air_mean').getByText('Нет данных').count(), 1);
    assert.equal(pending[0].get('stations'), 'kr1,kr2');
    assert.equal(pending[0].get('start'), first.start);
    assert.equal(pending[0].get('end'), first.end);
    assert.equal(pending[0].get('intervals'), null);

    await choice(page, 'air_min').click();
    await choice(page, 'air_max').click();
    assert.equal(pending.length, 1);
    assert.equal(await card(page, 'air_min').locator('.observation-indicator__value strong').textContent(), '-31,1');
    assert.equal(await card(page, 'air_max').locator('.observation-indicator__value strong').textContent(), '24');

    await page.evaluate(({ stations, first, second }) => {
      window.indicatorPanel.update({ stations, start: first.start, end: second.end, intervals: [first, second] });
    }, { stations, first, second });
    const matrix = card(page, 'air_mean').getByRole('table');
    await matrix.waitFor();
    assert.equal(pending.length, 2);
    assert.equal(pending[1].get('intervals'), [dates(first), dates(second)].join(','));
    assert.equal(pending[1].get('grain'), 'year');
    assert.equal(await matrix.getByRole('columnheader').count(), 3);
    assert.equal(await matrix.getByRole('rowheader').count(), 2);
    assert((await matrix.textContent()).includes('02.01.2024 – 09.01.2024'));
    assert((await matrix.textContent()).includes('05.02.2025 – 13.02.2025'));
    assert.deepEqual(await matrix.locator('.observation-indicator__value strong').allTextContents(), ['0', '12,7', '12,7']);

    await choice(page, 'soil_05_mean').click();
    const soil = card(page, 'soil_05_mean');
    await soil.getByRole('table').waitFor();
    assert.equal(await soil.getByText('Нет данных', { exact: true }).count(), 4);
    assert.equal(pending.length, 3);
    await soil.getByRole('button', { name: /^Скрыть карточку/ }).click();
    assert(await soil.isHidden());
    assert.equal(await checkbox(page, 'soil_05_mean').isChecked(), false);
    assert.equal(await page.evaluate(() => window.lastCount), 3);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    assert(await matrix.evaluate(table => table.parentElement.scrollWidth === table.parentElement.clientWidth));
    await catalog.click();
    if (process.env.METEO_INDICATORS_SCREENSHOT) await page.screenshot({ path: process.env.METEO_INDICATORS_SCREENSHOT, fullPage: true });

    await page.reload();
    await catalog.click();
    assert(await checkbox(page, 'air_mean').isChecked());
    assert(await checkbox(page, 'air_min').isChecked());
    assert(await checkbox(page, 'air_max').isChecked());
    assert.equal(await checkbox(page, 'soil_05_mean').isChecked(), false);
    assert.equal(await page.evaluate(() => window.indicatorPanel.count), 3);
    await page.evaluate(() => window.indicatorPanel.destroy());
    assert.equal(await page.locator('#catalog details').count(), 0);
    assert(await page.locator('#board').isHidden());
    assert.deepEqual(errors, []);
    console.log('Числовые карточки: выбор, удаление, статистика, фильтры, сравнение периодов и сохранение выбора проверены.');
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
