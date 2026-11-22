import {land, lakes, rivers, roads, places} from './station-map-data.js';
import {reserveBoundary} from './precip-boundary.js';

const latitudes = [56.625, 56.5625, 56.5, 56.4375, 56.375];
const longitudes = [32.625, 32.75, 32.875, 33, 33.125];
const points = latitudes.flatMap(latitude => longitudes.map(longitude => ({latitude, longitude})));
const reserveIndex = 12;
const reserve = {latitude: 56.52, longitude: 32.87};
const northSouthKmPerDegree = 111.2;
const eastWestKmPerDegree = northSouthKmPerDegree * Math.cos(reserve.latitude * Math.PI / 180);
const dx = longitudes[1] - longitudes[0];
const dy = latitudes[0] - latitudes[1];
const area = {
  west: longitudes[0] - dx / 2,
  east: longitudes.at(-1) + dx / 2,
  south: latitudes.at(-1) - dy / 2,
  north: latitudes[0] + dy / 2,
};
const overview = points[reserveIndex];
const step = 3600;
const hours = 24;
const endpoint = new URL('https://api.open-meteo.com/v1/forecast');
endpoint.search = new URLSearchParams({
  latitude: points.map(point => point.latitude).join(','),
  longitude: points.map(point => point.longitude).join(','),
  hourly: 'precipitation',
  forecast_hours: String(hours),
  timeformat: 'unixtime',
  models: 'dwd_icon_eu',
  cell_selection: 'nearest',
});

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
const shading = document.querySelector('#precip-map-shading');
const pointsGroup = document.querySelector('#precip-map-points');
const scaleLabel = document.querySelector('#precip-scale');
const mapReading = document.querySelector('#precip-map-reading');
const zoomIn = document.querySelector('#precip-zoom-in');
const zoomOut = document.querySelector('#precip-zoom-out');
const zoomReset = document.querySelector('#precip-zoom-reset');
const zoomLevels = [1, 1.5, 2.25, 3];
const svgNamespace = 'http://www.w3.org/2000/svg';
let shown;
let selected = reserveIndex;
let center = overview;
let zoom = 0;

function coordinates(point) {
  return `${number.format(point.latitude)}° с. ш., ${number.format(point.longitude)}° в. д.`;
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
    if (!Number.isFinite(node?.latitude) || !Number.isFinite(node?.longitude) ||
        Math.abs(node.latitude - point.latitude) > 0.15 || Math.abs(node.longitude - point.longitude) > 0.2 ||
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

const placePins = new Set(['Посёлок Заповедный', 'Староселье', 'Барсуки', 'Стулово', 'Гусевка', 'Васьково', 'Кисловка', 'Падоры']);
const mapPlaces = [...places.filter(([name]) => placePins.has(name)), ...places.filter(([name]) => !placePins.has(name))];

function renderPlaces(bounds, project, width, height, reserveX, reserveY) {
  const occupied = [[reserveX - 10, reserveY - 10, reserveX + 10, reserveY + 10]];
  const labels = [];
  for (const [name, longitude, latitude] of mapPlaces) {
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
    center = points[index];
    renderMap();
  }
  renderHour();
  if (focus) cells[index].button.focus({preventScroll: true});
}

const cells = points.map((point, index) => {
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
pointsGroup.append(...cells.map(cell => cell.button));
shading.append(...cells.map(cell => cell.rect));

function renderMap() {
  const width = map.clientWidth;
  const height = map.clientHeight;
  if (!width || !height) return;
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
  const layers = [
    ['land', land, true], ['rivers', rivers], ['lakes', lakes, true],
    ['roads', roads], ['boundary', reserveBoundary, true],
  ];
  for (const [layer, shapes, closed] of layers) {
    document.querySelector('#precip-map-' + layer).setAttribute('d', shapesPath(shapes, bounds, project, closed));
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

  const [reserveX, reserveY] = project(reserve.longitude, reserve.latitude);
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
  center = zoom ? points[selected] : overview;
  renderMap();
}

new ResizeObserver(renderMap).observe(map);
zoomIn.addEventListener('click', () => setZoom(zoom + 1));
zoomOut.addEventListener('click', () => setZoom(zoom - 1));
zoomReset.addEventListener('click', () => setZoom(0));

function renderHour() {
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
    button.setAttribute('aria-label', `${coordinates(points[i])}, ${display}` +
      (i === reserveIndex ? ', ячейка с точкой в заповеднике' : ''));
    button.title = display;
  }
  const node = shown.nodes[selected];
  const value = node.precipitation[index];
  cellDetails.textContent = value === null ? 'Нет данных' : number.format(value) + ' мм';
  mapReading.textContent = cellDetails.textContent;
  coordinateDetails.textContent = 'Центр ячейки · ' + coordinates(points[selected]);
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

async function loadForecast() {
  refresh.disabled = true;
  statusLine.textContent = 'Запрашиваем свежий прогноз Open-Meteo…';
  delete statusLine.dataset.error;
  try {
    const response = await requestJSON(endpoint);
    const retrieved = new Date();
    const forecast = parseForecast(response, retrieved);
    const currentHour = Math.floor(retrieved.getTime() / 1000 / step) * step;
    if (forecast.times[0] > currentHour || forecast.times[0] < currentHour - step) {
      throw new Error('Устаревший прогноз');
    }
    renderForecast(forecast);
    statusLine.textContent = 'Прогноз получен ' + recorded(forecast) + '.';
  } catch {
    try {
      if (!shown) {
        const snapshot = await requestJSON('/precip-example.json');
        renderForecast(parseForecast(snapshot.response, new Date(snapshot.retrieved_utc), true));
      }
      const source = shown.bundled ? 'сохранённый' : 'ранее полученный';
      statusLine.textContent = `Свежий прогноз недоступен. Показан ${source} прогноз от ${recorded(shown)}.`;
    } catch {
      statusLine.dataset.error = 'true';
      statusLine.textContent = 'Свежий прогноз и сохранённая копия недоступны. Повторите попытку позже.';
    }
  } finally {
    refresh.disabled = false;
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
loadForecast();
