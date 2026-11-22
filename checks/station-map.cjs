// Optional Playwright check. Only local source files and a test catalog are used.
const assert = require('node:assert/strict');
const { readFile } = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const host = 'http://station-map.test';
const web = path.join(__dirname, '..', 'web');
const air = {id: 't_air', label: 'Температура воздуха', unit: '°C'};
const soil = {id: 'soil_10', label: 'Температура почвы · 10 см', unit: '°C'};
const pressure = {id: 'pressure_hpa', label: 'Атмосферное давление', unit: 'гПа'};
const paramOptions = [air, soil, pressure];
const catalog = [
  { id: 'kr1', label: 'Кр1 · поляна', color: '#2b7650', terrain: 'clearing', latitude: 56.464310, longitude: 32.970290, location_precision: 'area', location_label: 'Красное', location_reference: 'Точка сбора 26.07.2019', params: [air] },
  { id: 'kr2', label: 'Кр2 · поле', color: '#426583', terrain: 'field', location_precision: 'area' },
  { id: 'ame60', label: 'АМЕ-60', color: '#735579', terrain: 'unknown', location_precision: 'area' },
  { id: 'field-check', label: 'Контрольный пост', terrain: 'forest', latitude: 56.35, longitude: 33.13, location_precision: 'exact', params: [air] },
  { id: 'shore-east', label: 'Восточный берег', latitude: 60, longitude: 42, location_precision: 'exact' },
  { id: 'shore-west', label: 'Западный берег', latitude: 60, longitude: 42, location_precision: 'exact' },
  { id: 'unknown', label: 'Лесная', location_precision: 'unknown' },
];
const fixture = `<!doctype html><html lang="ru"><meta charset="utf-8">
  <link rel="stylesheet" href="/archive.css"><link rel="stylesheet" href="/station-map.css">
  <button class="btn" id="open">Выбрать станции</button><output id="dialog-result"></output>
  <section id="station-overview" aria-label="Карта метеостанций"></section><output id="overview-result"></output>
  <script type="module">
    import {mountStationOverview, pickStations} from '/station-map.js';
    const stations = ${JSON.stringify(catalog)};
    document.querySelector('#open').onclick = async () => {
      document.querySelector('#dialog-result').textContent = JSON.stringify(await pickStations(stations, ['kr1']));
    };
    window.mountTestOverview = () => {
      let selected = ['kr1'];
      function setSelected(ids) {
        selected = ids;
        document.querySelector('#overview-result').textContent = JSON.stringify(ids);
        overview.update(structuredClone(stations), ids);
      }
      const overview = mountStationOverview(document.querySelector('#station-overview'), {
        onChange: setSelected,
        async onExpand() {
          const next = await pickStations(stations, selected);
          if (next) setSelected(next);
        },
      });
      window.updateOverview = ids => { selected = ids; overview.update(stations, ids); };
      window.addOutsideStation = () => {
        stations.push({id: 'outside', label: 'Зарубежный пост', latitude: 59.91, longitude: 10.75,
          location_precision: 'exact', params: [${JSON.stringify(air)}]});
        overview.update(stations, selected);
      };
      window.destroyOverview = overview.destroy;
      overview.update(stations, selected);
    };
  </script></html>`;

async function markerBounds(locator) {
  const {x, y, width, height} = await locator.boundingBox();
  return {left: x + width / 2 - 28, right: x + width / 2 + 28, top: y + height / 2 - 28, bottom: y + height / 2 + 28};
}

async function beginDrag(page, bounds) {
  await page.mouse.move(bounds.left, bounds.top);
  await page.mouse.down();
  await page.mouse.move(bounds.right, bounds.bottom, {steps: 5});
}

async function boxSelect(page, bounds, modifier = null) {
  if (modifier) await page.keyboard.down(modifier);
  await beginDrag(page, bounds);
  await page.mouse.up();
  if (modifier) await page.keyboard.up(modifier);
}

