// Scope checks for the forecast and precipitation UI using a fixed, local API scenario.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const base = 'http://127.0.0.1:' + (process.env.METEO_TEST_PORT || '8765');
const web = path.join(__dirname, '..', 'web');
const central = {id: 'central-forest', name: 'Центрально-Лесной заповедник'};
const eastern = {id: 'kamchatka', name: 'Камчатский заповедник'};
const station = {id: 'field', label: 'Восточный пост', latitude: 54.1, longitude: 158.75, location_precision: 'exact'};

async function check() {
  const snapshots = {
    forecast: JSON.parse(await fs.readFile(path.join(web, 'forecast-example.json'), 'utf8')),
    precip: JSON.parse(await fs.readFile(path.join(web, 'precip-example.json'), 'utf8')),
  };
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    for (const pageName of ['forecast', 'precip']) {
      const page = await browser.newPage({viewport: {width: 1120, height: 950}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const snapshot = snapshots[pageName];
      await page.clock.setFixedTime(new Date(snapshot.retrieved_utc));
      const panel = page.locator('#' + pageName + '-data');
      const status = page.locator('#' + pageName + '-status');
      const refresh = page.locator(pageName === 'forecast' ? '#refresh' : '#precip-refresh');
      const finished = () => page.waitForFunction(selector => !document.querySelector(selector).disabled,
        pageName === 'forecast' ? '#refresh' : '#precip-refresh');
      const centralLink = page.locator(`main a[href="/${pageName}-example.json"]`);
      let center = eastern;
      let stations = [{id: 'no-location', label: 'Пост без координат'}];
      let mode = 'online';
      let accountStatus = 200;
      let scopedRequests = 0;
      let centralCopies = 0;
      const weatherRequests = [];
      await page.route('**/api/account/me', route => route.fulfill({
        status: accountStatus,
        json: {user: center === central ? null : {id: 'admin', role: 'global_admin'}, center, centers: [central, eastern]},
      }));
      await page.route('**/api/stations', route => {
        scopedRequests++;
        return route.fulfill({json: {stations}});
      });
      await page.route(`**/${pageName}-example.json`, route => {
        centralCopies++;
        return route.fulfill({json: snapshot});
      });
      await page.route('https://api.open-meteo.com/**', route => {
        const query = new URL(route.request().url()).searchParams;
        weatherRequests.push(query);
        if (mode === 'offline') return route.abort();
        if (mode === 'wrong-location' || center === central) return route.fulfill({json: snapshot.response});
        if (pageName === 'forecast') {
          return route.fulfill({json: {...snapshot.response, latitude: station.latitude, longitude: station.longitude}});
        }
        const latitudes = query.get('latitude').split(',').map(Number);
        const longitudes = query.get('longitude').split(',').map(Number);
        return route.fulfill({json: snapshot.response.map((node, index) => ({
          ...node, latitude: latitudes[index], longitude: longitudes[index],
        }))});
      });

      await page.goto(base + '/' + pageName);
      await finished();
      assert.match(await status.textContent(), /Добавьте метеостанцию с координатами/);
      assert.match(await page.locator('main .sub').textContent(), /Камчатский заповедник/);
      assert.equal(await panel.isHidden(), true);
      assert.equal(await centralLink.isHidden(), true);
      assert.ok(scopedRequests > 0);
      assert.equal(weatherRequests.length, 0);
      assert.equal(centralCopies, 0);

      stations = [
        {id: 'archive', label: 'Урочище «Красное»', latitude: 56.4, longitude: 32.9, archive: true},
        {id: 'area', label: 'Район', latitude: 54, longitude: 158.7, location_precision: 'area'},
        station,
        {id: 'other', label: 'Второй пост', latitude: 53, longitude: 159, location_precision: 'exact'},
      ];
      await refresh.click();
      await finished();
      assert.equal(await panel.isVisible(), true);
      assert.equal(await centralLink.isHidden(), true);
      assert.equal(centralCopies, 0);
      assert.equal(weatherRequests.length, 1);
      const query = weatherRequests.at(-1);
      if (pageName === 'forecast') {
        assert.equal(query.get('latitude'), String(station.latitude));
        assert.equal(query.get('longitude'), String(station.longitude));
        assert.match(await page.locator('#forecast-place').textContent(), /Восточный пост.*54,1.*158,75/);
        assert.match(await page.locator('[data-forecast-place]').first().textContent(), /Восточный пост/);
      } else {
        assert.equal(query.get('models'), 'dwd_icon_global');
        const latitudes = query.get('latitude').split(',').map(Number);
        const longitudes = query.get('longitude').split(',').map(Number);
        assert.equal(latitudes.length, 25);
        assert.equal(longitudes.length, 25);
        assert.equal(latitudes[12], station.latitude);
        assert.equal(longitudes[12], station.longitude);
        assert.equal(await page.locator('#precip-map-relief').getAttribute('href'), '/russia-relief.webp');
        assert.equal(await page.locator('#precip-map-boundary').getAttribute('d'), '');
        assert.equal(await page.locator('#precip-boundary-key').isHidden(), true);
        assert.match(await page.locator('#precip-map-location').textContent(), /Восточный пост/);
        assert.match(await page.locator('#precip-forecast-point').textContent(), /54,1.*158,75/);
        assert.equal(await page.locator('#precip-map-points button[aria-pressed="true"]').count(), 1);
        assert.ok(await page.locator('#precip-map-land').getAttribute('d'));
      }

      for (const failure of ['offline', 'wrong-location']) {
        mode = failure;
        await page.goto(base + '/' + pageName);
        await finished();
        assert.match(await status.textContent(), /Не удалось получить прогноз/);
        assert.equal(await panel.isHidden(), true);
        assert.equal(await centralLink.isHidden(), true);
        assert.equal(centralCopies, 0);
      }

      center = central;
      mode = 'offline';
      await page.evaluate(() => window.dispatchEvent(new Event('meteoarchive:account-change')));
      await finished();
      assert.match(await page.locator('main .sub').textContent(), /Центрально-Лесной заповедник/);
      assert.equal(centralCopies, 1);
      assert.equal(await panel.isVisible(), true);
      assert.equal(await centralLink.isVisible(), true);

      center = eastern;
      stations = [];
      await page.evaluate(() => window.dispatchEvent(new Event('meteoarchive:account-change')));
      await finished();
      assert.match(await status.textContent(), /Добавьте метеостанцию с координатами/);
      assert.equal(await panel.isHidden(), true);
      assert.equal(await centralLink.isHidden(), true);
      assert.equal(centralCopies, 1);

      accountStatus = 503;
      const before = weatherRequests.length;
      await refresh.click();
      await finished();
      assert.match(await status.textContent(), /Не удалось определить/);
      assert.equal(await panel.isHidden(), true);
      assert.equal(await centralLink.isHidden(), true);
      assert.equal(weatherRequests.length, before);
      assert.equal(centralCopies, 1);
      assert.deepEqual(errors, []);
      await page.close();
      console.log(pageName + ': первая станция с точными координатами, пустой центр, ошибки провайдера и смена центра.');
    }
  } finally {
    await browser.close();
  }
}

check().catch(error => { console.error(error); process.exitCode = 1; });
