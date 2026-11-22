import {land, lakes, rivers, roads, places} from './station-map-data.js';
import {reserveBoundary} from './precip-boundary.js';

import {activeWeatherLocation, coordinates, distanceKm, wrapLongitude} from './weather-center.js';
import {russiaRaster, reserveRaster, rasterOffsets} from './station-map-raster.js';

const northSouthKmPerDegree = 111.2;
const step = 3600;
const hours = 24;

function gridFor({point, bundled}) {
  let latitudes, longitudes, dx, dy;
  const eastWestKmPerDegree = northSouthKmPerDegree * Math.max(.05, Math.cos(point.latitude * Math.PI / 180));
  if (bundled) {
    latitudes = [56.625, 56.5625, 56.5, 56.4375, 56.375];
    longitudes = [32.625, 32.75, 32.875, 33, 33.125];
    dx = .125;
    dy = .0625;
  } else {
    dy = .12;
    dx = Number((13.3 / eastWestKmPerDegree).toFixed(5));
    const latitude = Math.max(-90 + 2 * dy, Math.min(90 - 2 * dy, point.latitude));
    const offsets = [-2, -1, 0, 1, 2];
    latitudes = offsets.map(offset => Number((latitude - dy * offset).toFixed(6)));
    longitudes = offsets.map(offset => Number((point.longitude + dx * offset).toFixed(6)));
  }
  const points = latitudes.flatMap(latitude => longitudes.map(longitude => ({latitude, longitude})));
  const forecastIndex = points.findIndex(item => Math.abs(item.latitude - point.latitude) <= dy / 2 &&
    Math.abs(item.longitude - point.longitude) <= dx / 2);
  const area = {
    west: longitudes[0] - dx / 2, east: longitudes.at(-1) + dx / 2,
    south: latitudes.at(-1) - dy / 2, north: latitudes[0] + dy / 2,
  };
  const overview = {latitude: (area.south + area.north) / 2, longitude: (area.west + area.east) / 2};
  return {latitudes, longitudes, points, dx, dy, forecastIndex, area, overview, eastWestKmPerDegree};
}

function forecastEndpoint({points}, bundled) {
  const endpoint = new URL('https://api.open-meteo.com/v1/forecast');
  endpoint.search = new URLSearchParams({
    latitude: points.map(point => point.latitude).join(','),
    longitude: points.map(point => bundled ? point.longitude : Number(wrapLongitude(point.longitude).toFixed(6))).join(','),
    hourly: 'precipitation', forecast_hours: String(hours), timeformat: 'unixtime',
    models: bundled ? 'dwd_icon_eu' : 'dwd_icon_global', cell_selection: 'nearest',
  });
  return endpoint;
}

const localPlaces = new Set(['Посёлок Заповедный', 'Староселье', 'Барсуки', 'Стулово', 'Гусевка', 'Васьково', 'Кисловка', 'Падоры']);
const localBase = {
  raster: reserveRaster,
  layers: [['land', land, true], ['rivers', rivers], ['lakes', lakes, true], ['roads', roads], ['boundary', reserveBoundary, true]],
  places: [...places.filter(([name]) => localPlaces.has(name)), ...places.filter(([name]) => !localPlaces.has(name))],
};

async function mapBase(bundled) {
  if (bundled) return localBase;
  const {russiaLand, russiaRivers, russiaLakes, russiaRoads, russiaPlaces} = await import('./russia-map-data.js');
  return {
    raster: russiaRaster,
    layers: [['land', russiaLand, true], ['rivers', russiaRivers], ['lakes', russiaLakes, true],
      ['roads', russiaRoads], ['boundary', [], true]],
    places: russiaPlaces,
  };
}

const dateTime = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
});
const shortDateTime = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
});
const recordedDateTime = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', dateStyle: 'medium', timeStyle: 'short',
});
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 4});
const statusLine = document.querySelector('#precip-status');
const panel = document.querySelector('#precip-data');
const refresh = document.querySelector('#precip-refresh');
const range = document.querySelector('#precip-hour');
const previous = document.querySelector('#precip-prev');
const next = document.querySelector('#precip-next');
const cellDetails = document.querySelector('#precip-cell');
const coordinateDetails = document.querySelector('#precip-coordinate');
const nodeDetails = document.querySelector('#precip-node');
const map = document.querySelector('#precip-map');
const drawing = document.querySelector('#precip-map-drawing');
const relief = document.querySelector('#precip-map-relief');
const shading = document.querySelector('#precip-map-shading');
const outline = document.querySelector('#precip-map-selection');
const pointsGroup = document.querySelector('#precip-map-points');
const scaleLabel = document.querySelector('#precip-scale');
const mapReading = document.querySelector('#precip-map-reading');
const zoomIn = document.querySelector('#precip-zoom-in');
const zoomOut = document.querySelector('#precip-zoom-out');
const zoomReset = document.querySelector('#precip-zoom-reset');
const zoomLevels = [1, 1.5, 2.25, 3];
const svgNamespace = 'http://www.w3.org/2000/svg';
let shown;
let geometry;
let locationKey;
let selected;
let center;
let zoom = 0;
let cells = [];
let forecastRequest = 0;

