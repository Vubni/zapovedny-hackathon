// Browser check with a deterministic substitute for the documented Yandex SDK interfaces.
const assert = require('node:assert/strict');
const {readFile} = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const host = 'https://station-sdk.test';
const groups = [
  {point: {latitude: 56.52, longitude: 32.87}, area: true, stations: [
    {id: 'kr1', label: 'Кр1', selected: true}, {id: 'kr2', label: 'Кр2'},
  ]},
  {point: {latitude: 56.37, longitude: 33.10}, stations: [{id: 'field', label: 'Контрольный пост'}]},
];
const html = `<!doctype html><html lang="ru"><meta charset="utf-8">
<style>body{margin:0;min-height:1550px}#map{margin:240px 0 0 120px;width:620px;height:390px}</style>
<button id="open">Открыть Яндекс Карты</button><div id="map"></div>
<script type="module">
import {openYandexStationMap} from '/yandex-station-sdk.js';
window.events = [];
window.groups = ${JSON.stringify(groups)};
const offlineMarker = document.createElement('button');
offlineMarker.style.setProperty('--terrain-fill', 'conic-gradient(#5e6b2d 0% 50%, #8a5628 50% 100%)');
window.groups[0].element = offlineMarker;
window.openMap = async (apiKey = 'fixture') => {
  try {
    window.controller = await openYandexStationMap(document.querySelector('#map'), {
      apiKey, groups,
      onToggleGroup: group => events.push(['toggle', group.stations.map(station => station.id)]),
      onSelectIds: (ids, additive) => events.push(['select', ids, additive]),
      onPick: point => events.push(['pick', point]),
    });
    return 'ok';
  } catch (error) {
    return error.message;
  }
};
document.querySelector('#open').onclick = async () => window.openStatus = await openMap();
</script></html>`;

function mockSdk(namespace) {
  return `
    window[${JSON.stringify(namespace)}] = {
      ready(done) { done(); },
      Map: class {
        constructor(element, {center, zoom, behaviors}) {
          this.center = center;
          this.zoom = zoom;
          const listeners = new Map();
          const projection = {
            toGlobalPixels: ([lat, lon], level) => [lon * level * 50, -lat * level * 40],
            fromGlobalPixels: ([x, y], level) => [-y / (level * 40), x / (level * 50)],
          };
          const offset = () => {
            const rect = element.getBoundingClientRect();
            const [x, y] = projection.toGlobalPixels(this.center, this.zoom);
            return [rect.left + scrollX + rect.width / 2 - x, rect.top + scrollY + rect.height / 2 - y];
          };
          this.events = {
            add: (names, callback) => names.forEach(name => listeners.set(name, callback)),
            remove: names => names.forEach(name => listeners.delete(name)),
          };
          this.emit = () => listeners.get('boundschange')?.();
          this.options = {get(property) {
            if (property !== 'projection') throw Error('Unknown SDK option');
            return projection;
          }};
          this.converter = {
            globalToPage: point => point.map((value, index) => value + offset()[index]),
            pageToGlobal: point => point.map((value, index) => value - offset()[index]),
          };
          const attribution = document.createElement('button');
          attribution.type = 'button';
          attribution.textContent = '© Яндекс';
          attribution.style = 'position:absolute;bottom:5px;right:5px';
          attribution.onclick = () => window.events.push(['attribution']);
          element.append(attribution);
          element.addEventListener('pointerdown', event => {
            if (event.target !== element || !behaviors.includes('drag')) return;
            const x = event.clientX;
            element.setPointerCapture(event.pointerId);
            element.addEventListener('pointerup', end => {
              const [latitude, longitude] = this.center;
              this.setCenter([latitude, longitude - (end.clientX - x) / (this.zoom * 50)]);
            }, {once: true});
          });
        }
        getCenter() { return this.center; }
        getZoom() { return this.zoom; }
        setCenter(value) { this.center = value; this.emit(); return Promise.resolve(); }
        setZoom(value) { this.zoom = value; this.emit(); return Promise.resolve(); }
        destroy() { window.destroyed = (window.destroyed || 0) + 1; }
      },
    };
  `;
}

async function drag(page, box, modifier) {
  if (modifier) await page.keyboard.down(modifier);
  await page.mouse.move(box.x, box.y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width, box.y + box.height, {steps: 5});
  await page.mouse.up();
  if (modifier) await page.keyboard.up(modifier);
}

async function enclosing(marker) {
  const {x, y, width, height} = await marker.boundingBox();
  return {x: x + width / 2 - 25, y: y + height / 2 - 25, width: 50, height: 50};
}

