'use strict';

const latitudes = [56.625, 56.5625, 56.5, 56.4375, 56.375];
const longitudes = [32.625, 32.75, 32.875, 33, 33.125];
const points = latitudes.flatMap(latitude => longitudes.map(longitude => ({latitude, longitude})));
const reserveIndex = 12;
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
const nodeDetails = document.querySelector('#precip-node');
let shown;
let selected = reserveIndex;

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

function pointHeader(value, scope) {
  const heading = document.createElement('th');
  heading.scope = scope;
  heading.textContent = number.format(value) + '°';
  return heading;
}

function buildGrid() {
  const rows = document.querySelector('#precip-rows');
  const longitudeRow = document.querySelector('#precip-longitudes');
  longitudeRow.append(...longitudes.map(longitude => pointHeader(longitude, 'col')));
  const buttons = [];
  for (const [rowIndex, latitude] of latitudes.entries()) {
    const row = rows.insertRow();
    row.append(pointHeader(latitude, 'row'));
    for (let column = 0; column < longitudes.length; column++) {
      const index = rowIndex * longitudes.length + column;
      const cell = row.insertCell();
      const button = document.createElement('button');
      button.type = 'button';
      button.addEventListener('click', () => {
        selected = index;
        renderHour();
      });
      buttons.push(button);
      cell.append(button);
      if (index === reserveIndex) {
        const marker = document.createElement('span');
        marker.className = 'precip-marker';
        marker.setAttribute('aria-hidden', 'true');
        cell.append(marker);
      }
    }
  }
  return buttons;
}

const buttons = buildGrid();

function renderHour() {
  const index = Number(range.value);
  const time = shown.times[index];
  const interval = dateTime.formatRange((time - step) * 1000, time * 1000) + ' МСК';
  document.querySelector('#precip-period').textContent = interval;
  range.setAttribute('aria-valuetext', interval);
  previous.disabled = index === 0;
  next.disabled = index === hours - 1;

  for (const [i, button] of buttons.entries()) {
    const value = shown.nodes[i].precipitation[index];
    const display = value === null ? 'Нет данных' : number.format(value) + ' мм';
    button.textContent = value === null ? '—' : number.format(value);
    button.dataset.level = level(value);
    button.setAttribute('aria-pressed', String(i === selected));
    button.setAttribute('aria-label', `${coordinates(points[i])}, ${display}` +
      (i === reserveIndex ? ', квадрат с точкой в заповеднике' : ''));
  }
  const node = shown.nodes[selected];
  const value = node.precipitation[index];
  cellDetails.textContent = coordinates(points[selected]) + ' · ' +
    (value === null ? 'нет данных' : number.format(value) + ' мм');
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
  renderHour();
  panel.hidden = false;
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