relief.addEventListener('load', () => { relief.dataset.loaded = 'true'; });

function pointLabel(point) {
  return coordinates({...point, longitude: Number(wrapLongitude(point.longitude).toFixed(6))});
}

function level(value) {
  if (value === null) return 'missing';
  if (value === 0) return '0';
  if (value < 0.2) return '1';
  if (value < 1) return '2';
  if (value < 3) return '3';
  return '4';
}

function parseForecast(response, retrieved, bundled = false) {
  const {points} = geometry;
  const times = response?.[0]?.hourly?.time;
  if (!Array.isArray(response) || response.length !== points.length ||
      !Array.isArray(times) || times.length !== hours ||
      !times.every((time, i) => Number.isInteger(time) && time % step === 0 &&
        (i === 0 || time - times[i - 1] === step)) ||
      !Number.isFinite(retrieved.getTime())) {
    throw new Error('Неполный прогноз');
  }

  const nodes = response.map((node, index) => {
    const point = points[index];
    const precipitation = node?.hourly?.precipitation;
    if (distanceKm(node, {...point, longitude: wrapLongitude(point.longitude)}) > 24 ||
        node.hourly_units?.time !== 'unixtime' || node.hourly_units?.precipitation !== 'mm' ||
        node.hourly?.time?.length !== hours || !node.hourly.time.every((time, i) => time === times[i]) ||
        !Array.isArray(precipitation) || precipitation.length !== hours ||
        !precipitation.every(value => value === null || (Number.isFinite(value) && value >= 0))) {
      throw new Error('Несогласованные данные узла');
    }
    return {latitude: node.latitude, longitude: node.longitude, precipitation};
  });
  if (!nodes.some(node => node.precipitation.some(value => value !== null))) {
    throw new Error('Суммы осадков отсутствуют');
  }
  return {times, nodes, retrieved, bundled};
}

