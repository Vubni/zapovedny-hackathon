// Integration check of georeferencing and lazy, local loading at close zoom.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const {withIsolatedMeteo} = require('./isolated-meteo.cjs');

const output = path.join(__dirname, '..', 'results', 'browser', 'station-map-detail.json');

async function checkAlignment(page) {
  return page.evaluate(async () => {
    const {loadRaster, loadReserveObjects, reserveDetailRaster, reserveRaster} = await import('/station-map-raster.js');
    const [wide, close, objects] = await Promise.all([
      loadRaster(reserveRaster), loadRaster(reserveDetailRaster), loadReserveObjects(),
    ]);
    const [west, south, east, north] = reserveRaster.bounds;
    const [left, bottom, right, top] = reserveDetailRaster.bounds;
    const xScale = wide.width / (east - west);
    const yScale = wide.height / (north - south);
    const width = 100;
    const height = 70;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', {willReadFrequently: true});
    context.drawImage(wide, (left - west) * xScale, (north - top) * yScale,
      (right - left) * xScale, (top - bottom) * yScale, 0, 0, width, height);
    const coarse = context.getImageData(0, 0, width, height).data;
    context.clearRect(0, 0, width, height);
    context.drawImage(close, 0, 0, width, height);
    const detail = context.getImageData(0, 0, width, height).data;
    const samples = [];
    // Sample only the common opaque footprint.
    for (let i = 0; i < detail.length; i += 4) {
      if (detail[i + 3] === 255) samples.push([coarse[i + 1], detail[i + 1]]);
    }
    const means = [0, 1].map(channel => samples.reduce((sum, value) => sum + value[channel], 0) / samples.length);
    let cross = 0;
    let coarseSquares = 0;
    let detailSquares = 0;
    for (const [x, y] of samples) {
      const a = x - means[0];
      const b = y - means[1];
      cross += a * b;
      coarseSquares += a * a;
      detailSquares += b * b;
    }

    const aroundStations = [[32.970290, 56.464310], [32.970370, 56.463800]];
    return {
      bounds: reserveDetailRaster.bounds,
      size: [close.width, close.height],
      correlation: cross / Math.sqrt(coarseSquares * detailSquares),
      opaques: samples.length,
      coversStations: aroundStations.every(([lon, lat]) => lon > left && lon < right && lat > bottom && lat < top),
      roads: objects.roads.length,
      paths: objects.paths.length,
      buildings: objects.buildings.length,
      labels: objects.labels.map(feature => feature[0]),
      bounded: ['roads', 'paths', 'buildings', 'waterways'].every(layer => objects[layer].every(
        ([w, s, e, n]) => left <= w && bottom <= s && e <= right && n <= top)),
    };
  });
}

async function check(base) {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const evidence = [];
    for (const width of [1280, 390]) {
      const page = await browser.newPage({viewport: {width, height: 850}});
      const external = [];
      const details = new Set();
      const errors = [];
      await page.addInitScript(() => {
        window.detailDraws = [];
        const drawImage = CanvasRenderingContext2D.prototype.drawImage;
        CanvasRenderingContext2D.prototype.drawImage = function(image, ...args) {
          if (image.src?.endsWith('/reserve-detail.webp') && this.canvas.isConnected) {
            window.detailDraws.push({width: args[6], height: args[7],
              canvasWidth: this.canvas.clientWidth, canvasHeight: this.canvas.clientHeight});
          }
          return drawImage.call(this, image, ...args);
        };
      });
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) {
          external.push(url.origin);
          return route.abort();
        }
        if (url.pathname.startsWith('/reserve-detail')) details.add(url.pathname);
        return route.continue();
      });
      await page.goto(`${base}/observations`, {waitUntil: 'domcontentloaded'});
      const viewport = page.locator('#station-overview .station-map-viewport');
      await page.locator('#station-overview [data-map-raster="reserve"]').waitFor();
      assert.deepEqual([...details], []);
      const zoom = viewport.getByRole('button', {name: 'Приблизить'});
      for (let step = 0; step < 7 && !details.has('/reserve-detail.webp'); step++) {
        await zoom.click();
        await viewport.evaluate(() => new Promise(requestAnimationFrame));
      }
      await page.locator('#station-overview [data-map-detail="sentinel-2"][data-map-objects="osm"]').waitFor();
      const source = page.locator('#station-overview [data-reserve-objects-credit]');
      assert.match(await source.textContent(), /OpenStreetMap contributors/);
      assert.equal(await source.isVisible(), true);
      assert.deepEqual([...details].sort(), ['/reserve-detail-osm.js', '/reserve-detail.webp']);
      await zoom.click();
      await zoom.click();
      await viewport.evaluate(() => new Promise(requestAnimationFrame));
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await viewport.screenshot({path: `/tmp/meteo-station-detail-${width}.png`});
      }
      const alignment = await checkAlignment(page);
      assert.deepEqual(alignment.size, [1740, 1164]);
      assert.ok(alignment.correlation > .85);
      assert.ok(alignment.opaques > 5800 && alignment.coversStations);
      assert.equal(alignment.bounded, true);
      assert.ok(alignment.roads >= 10 && alignment.paths >= 30 && alignment.buildings >= 100);
      for (const name of ['Заповедный', 'Экотропа', 'Межа', 'Березники — Хмелевка']) {
        assert.ok(alignment.labels.includes(name));
      }
      await viewport.getByRole('button', {name: 'Детальный вид Красного'}).click();
      const scale = viewport.locator('.station-map-scale').filter({hasText: /^50 м$/});
      await scale.waitFor();
      assert.equal(await scale.isVisible(), true);
      await viewport.focus();
      // The center starts at the station reference. Move about 800 m south to the village buildings.
      for (let i = 0; i < 18; i++) await viewport.press('ArrowDown');
      await viewport.press('ArrowLeft');
      await viewport.evaluate(() => new Promise(requestAnimationFrame));
      if (process.env.METEO_MAP_SCREENSHOTS) {
        await viewport.screenshot({path: `/tmp/meteo-station-detail-buildings-${width}.png`});
      }
      const maximum = await viewport.locator('canvas').evaluate(canvas => {
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        let buildingPixels = 0;
        for (let i = 0; i < pixels.length; i += 4) {
          if (pixels[i] === 234 && pixels[i + 1] === 216 && pixels[i + 2] === 184) buildingPixels++;
        }
        return {
          canvas: [canvas.width, canvas.height],
          display: [canvas.clientWidth, canvas.clientHeight],
          croppedDraws: window.detailDraws.length,
          viewportBounded: window.detailDraws.every(draw =>
            draw.width > 0 && draw.height > 0 && draw.width <= draw.canvasWidth && draw.height <= draw.canvasHeight),
          buildingPixels,
        };
      });
      assert.deepEqual(maximum.canvas, maximum.display);
      assert.ok(maximum.croppedDraws > 0 && maximum.viewportBounded && maximum.buildingPixels > 0);
      assert.deepEqual(external, []);
      assert.deepEqual(errors, []);
      await viewport.getByRole('button', {name: 'Вся Россия'}).click();
      await page.locator('#station-overview [data-map-raster="russia"]').waitFor();
      assert.equal(await source.isHidden(), true);
      evidence.push({width, localRequests: [...details].sort(), externalRequests: external,
        ...alignment, maximum: {scale: '50 м', ...maximum}});
      await page.close();
    }
    await fs.writeFile(output, JSON.stringify(evidence, null, 2) + '\n');
    console.log('Проверены привязка снимка около 10 м, локальные объекты и подгрузка на 1280 и 390 px.');
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check).catch(error => { console.error(error); process.exitCode = 1; });