async function run() {
  const source = await readFile(path.join(__dirname, '..', 'web', 'yandex-station-sdk.js'));
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1080, height: 700}, hasTouch: true});
    page.setDefaultTimeout(6000);
    const errors = [];
    let sdkRequests = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin === host && url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
      if (url.origin === host && url.pathname === '/yandex-station-sdk.js') {
        return route.fulfill({contentType: 'text/javascript', body: source});
      }
      if (url.origin === 'https://api-maps.yandex.ru' && url.pathname === '/2.1/') {
        sdkRequests++;
        assert.equal(url.searchParams.get('apikey'), 'fixture');
        assert.equal(url.searchParams.get('lang'), 'ru_RU');
        if (sdkRequests === 1) return route.abort();
        if (sdkRequests === 2) {
          const errorCallback = JSON.stringify(url.searchParams.get('onerror'));
          return route.fulfill({contentType: 'text/javascript', body: `window[${errorCallback}]({message:'denied'});`});
        }
        return route.fulfill({contentType: 'text/javascript', body: mockSdk(url.searchParams.get('ns'))});
      }
      return route.abort();
    });

    await page.goto(host);
    await page.waitForFunction(() => typeof window.openMap === 'function');
    assert.equal(sdkRequests, 0);
    assert.match(await page.evaluate(() => openMap('')), /Требуется ключ/);
    assert.equal(sdkRequests, 0);
    assert.match(await page.evaluate(() => openMap()), /Не удалось загрузить/);
    assert.match(await page.evaluate(() => openMap()), /Не удалось загрузить/);
    assert.equal(await page.locator('#map > *').count(), 0);
    await page.locator('#open').click();
    await page.waitForFunction(() => window.openStatus === 'ok');
    await page.evaluate(() => scrollTo(0, 160));

    const map = page.getByRole('group', {name: /^Станции на Яндекс Карте/});
    const area = map.getByRole('button', {name: /Район заповедника/});
    const field = map.getByRole('button', {name: 'Контрольный пост'});
    const modes = page.getByRole('group', {name: 'Управление Яндекс Картой'});
    const frame = page.locator('.yandex-station-frame');
    const lastEvent = () => page.evaluate(() => events.at(-1));
    await area.waitFor();
    assert.equal(await area.getAttribute('aria-pressed'), 'mixed');
    assert.match(await area.evaluate(element => getComputedStyle(element).backgroundImage), /^conic-gradient/);
    await area.press('Enter');
    await field.click();
    assert.deepEqual(await page.evaluate(() => events.slice(-2)), [['toggle', ['kr1', 'kr2']], ['toggle', ['field']]]);

    await area.focus();
    await page.evaluate(() => controller.update(groups, new Set(['kr1', 'kr2', 'field'])));
    assert.equal(await area.getAttribute('aria-pressed'), 'true');
    assert.ok(await area.evaluate(element => document.activeElement === element));
    await drag(page, await enclosing(area));
    assert.deepEqual(await lastEvent(), ['select', ['kr1', 'kr2'], false]);
    await drag(page, await enclosing(field), 'Control');
    assert.deepEqual(await lastEvent(), ['select', ['field'], true]);

    const aroundField = await enclosing(field);
    await page.mouse.move(aroundField.x, aroundField.y);
    await page.mouse.down();
    await page.mouse.move(aroundField.x + 30, aroundField.y + 30, {steps: 5});
    await frame.waitFor();
    await page.keyboard.press('Escape');
    await frame.waitFor({state: 'hidden'});
    await page.mouse.up();
    assert.deepEqual(await lastEvent(), ['select', ['field'], true]);

    const touch = await page.context().newCDPSession(page);
    await touch.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: aroundField.x, y: aroundField.y}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchMove',
      touchPoints: [{x: aroundField.x + aroundField.width, y: aroundField.y + aroundField.height}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    assert.deepEqual(await lastEvent(), ['select', ['field'], false]);
    await touch.detach();

    await modes.getByRole('button', {name: 'Перемещение'}).click();
    const beforePan = (await area.boundingBox()).x;
    const bounds = await map.boundingBox();
    await drag(page, {x: bounds.x + 80, y: bounds.y + 90, width: 60, height: 8});
    await page.waitForFunction(position => document.querySelector('.yandex-station-marker').getBoundingClientRect().left !== position, beforePan);
    await drag(page, await enclosing(area), 'Shift');
    assert.deepEqual(await lastEvent(), ['select', ['kr1', 'kr2'], false]);

    await page.evaluate(() => controller.focus(groups[0].point));
    await page.waitForFunction(() => {
      const view = document.querySelector('.yandex-station-view');
      const marker = view.querySelector('.yandex-station-marker[data-area="true"]');
      return Math.abs(parseFloat(marker.style.left) - view.clientWidth / 2) < 1;
    });
    const beforeZoom = (await field.boundingBox()).x;
    await modes.getByRole('button', {name: 'Приблизить'}).click();
    await page.waitForFunction(position => document.querySelector('.yandex-station-marker:not([data-area="true"])').getBoundingClientRect().left > position, beforeZoom);
    await page.evaluate(() => controller.setPicking(true));
    assert.equal(await area.isDisabled(), true);
    await map.press('Enter');
    assert.deepEqual(await lastEvent(), ['pick', groups[0].point]);
    await page.mouse.click(bounds.x + bounds.width / 2 + 55, bounds.y + bounds.height / 2 - 44);
    const [type, point] = await lastEvent();
    assert.equal(type, 'pick');
    assert.ok(Math.abs(point.latitude - 56.62) < 1e-8);
    assert.ok(Math.abs(point.longitude - 32.97) < 1e-8);

    await map.getByRole('button', {name: '© Яндекс'}).click();
    assert.deepEqual(await lastEvent(), ['attribution']);
    await page.evaluate(() => {
      controller.destroy();
      controller.destroy();
    });
    assert.equal(await page.evaluate(() => window.destroyed), 1);
    assert.equal(await page.locator('#map > *').count(), 0);
    assert.equal(await page.evaluate(() => openMap()), 'ok');
    assert.equal(sdkRequests, 3, 'После успешной загрузки SDK используется повторно');
    assert.deepEqual(errors, []);
    console.log('Проверены отложенная загрузка, повтор после ошибки, маркеры, рамка мышью и касанием, панорамирование, выбор координат и ссылка атрибуции.');
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