function svgElement(tag, attributes) {
  const node = document.createElementNS(svgNamespace, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function overlaps(shape, bounds) {
  const [west, south, east, north] = shape;
  return west <= bounds.east && east >= bounds.west && south <= bounds.north && north >= bounds.south;
}

function shapesPath(shapes, bounds, project, closed = false) {
  return shapes.filter(shape => overlaps(shape, bounds)).map(([, , , , pairs]) => {
    const vertices = [];
    for (let i = 0; i < pairs.length; i += 2) vertices.push(project(pairs[i], pairs[i + 1]).map(x => x.toFixed(1)).join(' '));
    return 'M' + vertices.join('L') + (closed ? 'Z' : '');
  }).join('');
}

function renderPlaces(bounds, project, width, height, reserveX, reserveY) {
  const occupied = [[reserveX - 10, reserveY - 10, reserveX + 10, reserveY + 10]];
  const labels = [];
  for (const [name, rawLongitude, latitude] of geometry.base.places) {
    const longitude = center.longitude + wrapLongitude(rawLongitude - center.longitude);
    if (longitude < bounds.west || longitude > bounds.east || latitude < bounds.south || latitude > bounds.north) continue;
    const [x, y] = project(longitude, latitude);
    const end = x + 10 + name.length * 7;
    const box = [x - 3, y - 9, end, y + 9];
    if (box[0] < 7 || box[2] > width - 8 || box[1] < 28 || box[3] > height - 36 ||
        occupied.some(([left, top, right, bottom]) => left < box[2] && right > box[0] && top < box[3] && bottom > box[1])) continue;
    occupied.push(box);
    const dot = svgElement('circle', {cx: x, cy: y, r: 2.2});
    const label = svgElement('text', {x: x + 6, y: y + 4});
    label.textContent = name;
    labels.push(dot, label);
    if (labels.length >= (width < 440 ? 16 : 36)) break;
  }
  document.querySelector('#precip-map-places').replaceChildren(...labels);
}

function selectCell(index, focus = false) {
  selected = index;
  if (zoom && !cells[index].visible) {
    center = geometry.points[index];
    renderMap();
  }
  renderHour();
  if (focus) cells[index].button.focus({preventScroll: true});
}

function buildCells({points, latitudes, longitudes}) {
  cells = points.map((point, index) => {
    const rect = svgElement('rect', {class: 'precip-map-cell'});
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'precip-map-target';
    button.addEventListener('click', () => selectCell(index));
    button.addEventListener('keydown', event => {
      const row = Math.floor(index / longitudes.length);
      const column = index % longitudes.length;
      const offsets = {
        ArrowLeft: column > 0 ? -1 : 0,
        ArrowRight: column + 1 < longitudes.length ? 1 : 0,
        ArrowUp: row > 0 ? -longitudes.length : 0,
        ArrowDown: row + 1 < latitudes.length ? longitudes.length : 0,
      };
      if (!Object.hasOwn(offsets, event.key)) return;
      event.preventDefault();
      if (offsets[event.key]) selectCell(index + offsets[event.key], true);
    });
    return {button, rect, point, visible: true};
  });
  pointsGroup.replaceChildren(...cells.map(cell => cell.button));
  shading.replaceChildren(...cells.map(cell => cell.rect));
}

function renderSelection() {
  const {rect} = cells[selected];
  for (const axis of ['x', 'y', 'width', 'height']) outline.setAttribute(axis, rect.getAttribute(axis));
}

function renderMap() {
  const width = map.clientWidth;
  const height = map.clientHeight;
  if (!geometry || !width || !height) return;
  const {area, dx, dy, latitudes, longitudes, location, base, eastWestKmPerDegree} = geometry;
  const fitted = Math.min(
    (width - 24) / ((area.east - area.west) * eastWestKmPerDegree),
    (height - 58) / ((area.north - area.south) * northSouthKmPerDegree),
  );
  const scale = fitted * zoomLevels[zoom];
  const xScale = eastWestKmPerDegree * scale;
  const yScale = northSouthKmPerDegree * scale;
  const project = (longitude, latitude) => [
    width / 2 + (longitude - center.longitude) * xScale,
    height / 2 - (latitude - center.latitude) * yScale,
  ];
  const bounds = {
    west: center.longitude - width / (2 * xScale),
    east: center.longitude + width / (2 * xScale),
    south: center.latitude - height / (2 * yScale),
    north: center.latitude + height / (2 * yScale),
  };
  drawing.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const rasterShift = rasterOffsets(base.raster.bounds, bounds)[0];
  if (rasterShift == null) {
    relief.setAttribute('visibility', 'hidden');
  } else {
    const [west, south, east, north] = base.raster.bounds;
    const [imageX, imageY] = project(west + rasterShift, north);
    const [imageRight, imageBottom] = project(east + rasterShift, south);
    relief.removeAttribute('visibility');
    relief.setAttribute('x', imageX);
    relief.setAttribute('y', imageY);
    relief.setAttribute('width', imageRight - imageX);
    relief.setAttribute('height', imageBottom - imageY);
  }
  const wraps = bounds.east > 180 ? [0, 360] : bounds.west < -180 ? [0, -360] : [0];
  for (const [layer, shapes, closed] of base.layers) {
    const path = wraps.map(shift => shapesPath(shapes, {
      ...bounds, west: bounds.west - shift, east: bounds.east - shift,
    }, (longitude, latitude) => project(longitude + shift, latitude), closed)).join('');
    document.querySelector('#precip-map-' + layer).setAttribute('d', path);
  }

  const minX = project(area.west, area.south)[0];
  const maxX = project(area.east, area.south)[0];
  const minY = project(area.west, area.north)[1];
  const maxY = project(area.west, area.south)[1];
  const grid = [`M${minX} ${minY}H${maxX}V${maxY}H${minX}Z`];
  for (let column = 1; column < longitudes.length; column++) {
    const [x] = project(area.west + dx * column, area.north);
    grid.push(`M${x} ${minY}V${maxY}`);
  }
  for (let row = 1; row < latitudes.length; row++) {
    const [, y] = project(area.west, area.north - dy * row);
    grid.push(`M${minX} ${y}H${maxX}`);
  }
  document.querySelector('#precip-map-cells').setAttribute('d', grid.join(''));

  const cellWidth = dx * xScale;
  const cellHeight = dy * yScale;
  for (const cell of cells) {
    const [x, y] = project(cell.point.longitude - dx / 2, cell.point.latitude + dy / 2);
    cell.visible = x >= -1 && y >= -1 && x + cellWidth <= width + 1 && y + cellHeight <= height + 1;
    cell.button.hidden = x + cellWidth <= 0 || y + cellHeight <= 0 || x >= width || y >= height;
    for (const [axis, value] of Object.entries({x, y, width: cellWidth, height: cellHeight})) cell.rect.setAttribute(axis, value);
    Object.assign(cell.button.style, {left: x + 'px', top: y + 'px', width: cellWidth + 'px', height: cellHeight + 'px'});
  }
  renderSelection();

  const [reserveX, reserveY] = project(location.point.longitude, location.point.latitude);
  const marker = document.querySelector('#precip-map-marker');
  marker.setAttribute('cx', reserveX);
  marker.setAttribute('cy', reserveY);
  renderPlaces(bounds, project, width, height, reserveX, reserveY);
  const kilometers = [1, 2, 5, 10, 20].find(distance => distance * scale >= 57) || 20;
  scaleLabel.style.width = kilometers * scale + 'px';
  scaleLabel.textContent = kilometers + ' км';
  zoomIn.disabled = zoom >= zoomLevels.length - 1;
  zoomOut.disabled = zoom === 0;
  zoomReset.disabled = zoom === 0;
}

function setZoom(value) {
  zoom = value;
  center = zoom ? geometry.points[selected] : geometry.overview;
  renderMap();
}

new ResizeObserver(renderMap).observe(map);
zoomIn.addEventListener('click', () => setZoom(zoom + 1));
zoomOut.addEventListener('click', () => setZoom(zoom - 1));
zoomReset.addEventListener('click', () => setZoom(0));

function renderHour() {
  const {points, forecastIndex} = geometry;
  const index = Number(range.value);
  const time = shown.times[index];
  const interval = dateTime.formatRange((time - step) * 1000, time * 1000) + ' МСК';
  document.querySelector('#precip-period').textContent = interval;
  range.setAttribute('aria-valuetext', interval);
  previous.disabled = index === 0;
  next.disabled = index === hours - 1;

  for (const [i, {button, rect}] of cells.entries()) {
    const value = shown.nodes[i].precipitation[index];
    const display = value === null ? 'Нет данных' : number.format(value) + ' мм';
    button.dataset.level = level(value);
    rect.dataset.level = button.dataset.level;
    button.tabIndex = i === selected ? 0 : -1;
    button.setAttribute('aria-pressed', String(i === selected));
    button.setAttribute('aria-label', `${pointLabel(points[i])}, ${display}` +
      (i === forecastIndex ? ', ячейка с точкой краткосрочного прогноза' : ''));
    button.title = display;
  }
  renderSelection();
  const node = shown.nodes[selected];
  const value = node.precipitation[index];
  cellDetails.textContent = value === null ? 'Нет данных' : number.format(value) + ' мм';
  mapReading.textContent = cellDetails.textContent;
  coordinateDetails.textContent = 'Центр ячейки · ' + pointLabel(points[selected]);
  nodeDetails.textContent = 'Узел модели ' + coordinates(node);
}

function renderForecast(forecast) {
  const oldHour = shown?.times[Number(range.value)];
  const retained = forecast.times.indexOf(oldHour);
  const current = forecast.bundled ? 0 : forecast.times.findIndex(time => time * 1000 > forecast.retrieved.getTime());
  shown = forecast;
  range.value = retained >= 0 ? retained : Math.max(0, current);
  document.querySelector('#precip-first').textContent = shortDateTime.format((forecast.times[0] - step) * 1000);
  document.querySelector('#precip-last').textContent = shortDateTime.format(forecast.times.at(-1) * 1000);
  panel.hidden = false;
  renderMap();
  renderHour();
}

async function requestJSON(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5500);
  try {
    const response = await fetch(url, {cache: 'no-store', credentials: 'omit', signal: controller.signal});
    if (!response.ok) throw new Error('Источник недоступен');
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function recorded(forecast) {
  return recordedDateTime.format(forecast.retrieved) + ' МСК';
}

function showLocation(location) {
  document.querySelector('#precip-location').textContent = location.name + ' · почасовой прогноз';
  document.querySelector('#precip-map-location').textContent = location.bundled
    ? 'Центрально-Лесной заповедник и окрестности' : 'Окрестности станции ' + (location.label || '');
  document.querySelector('#precip-forecast-point').textContent = location.point ? coordinates(location.point) : '';
  document.querySelector('#precip-model').textContent = location.bundled
    ? 'DWD ICON Europe с разрешением около 7 км' : 'DWD ICON Global с разрешением около 11 км';
  document.querySelector('#precip-step').textContent = location.bundled
    ? 'около 7 км с севера на юг и 8 км с запада на восток' : 'около 13 км';
  document.querySelector('#precip-model-link').textContent = location.bundled ? 'О модели ICON Europe' : 'О модели ICON Global';
  document.querySelector('#precip-boundary-key').hidden = !location.bundled;
  document.querySelector('#precip-provenance').hidden = !location.point;
  document.querySelector('#precip-russia-source').hidden = location.bundled || !location.point;
  for (const item of document.querySelectorAll('[data-precip-local]')) item.hidden = !location.bundled;
}

function clearLocation() {
  shown = geometry = locationKey = undefined;
  cells = [];
  pointsGroup.replaceChildren();
  shading.replaceChildren();
  relief.removeAttribute('href');
  delete relief.dataset.loaded;
}

async function prepareMap(location, request) {
  const key = JSON.stringify([location.id, location.point.latitude, location.point.longitude]);
  if (locationKey === key) return;
  clearLocation();
  const base = await mapBase(location.bundled);
  if (request !== forecastRequest) return;
  geometry = {...gridFor(location), location, base};
  locationKey = key;
  selected = geometry.forecastIndex;
  center = geometry.overview;
  zoom = 0;
  relief.setAttribute('href', geometry.base.raster.src);
  buildCells(geometry);
}

async function loadForecast() {
  const request = ++forecastRequest;
  refresh.disabled = true;
  panel.hidden = true;
  document.querySelector('#precip-location').textContent = 'Почасовой прогноз осадков';
  document.querySelector('#precip-provenance').hidden = true;
  statusLine.textContent = 'Проверяем активный заповедник…';
  delete statusLine.dataset.error;
  let location;
  try {
    location = await activeWeatherLocation();
    if (request !== forecastRequest) return;
    showLocation(location);
    if (!location.point) {
      clearLocation();
      statusLine.textContent = 'Добавьте метеостанцию с координатами в этом заповеднике, чтобы открыть карту осадков.';
      return;
    }
    await prepareMap(location, request);
    if (request !== forecastRequest) return;
    statusLine.textContent = 'Запрашиваем свежий прогноз Open-Meteo…';
    const endpoint = forecastEndpoint(geometry, location.bundled);
    try {
      const response = await requestJSON(endpoint);
      if (request !== forecastRequest) return;
      const retrieved = new Date();
      const forecast = parseForecast(response, retrieved);
      const currentHour = Math.floor(retrieved.getTime() / 1000 / step) * step;
      if (forecast.times[0] > currentHour || forecast.times[0] < currentHour - step) {
        throw new Error('Устаревший прогноз');
      }
      renderForecast(forecast);
      statusLine.textContent = 'Прогноз получен ' + recorded(forecast) + '.';
    } catch {
      if (request !== forecastRequest) return;
      let saved = shown;
      if (!saved && location.bundled) {
        const snapshot = await requestJSON('/precip-example.json');
        if (request !== forecastRequest) return;
        if (new URL(snapshot.request_url).href !== endpoint.href) throw new Error('Копия относится к другому району');
        saved = parseForecast(snapshot.response, new Date(snapshot.retrieved_utc), true);
      }
      if (!saved) {
        statusLine.dataset.error = 'true';
        statusLine.textContent = 'Не удалось получить прогноз осадков для выбранной станции. Попробуйте обновить страницу позже.';
        return;
      }
      renderForecast(saved);
      const source = saved.bundled ? 'сохранённый' : 'ранее полученный';
      statusLine.textContent = `Свежий прогноз недоступен. Показан ${source} прогноз от ${recorded(saved)}.`;
    }
  } catch {
    if (request === forecastRequest) {
      if (!location) clearLocation();
      statusLine.dataset.error = 'true';
      statusLine.textContent = location?.bundled
        ? 'Свежий прогноз и сохранённая копия недоступны. Повторите попытку позже.'
        : 'Не удалось определить район прогноза. Попробуйте обновить страницу.';
    }
  } finally {
    if (request === forecastRequest) refresh.disabled = false;
  }
}

range.addEventListener('input', renderHour);
previous.addEventListener('click', () => {
  range.stepDown();
  renderHour();
});
next.addEventListener('click', () => {
  range.stepUp();
  renderHour();
});
refresh.addEventListener('click', loadForecast);
window.addEventListener('meteoarchive:account-change', loadForecast);
window.addEventListener('pageshow', event => { if (event.persisted) loadForecast(); });
loadForecast();