async function touchDrag(touch, bounds) {
  await touch.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: bounds.left, y: bounds.top}]});
  await touch.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: bounds.right, y: bounds.bottom}]});
  await touch.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1280, height: 840 }, locale: 'ru-RU' });
  page.setDefaultTimeout(6000);
  const errors = [];
  const external = [];
  const posts = [];
  const requested = new Set();
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requested.add(url.pathname);
    if (url.origin !== host) {
      external.push(url.href);
      return route.abort();
    }
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: fixture });
    if (url.pathname === '/api/stations') {
      if (request.method() === 'GET') return route.fulfill({json: {stations: catalog, param_options: paramOptions}});
      const body = request.postDataJSON();
      posts.push(body);
      if (posts.length === 1) return route.fulfill({ status: 400, json: { error: 'Станция уже есть.' } });
      if (posts.length === 2) return route.fulfill({ status: 503, json: { error: 'Каталог временно недоступен.' } });
      const params = body.params.map(id => paramOptions.find(param => param.id === id));
      return route.fulfill({ status: 201, json: { station: { ...body, id: 'testing-ame', params, location_precision: 'exact' } } });
    }
    const ext = path.extname(url.pathname);
    const type = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.webp': 'image/webp' }[ext];
    if (!type) return route.fulfill({ status: 404 });
    return route.fulfill({ contentType: type, body: await readFile(path.join(web, url.pathname)) });
  });

  try {
    await page.goto(host);
    const open = page.locator('#open');
    const picker = page.locator('dialog.station-picker');
    const result = expected => page.waitForFunction(value =>
      document.querySelector('output').textContent === JSON.stringify(value), expected);
    const area = picker.locator('.station-map-marker-area');
    const map = picker.locator('.station-map-viewport');
    const frame = picker.locator('.station-map-frame');
    const selectMode = picker.getByRole('button', {name: 'Выделение рамкой'});
    const panMode = picker.getByRole('button', {name: 'Перемещение'});
    const localMarker = picker.getByRole('button', {name: 'Контрольный пост', exact: true});
    const checked = picker.locator('[data-list] input:checked');
    await open.click();
    await area.waitFor();
    await picker.locator('[data-map-raster="reserve"]').waitFor();
    assert.equal(requested.has('/reserve-imagery.webp'), true);
    assert.equal(requested.has('/russia-relief.webp'), false);
    assert.equal(await picker.locator('[data-reserve-credit]').isVisible(), true);
    assert.equal(await area.count(), 1);
    assert.equal(await area.textContent(), '3');
    assert.equal(await area.getAttribute('aria-pressed'), 'mixed');
    assert.match(await area.evaluate(el => getComputedStyle(el).backgroundImage), /conic-gradient/);
    const legend = picker.locator('[data-terrain-legend]');
    assert.deepEqual(await legend.locator('li').allTextContents(), ['Поляна', 'Поле', 'Лес', 'Тип местности не указан']);
    const krPassport = picker.getByRole('button', {name: /Паспорт станции Кр1/});
    await krPassport.click();
    const krDetails = picker.locator(`#${await krPassport.getAttribute('aria-controls')}`);
    assert.match(await krDetails.textContent(), /Поляна/);
    assert.match(await krDetails.textContent(), /Район заповедника/);
    assert.match(await krDetails.textContent(), /Красное.*Точка сбора 26\.07\.2019/);
    assert.doesNotMatch(await krDetails.textContent(), /56,46431°/);
    assert.match(await krDetails.textContent(), /Температура воздуха · °C/);
    await krPassport.click();
    const exactPassport = picker.getByRole('button', {name: /Паспорт станции Контрольный пост/});
    await exactPassport.click();
    const exactDetails = picker.locator(`#${await exactPassport.getAttribute('aria-controls')}`);
    assert.match(await exactDetails.textContent(), /56,35° с\. ш\., 33,13° в\. д\./);
    await exactPassport.click();
    assert.ok(await picker.locator('canvas').evaluate(canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      const colors = new Set();
      for (let p = 0; p < pixels.length; p += 4 * 21) colors.add(pixels[p] + ',' + pixels[p + 1] + ',' + pixels[p + 2]);
      return colors.size > 150;
    }), 'Подложка должна отображаться вместе с географическими деталями');
    if (process.env.METEO_MAP_SCREENSHOTS) await page.screenshot({ path: '/tmp/station-map-desktop.png' });

    assert.equal(await selectMode.getAttribute('aria-pressed'), 'true');
    const localBounds = await markerBounds(localMarker);
    const areaBounds = await markerBounds(area);
    await boxSelect(page, localBounds);
    assert.equal(await area.getAttribute('aria-pressed'), 'false');
    assert.equal(await localMarker.getAttribute('aria-pressed'), 'true');
    assert.equal(await localMarker.evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(37, 98, 72)');
    assert.equal(await checked.count(), 1);
    await boxSelect(page, areaBounds, 'Control');
    assert.equal(await area.getAttribute('aria-pressed'), 'true');
    assert.equal(await localMarker.getAttribute('aria-pressed'), 'true');
    assert.equal(await checked.count(), 4);
    await boxSelect(page, areaBounds);
    assert.equal(await checked.count(), 3);
    await boxSelect(page, localBounds, 'Meta');
    assert.equal(await checked.count(), 4);
    await boxSelect(page, {
      left: Math.min(areaBounds.left, localBounds.left), top: Math.min(areaBounds.top, localBounds.top),
      right: Math.max(areaBounds.right, localBounds.right), bottom: Math.max(areaBounds.bottom, localBounds.bottom),
    });
    assert.equal(await checked.count(), 4);
    await beginDrag(page, {...areaBounds, left: areaBounds.left + 34});
    await frame.waitFor();
    await page.keyboard.press('Escape');
    await frame.waitFor({state: 'hidden'});
    await page.mouse.up();
    assert.equal(await checked.count(), 4);
    assert.equal(await picker.isVisible(), true);
    await boxSelect(page, {...areaBounds, left: areaBounds.left + 34});
    assert.equal(await checked.count(), 0);
    assert.equal(await picker.locator('[data-apply]').isDisabled(), true);
    await picker.getByRole('checkbox', {name: /Кр1/}).check();

    const initialScale = await picker.locator('.station-map-scale').textContent();
    await picker.getByRole('button', { name: 'Приблизить' }).click();
    await page.waitForFunction(initial => document.querySelector('.station-map-scale').textContent !== initial, initialScale);
    const initialX = await area.evaluate(element => element.style.left);
    await map.press('ArrowRight');
    await page.waitForFunction(initial => document.querySelector('.station-map-marker-area').style.left !== initial, initialX);
    const canvas = await picker.locator('canvas').boundingBox();
    const beforePan = await area.evaluate(element => element.style.left);
    await panMode.click();
    assert.equal(await panMode.getAttribute('aria-pressed'), 'true');
    await boxSelect(page, {left: canvas.x + 95, top: canvas.y + 80, right: canvas.x + 170, bottom: canvas.y + 80});
    await page.waitForFunction(initial => document.querySelector('.station-map-marker-area').style.left !== initial, beforePan);
    assert.equal(await checked.count(), 1);
    assert.equal(await frame.isHidden(), true);
    await selectMode.click();
    await area.click();
    assert.equal(await area.getAttribute('aria-pressed'), 'true');
    assert.equal(await picker.getByRole('checkbox', { name: /Кр2/ }).isChecked(), true);
    await picker.getByRole('checkbox', { name: /АМЕ/ }).uncheck();
    assert.equal(await area.getAttribute('aria-pressed'), 'mixed');

    const offshore = picker.getByRole('button', { name: /Восточный берег, Западный берег/ });
    assert.equal(await offshore.count(), 0);
    await picker.getByRole('button', { name: 'К станциям' }).click();
    await offshore.waitFor();
    assert.equal(await area.isVisible(), true);
    await picker.getByRole('button', { name: /Показать на карте: Восточный берег/ }).click();
    await offshore.waitFor();
    assert.equal(await offshore.textContent(), '2');
    await offshore.click();
    await picker.getByRole('checkbox', { name: /Западный берег/ }).uncheck();
    assert.equal(await offshore.getAttribute('aria-pressed'), 'mixed');
    assert.equal(await picker.getByRole('checkbox', { name: /Восточный берег/ }).isChecked(), true);
    assert.equal(await picker.getByRole('button', { name: /Показать на карте: Лесная/ }).count(), 0);
    await picker.locator('[data-apply]').click();
    await picker.waitFor({ state: 'hidden' });
    await result(['kr1', 'kr2', 'shore-east']);

    await open.click();
    await picker.waitFor();
    await page.keyboard.press('Escape');
    await picker.waitFor({ state: 'hidden' });
    await result(null);
    assert.equal(await open.evaluate(el => document.activeElement === el), true);

    await page.setViewportSize({ width: 390, height: 780 });
    await open.click();
    await area.waitFor();
    const touch = await page.context().newCDPSession(page);
    await touch.send('Emulation.setTouchEmulationEnabled', {enabled: true, maxTouchPoints: 1});
    await panMode.click();
    const mobileMap = await map.boundingBox();
    const beforeTouchPan = await area.evaluate(element => element.style.left);
    await touchDrag(touch, {left: mobileMap.x + 60, top: mobileMap.y + 75, right: mobileMap.x + 96, bottom: mobileMap.y + 83});
    await page.waitForFunction(initial => document.querySelector('.station-map-marker-area').style.left !== initial, beforeTouchPan);
    assert.equal(await checked.count(), 1);
    await selectMode.click();
    await touchDrag(touch, await markerBounds(area));
    assert.equal(await area.getAttribute('aria-pressed'), 'true');
    assert.equal(await checked.count(), 3);
    await touch.send('Emulation.setTouchEmulationEnabled', {enabled: false});
    await touch.detach();
    await picker.getByRole('checkbox', {name: /Кр2/}).uncheck();
    await picker.getByRole('checkbox', {name: /АМЕ/}).uncheck();
    if (process.env.METEO_MAP_SCREENSHOTS) await page.screenshot({ path: '/tmp/station-map-mobile.png' });
    assert.ok(await picker.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    assert.ok((await picker.locator('[data-apply]').boundingBox()).y < 745);
    await picker.locator('[data-add]').click();
    await picker.getByRole('textbox', { name: 'Название станции' }).fill('Тестовая АМЕ');
    assert.equal(await picker.locator('[data-station-form] [name="profile"]').count(), 0);
    await picker.getByRole('checkbox', {name: /Температура почвы · 10 см/}).check();
    await picker.getByRole('checkbox', {name: /Атмосферное давление/}).check();
    await picker.locator('[data-save]').click();
    assert.equal(await picker.locator('input[name="latitude"]').evaluate(input => input.validity.valueMissing), true);
    assert.equal(posts.length, 0);
    const terrain = picker.getByRole('combobox', {name: /^Тип местности/});
    await terrain.selectOption('wetland');
    await picker.locator('[data-pick]').click();
    const mobileCanvas = await picker.locator('canvas').boundingBox();
    await page.mouse.click(mobileCanvas.x + 80, mobileCanvas.y + 95);
    const latitude = Number(await picker.getByLabel('Широта').inputValue());
    const longitude = Number(await picker.getByLabel('Долгота').inputValue());
    assert.ok(latitude > 56 && latitude < 57);
    assert.ok(longitude > 32 && longitude < 33);
    assert.equal(await picker.locator('[data-pick]').getAttribute('aria-pressed'), 'false');
    await picker.getByText('Дополнительные поля').click();
    await picker.getByLabel('Код станции').fill('testing-ame');
    await picker.getByLabel('Шаг сырых наблюдений').selectOption('1');
    await picker.getByLabel('Минимум сроков за сутки').fill('78');
    await picker.locator('[data-save]').click();
    await picker.getByRole('alert').getByText('Станция уже есть.').waitFor();
    assert.equal(await picker.getByRole('textbox', { name: 'Название станции' }).inputValue(), 'Тестовая АМЕ');
    await picker.locator('[data-save]').click();
    await picker.getByRole('alert').getByText('Каталог временно недоступен.').waitFor();
    assert.equal(await picker.locator('a[href="/operator"]').count(), 0);
    assert.equal(await terrain.inputValue(), 'wetland');
    await picker.locator('[data-save]').click();
    await picker.getByRole('checkbox', { name: /Тестовая АМЕ/ }).waitFor();
    assert.equal(await picker.getByRole('checkbox', { name: /Тестовая АМЕ/ }).isChecked(), true);
    assert.deepEqual(posts[0], posts[1]);
    assert.deepEqual(posts[1], posts[2]);
    assert.equal(posts[1].id, 'testing-ame');
    assert.equal(Object.hasOwn(posts[1], 'profile'), false);
    assert.equal(posts[1].terrain, 'wetland');
    assert.equal(posts[1].slot_minutes, 1);
    assert.equal(posts[1].minimum_slots, 78);
    assert.equal(posts[1].latitude, latitude);
    assert.equal(posts[1].longitude, longitude);
    assert.deepEqual(posts[1].params, ['soil_10', 'pressure_hpa']);
    await picker.locator('[data-apply]').click();
    await picker.waitFor({ state: 'hidden' });
    await result(['kr1', 'testing-ame']);

    await page.setViewportSize({width: 1280, height: 840});
    await page.evaluate(() => window.mountTestOverview());
    const overview = page.locator('#station-overview');
    const overviewMap = overview.locator('.station-map-viewport');
    const overviewList = overview.locator('[data-picker-list]');
    const selectedList = overview.locator('[data-selected-stations]');
    const otherList = overview.locator('[data-other-stations]');
    const overviewArea = overview.locator('.station-map-marker-area');
    await overviewArea.waitFor();
    assert.equal(await overview.getByRole('checkbox', {name: /Кр1/}).isChecked(), true);
    assert.ok((await overviewList.boundingBox()).x > (await overviewMap.boundingBox()).x);
    const overviewResult = expected => page.waitForFunction(value =>
      document.querySelector('#overview-result').textContent === JSON.stringify(value), expected);
    await overview.getByRole('checkbox', {name: /Кр2/}).check();
    await overviewResult(['kr1', 'kr2']);
    assert.equal(await overview.getByRole('checkbox', {name: /Кр2/}).evaluate(el => document.activeElement === el), true);
    assert.equal(await selectedList.getByRole('checkbox', {name: /Кр2/}).getAttribute('title'), 'Скрыть из статистики');
    await selectedList.getByRole('checkbox', {name: /Кр2/}).uncheck();
    await overviewResult(['kr1']);
    assert.equal(await otherList.getByRole('checkbox', {name: /Кр2/}).getAttribute('title'), 'Включить в статистику');
    await otherList.getByRole('checkbox', {name: /Кр2/}).check();
    await overviewArea.click();
    await overviewResult(['kr1', 'kr2', 'ame60']);
    const exact = overview.getByRole('button', {name: 'Контрольный пост', exact: true});
    await boxSelect(page, await markerBounds(exact));
    await overviewResult(['field-check']);
    await beginDrag(page, await markerBounds(overviewArea));
    const overviewFrame = overview.locator('.station-map-frame');
    await overviewFrame.waitFor();
    await page.keyboard.press('Escape');
    await overviewFrame.waitFor({state: 'hidden'});
    await page.mouse.up();
    await overviewResult(['field-check']);

    await page.evaluate(() => window.updateOverview(['ame60']));
    assert.equal(await overview.getByRole('checkbox', {name: /^АМЕ-60/}).isChecked(), true);
    await overviewResult(['field-check']);
    await overview.getByRole('button', {name: 'Открыть карту'}).click();
    await picker.getByRole('checkbox', {name: /^АМЕ-60/}).waitFor();
    assert.equal(await picker.getByRole('checkbox', {name: /^АМЕ-60/}).isChecked(), true);
    await picker.getByRole('checkbox', {name: /Кр2/}).check();
    await picker.locator('[data-apply]').click();
    await overviewResult(['ame60', 'kr2']);
    assert.equal(await overview.getByRole('checkbox', {name: /Кр2/}).isChecked(), true);
    if (process.env.METEO_MAP_SCREENSHOTS) await page.screenshot({path: '/tmp/station-overview-desktop.png'});

    await overview.getByRole('button', {name: 'Вся Россия'}).click();
    await overview.locator('[data-map-raster="russia"]').waitFor();
    assert.equal(requested.has('/russia-relief.webp'), true);
    assert.equal(await overview.locator('[data-reserve-credit]').isHidden(), true);
    const relief = await page.evaluate(async () => {
      const {loadRaster, russiaRaster} = await import('/station-map-raster.js');
      const image = await loadRaster(russiaRaster);
      const raster = document.createElement('canvas');
      raster.width = image.width;
      raster.height = image.height;
      const context = raster.getContext('2d');
      context.drawImage(image, 0, 0);
      const [west, south, east, north] = russiaRaster.bounds;
      const alpha = (lon, lat) => {
        const longitude = lon < 0 ? lon + 360 : lon;
        const x = (longitude - west) * image.width / (east - west);
        const y = (north - lat) * image.height / (north - south);
        return context.getImageData(x, y, 1, 1).data[3];
      };
      return {
        size: [image.width, image.height],
        reserve: alpha(32.970290, 56.464310),
        farEast: alpha(169, 67),
        dateline: alpha(-173.3, 65.4),
        outsideRussia: alpha(105, 47),
      };
    });
    assert.deepEqual(relief, {size: [4200, 1056], reserve: 255, farEast: 255, dateline: 255, outsideRussia: 0});
    const cluster = overview.locator('.station-map-overview-marker');
    await cluster.waitFor();
    assert.equal(await cluster.textContent(), '7');
    assert.match(await cluster.getAttribute('aria-label'), /7 станций рядом.*Кр1.*Восточный берег/);
    assert.equal(await overviewArea.isHidden(), true);
    assert.equal(await cluster.getAttribute('aria-pressed'), 'mixed');
    assert.equal(await overview.locator('[data-overview-marker-note]').isVisible(), true);
    assert.equal(await overview.locator('[data-area-note]').isHidden(), true);

    const actualPositions = await overviewMap.evaluate(map => {
      const point = element => ({x: parseFloat(element.style.left), y: parseFloat(element.style.top)});
      const {x, y} = map.getBoundingClientRect();
      return {
        offset: {x: x + map.clientLeft, y: y + map.clientTop},
        area: point(map.querySelector('.station-map-marker-area')),
        east: point(map.querySelector('[aria-label="Восточный берег, Западный берег"]')),
      };
    });
    const {offset, area: geoArea, east: geoEast} = actualPositions;
    await boxSelect(page, {left: offset.x + geoArea.x - 30, top: offset.y + geoArea.y - 28,
      right: offset.x + (geoEast.x + geoArea.x) / 2, bottom: offset.y + geoArea.y + 20});
    await overviewResult(['kr1', 'kr2', 'ame60', 'field-check', 'testing-ame']);
    assert.equal(await overview.getByRole('checkbox', {name: /Восточный берег/}).isChecked(), false);
    await cluster.click();
    assert.equal(await selectedList.getByRole('checkbox').count(), 7);
    assert.equal(await cluster.getAttribute('aria-pressed'), 'true');
    if (process.env.METEO_MAP_SCREENSHOTS) await overview.screenshot({path: '/tmp/station-overview-cluster.png'});
    await overview.getByRole('button', {name: /Показать на карте: Кр1/}).click();
    await overview.locator('[data-map-raster="reserve"]').waitFor();
    await cluster.waitFor({state: 'hidden'});
    await overviewArea.waitFor();
    assert.equal(await overviewArea.textContent(), '3');
    assert.equal(await overview.locator('[data-area-note]').isVisible(), true);
    assert.equal(await exact.isVisible(), true);

    await page.setViewportSize({width: 390, height: 780});
    assert.ok((await overviewList.boundingBox()).y > (await overviewMap.boundingBox()).y);
    assert.ok(await overview.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    if (process.env.METEO_MAP_SCREENSHOTS) await page.screenshot({path: '/tmp/station-overview-mobile.png'});
    await page.evaluate(() => window.addOutsideStation());
    await overview.getByRole('button', {name: 'Показать на карте: Зарубежный пост'}).click();
    await overview.locator('[data-map-raster="vector"]').waitFor();
    const outside = overview.getByRole('button', {name: 'Зарубежный пост', exact: true});
    await outside.waitFor();
    await outside.click();
    assert.equal(await outside.getAttribute('aria-pressed'), 'true');
    await page.evaluate(() => window.destroyOverview());
    assert.equal(await overviewMap.count(), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    assert.equal(requested.has('/api/map-config'), false);
    console.log('Карта станций: обзорные группы, рамка по реальным точкам, паспорта, синхронизация с диалогом, мышь и касания, регистрация, 1280 и 390 px.');
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
