// Integration check against an isolated archive and a private station.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const {withIsolatedMeteo} = require('./isolated-meteo.cjs');

const output = path.join(__dirname, '..', 'results', 'browser', 'station-raster.json');
const archiveIds = ['kr1', 'kr2', 'ame60'];
const stationMarker = (root, id) => root.locator(`.station-map-marker[data-station-id="${id}"]`);

async function markerGeometry(root) {
  const points = {};
  for (const id of archiveIds) {
    const marker = stationMarker(root, id);
    await marker.waitFor();
    points[id] = await marker.evaluate(element => {
      const {clientWidth, clientHeight} = element.closest('.station-map-viewport');
      const {x, y, width, height} = element.getBoundingClientRect();
      return {x, y, width, height, center: [clientWidth / 2, clientHeight / 2],
        anchor: [Number(element.dataset.anchorX), Number(element.dataset.anchorY)],
        displaced: element.hasAttribute('data-displaced')};
    });
  }
  for (const [index, id] of archiveIds.entries()) {
    const a = points[id];
    for (const other of archiveIds.slice(index + 1)) {
      const b = points[other];
      assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y);
    }
  }
  return points;
}

async function selectedFromMarkers(root) {
  return root.locator('.station-map-marker[aria-pressed="true"]').evaluateAll(elements =>
    elements.map(element => element.dataset.stationId).sort());
}

async function rasterMode(map, value) {
  await map.locator(`[data-map-raster="${value}"]`).waitFor();
}

async function smoke(base) {
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
      const reserve = await markerGeometry(overview);
      assert.equal(await overview.locator('.station-map-marker-area').count(), 3);
      const source = overview.locator('[data-reserve-credit]');
      assert.match(await source.textContent(), /ESA WorldCover project 2021/);
      assert.ok(await source.isVisible());
      assert.equal(assets.has('/russia-relief.webp'), false);
      const [x, y] = reserve.kr1.anchor;
      const [centerX, centerY] = reserve.kr1.center;
      assert.ok(Math.abs(x - centerX) <= .1 && Math.abs(y - centerY) <= .1);
      assert.deepEqual(reserve.kr1.anchor, reserve.ame60.anchor);
      assert.ok(reserve.kr2.anchor[1] > reserve.kr1.anchor[1]);
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await overview.locator('.station-map-viewport').screenshot({path: `/tmp/meteo-station-reserve-${width}.png`});
      }

      await overview.getByRole('button', {name: 'Вся Россия'}).click();
      await rasterMode(overview, 'russia');
      await markerGeometry(overview);
      assert.ok(await source.isHidden());
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await overview.locator('.station-map-viewport').screenshot({path: `/tmp/meteo-station-russia-${width}.png`});
      }
      const ame = stationMarker(overview, 'ame60');
      const beforeClick = await selectedFromMarkers(overview);
      await ame.click();
      assert.deepEqual(await selectedFromMarkers(overview), [...beforeClick, 'ame60'].sort());
      const {x: boxX, y: boxY, width: boxW, height: boxH} = await ame.boundingBox();
      await page.mouse.move(boxX - 2, boxY - 2);
      await page.mouse.down();
      await page.mouse.move(boxX + boxW + 2, boxY + boxH + 2, {steps: 4});
      await page.mouse.up();
      assert.deepEqual(await selectedFromMarkers(overview), ['ame60']);

      await overview.getByRole('button', {name: 'Детальный вид Красного'}).click();
      await overview.locator('[data-map-detail="sentinel-2"]').waitFor();
      const close = await markerGeometry(overview);
      assert.ok(close.kr2.anchor[1] - close.kr1.anchor[1] > 50);
      assert.equal(close.kr2.displaced, false);
      assert.deepEqual(close.kr1.anchor, close.ame60.anchor);
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await overview.locator('.station-map-viewport').screenshot({path: `/tmp/meteo-station-detail-${width}.png`});
      }
      const dimensions = await overview.evaluate(element => ({scroll: element.scrollWidth, width: element.clientWidth}));
      assert.ok(dimensions.scroll <= dimensions.width + 1);
      const known = await page.request.get(`${base}/api/stations`).then(response => response.json());
      const archive = known.stations;
      assert.deepEqual(archive.map(station => station.id).sort(), [...archiveIds].sort());
      assert.deepEqual(Object.fromEntries(archive.map(station =>
        [station.id, [station.latitude, station.longitude, station.location_precision]])), {
        kr1: [56.464310, 32.970290, 'area'], kr2: [56.463800, 32.970370, 'area'], ame60: [56.464310, 32.970290, 'area'],
      });
      assert.deepEqual(external, []);
      assert.deepEqual(failures, []);
      assert.deepEqual(Object.fromEntries(assets), {
        '/reserve-imagery.webp': {status: 200, mime: 'image/webp'},
        '/reserve-detail.webp': {status: 200, mime: 'image/webp'},
        '/russia-relief.webp': {status: 200, mime: 'image/webp'},
      });
      evidence.push({
        width, localRaster: assets.get('/reserve-imagery.webp'), nationalRaster: assets.get('/russia-relief.webp'),
        detailRaster: assets.get('/reserve-detail.webp'),
        archiveAnchors: Object.fromEntries(archive.map(station => [station.id, [station.latitude, station.longitude]])),
        markerCount: archive.length, individualFrameSelection: true, outerRequests: external,
      });
      await page.close();
    }
    await fs.writeFile(output, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Индивидуальные метки, якоря, рамка и подложки Красного и России проверены на 1280 и 390 px без внешних запросов.');
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(smoke, {stations: [
  {id: 'raster-private', label: 'Частная станция для проверки карты', latitude: 57, longitude: 36, params: ['t_air']},
]}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
