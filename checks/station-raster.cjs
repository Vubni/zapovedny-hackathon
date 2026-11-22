// Integration check against an already-running local server.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const base = `http://127.0.0.1:${process.env.METEO_TEST_PORT || '8765'}`;
const output = path.join(__dirname, '..', 'results', 'browser', 'station-raster.json');

async function rasterMode(map, value) {
  await map.locator(`[data-map-raster="${value}"]`).waitFor();
}

async function smoke() {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const evidence = [];
    for (const width of [1280, 390]) {
      const page = await browser.newPage({viewport: {width, height: 850}});
      const external = [];
      const failures = [];
      const assets = new Map();
      page.on('pageerror', error => failures.push(error.message));
      page.on('response', response => {
        const url = new URL(response.url());
        if (url.pathname.endsWith('.webp')) {
          assets.set(url.pathname, {status: response.status(), mime: response.headers()['content-type']});
        }
      });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin === base) return route.continue();
        external.push(url.origin);
        return route.abort();
      });

      await page.goto(`${base}/observations`, {waitUntil: 'domcontentloaded'});
      const overview = page.locator('#station-overview');
      await rasterMode(overview, 'reserve');
      const marker = overview.locator('.station-map-marker-area');
      assert.equal(await marker.textContent(), '3');
      const source = overview.locator('[data-reserve-credit]');
      assert.match(await source.textContent(), /ESA WorldCover project 2021/);
      assert.ok(await source.isVisible());
      assert.equal(assets.has('/russia-relief.webp'), false);
      const [x, y, centerX, centerY] = await marker.evaluate(element => {
        const {clientWidth, clientHeight} = element.closest('.station-map-viewport');
        return [parseFloat(element.style.left), parseFloat(element.style.top), clientWidth / 2, clientHeight / 2];
      });
      assert.ok(Math.abs(x - centerX) <= .1 && Math.abs(y - centerY) <= .1);
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await overview.locator('.station-map-viewport').screenshot({path: `/tmp/meteo-station-reserve-${width}.png`});
      }

      await overview.getByRole('button', {name: 'Вся Россия'}).click();
      await rasterMode(overview, 'russia');
      assert.ok(await marker.isVisible());
      assert.ok(await source.isHidden());
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await overview.locator('.station-map-viewport').screenshot({path: `/tmp/meteo-station-russia-${width}.png`});
      }
      const dimensions = await overview.evaluate(element => ({scroll: element.scrollWidth, width: element.clientWidth}));
      assert.ok(dimensions.scroll <= dimensions.width + 1);
      const known = await page.request.get(`${base}/api/stations`).then(response => response.json());
      const archive = known.stations.filter(station => ['kr1', 'kr2', 'ame60'].includes(station.id));
      assert.equal(archive.length, 3);
      assert.ok(archive.every(station => station.latitude === 56.464310 && station.longitude === 32.970290));
      assert.deepEqual(external, []);
      assert.deepEqual(failures, []);
      assert.deepEqual(Object.fromEntries(assets), {
        '/reserve-imagery.webp': {status: 200, mime: 'image/webp'},
        '/russia-relief.webp': {status: 200, mime: 'image/webp'},
      });
      evidence.push({
        width, localRaster: assets.get('/reserve-imagery.webp'), nationalRaster: assets.get('/russia-relief.webp'),
        archiveReference: [archive[0].latitude, archive[0].longitude], markerCount: archive.length, outerRequests: external,
      });
      await page.close();
    }
    await fs.writeFile(output, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Подложки заповедника и страны работают на 1280 и 390 px без внешних запросов.');
  } finally {
    await browser.close();
  }
}

smoke().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
