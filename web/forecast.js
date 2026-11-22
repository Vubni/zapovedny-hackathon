import {activeWeatherLocation, coordinates, distanceKm} from './weather-center.js';

const dailyFields = [
  ['temperature_2m_min', '°C'], ['temperature_2m_max', '°C'],
  ['precipitation_sum', 'mm'], ['wind_speed_10m_max', 'm/s'],
];
const hourlyFields = [['temperature_2m', '°C'], ['wind_speed_10m', 'm/s']];
function forecastEndpoint({point, bundled}) {
  const endpoint = new URL('https://api.open-meteo.com/v1/forecast');
  endpoint.search = new URLSearchParams({
    latitude: bundled ? point.latitude.toFixed(6) : point.latitude,
    longitude: bundled ? point.longitude.toFixed(6) : point.longitude,
    daily: [...dailyFields.map(([key]) => key), 'weather_code'].join(','),
    hourly: hourlyFields.map(([key]) => key).join(','),
    wind_speed_unit: 'ms', timezone: 'Europe/Moscow', forecast_days: '3',
  });
  return endpoint;
}

const numberFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const signedNumber = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2, signDisplay: 'exceptZero'});
const dateFormat = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'long', timeZone: 'UTC'});
const dayLabel = new Intl.DateTimeFormat('ru-RU', {
  weekday: 'short', day: 'numeric', month: 'long', timeZone: 'UTC',
});
const moscowDay = new Intl.DateTimeFormat('sv-SE', {
  year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Europe/Moscow',
});
const moscowHour = new Intl.DateTimeFormat('sv-SE', {
  hour: '2-digit', hourCycle: 'h23', timeZone: 'Europe/Moscow',
});
const moscowTime = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'short', timeStyle: 'short', timeZone: 'Europe/Moscow',
});
const wallTime = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
});
const byId = (id) => document.getElementById(id);
const statusLine = byId('forecast-status');
const panel = byId('forecast-data');
const button = byId('refresh');
const source = byId('forecast-source');
const periodControl = byId('forecast-hours');
let hourly = [];
let selectedHours = 24;
let forecastRequest = 0;

function hasValues(data, units, key, unit) {
  const values = data[key];
  return units?.[key] === unit && Array.isArray(values) &&
    values.length === data.time.length && values.every(value => value === null || Number.isFinite(value));
}

function dailyRows(response, point) {
  const daily = response.daily;
  if (!Array.isArray(daily?.time) || daily.time.length !== 3 ||
      response.daily_units?.time !== 'iso8601' || response.timezone !== 'Europe/Moscow') {
    throw new Error('Провайдер вернул неполный период');
  }
  if (distanceKm(response, point) > 30) throw new Error('Ответ относится к другой местности.');
  if (!dailyFields.every(([key, unit]) => hasValues(daily, response.daily_units, key, unit))) {
    throw new Error('Провайдер вернул неполную таблицу');
  }
  const hasWeather = hasValues(daily, response.daily_units, 'weather_code', 'wmo code');
  let previous;
  return daily.time.map((iso, index) => {
    const time = Date.parse(iso + 'T00:00:00Z');
    if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== iso ||
        (index > 0 && time - previous !== 86400000)) {
      throw new Error('Провайдер вернул несогласованные даты');
    }
    previous = time;
    return {
      iso, time, label: dateFormat.format(time),
      values: dailyFields.map(([key]) => daily[key][index]),
      code: hasWeather ? daily.weather_code[index] : null,
    };
  });
}

function hourlyRows(response, displayDay, retrieved) {
  const hours = response.hourly;
  if (!Array.isArray(hours?.time) || response.hourly_units?.time !== 'iso8601' || !hourlyFields.every(([key, unit]) =>
    hasValues(hours, response.hourly_units, key, unit))) return [];
  const start = hours.time.indexOf(displayDay + 'T' + moscowHour.format(retrieved) + ':00');
  if (start === -1) return [];
  const times = hours.time.slice(start, start + 48);
  const first = Date.parse(times[0] + ':00Z');
  if (!Number.isFinite(first)) return [];
  const rows = [];
  for (const [index, iso] of times.entries()) {
    const time = first + index * 3600000;
    if (new Date(time).toISOString().slice(0, 16) !== iso) break;
    rows.push({iso, time, values: hourlyFields.map(([key]) => hours[key][start + index])});
  }
  return rows;
}

const weatherIcons = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19m0-14-1.5 1.5m-11 11L5 19"/>',
  cloud: '<path d="M6.5 18a4.5 4.5 0 0 1-.7-9 6.1 6.1 0 0 1 11.7-1.3A5.15 5.15 0 0 1 17 18H6.5Z"/>',
  rain: '<path d="M5 15a4 4 0 0 1 0-8 6 6 0 0 1 11-2 5 5 0 0 1 3 10H5Zm2 3-1 3m6-3-1 3m6-3-1 3"/>',
  snow: '<path d="M12 2v20M3.34 7l17.32 10M3.34 17 20.66 7M9 4l3 3 3-3m-6 16 3-3 3 3M4 10l4.1-1.1L7 4.8m10 14.4-1.1-4.1L20 14M4 14l4.1 1.1L7 19.2M17 4.8l-1.1 4.1L20 10"/>',
  fog: '<path d="M6.5 13a4.5 4.5 0 0 1 1-8.8 5.1 5.1 0 0 1 9.3 1.2A3.9 3.9 0 0 1 18 13H6.5ZM4 17h16m-14 4h12"/>',
};

function weatherCondition(code, precipitation) {
  if (code === 0) return ['sun', 'Ясно'];
  if (code === 1 || code === 2) return ['cloud', 'Переменная облачность'];
  if (code === 3) return ['cloud', 'Пасмурно'];
  if ([45, 48].includes(code)) return ['fog', 'Туман'];
  if ([56, 57, 66, 67].includes(code)) return ['rain', 'Переохлаждённые осадки'];
  if ([51, 53, 55].includes(code)) return ['rain', 'Морось'];
  if ([61, 63, 65, 80, 81, 82].includes(code)) return ['rain', 'Дождь'];
  if ([71, 73, 75, 77, 85, 86].includes(code)) return ['snow', 'Снег'];
  if ([95, 96, 99].includes(code)) return ['rain', 'Гроза'];
  if (precipitation > 0) return ['rain', 'Осадки'];
  return [null, ''];
}

const displayNumber = (value) => value === null ? '—' : numberFormat.format(value);

function renderTable(id, rows) {
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const tr = document.createElement('tr');
    const heading = document.createElement('th');
    heading.scope = 'row';
    heading.textContent = row.label;
    tr.append(heading);
    for (const value of row.values) {
      const cell = document.createElement('td');
      cell.textContent = displayNumber(value);
      tr.append(cell);
    }
    fragment.append(tr);
  }
  byId(id).replaceChildren(fragment);
}

function renderDays(rows) {
  byId('forecast-days').innerHTML = rows.map(({iso, time, values, code}) => {
    const [min, max, precipitation, wind] = values;
    const [glyph, condition] = weatherCondition(code, precipitation);
    const temperatures = min === null || max === null
      ? [min === null ? null : 'мин. ' + signedNumber.format(min), max === null ? null : 'макс. ' + signedNumber.format(max)].filter(Boolean).join(' · ')
      : signedNumber.format(min) + '…' + signedNumber.format(max);
    const icon = glyph ? '<svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + weatherIcons[glyph] + '</svg>' : '';
    return `<div class="weather-day">
      <time class="small muted" datetime="${iso}">${dayLabel.format(time)}</time>
      <div class="forecast-day-reading">${icon}<strong>${temperatures ? temperatures + ' °C' : '—'}</strong></div>
      ${condition ? '<p class="small muted">' + condition + '</p>' : ''}
      <p class="small muted">Осадки ${displayNumber(precipitation)} мм · ветер до ${displayNumber(wind)} м/с</p>
    </div>`;
  }).join('');
}

function chartMarkup(rows, column, name, unit) {
  const values = rows.map(row => row.values[column]);
  const present = values.filter(value => value !== null);
  if (!present.length) return '<p class="note">На этот период значений нет.</p>';

  const width = 440, height = 220, left = 42, right = 421, top = 14, bottom = 190;
  const minimum = column === 1 ? 0 : Math.min(...present);
  const maximum = Math.max(...present);
  const roughStep = Math.max((maximum - minimum) / 4, column === 1 ? .5 : 1);
  const order = 10 ** Math.floor(Math.log10(roughStep));
  const step = [1, 2, 5, 10].map(factor => factor * order).find(value => value >= roughStep);
  const lower = Math.floor(minimum / step) * step;
  const upper = Math.max(lower + step, Math.ceil(maximum / step) * step);
  const x = index => left + (right - left) * index / Math.max(rows.length - 1, 1);
  const y = value => bottom - (bottom - top) * (value - lower) / (upper - lower);

  let guides = '';
  for (let index = 0; index <= Math.round((upper - lower) / step); index++) {
    const value = lower + index * step;
    const level = y(value).toFixed(2);
    guides += `<path class="grid" d="M${left} ${level}H${right}"/><text x="${left - 8}" y="${level}" dy=".32em" text-anchor="end">${numberFormat.format(value)}</text>`;
  }
  const labels = [...new Set([0, Math.round((rows.length - 1) / 3), Math.round((rows.length - 1) * 2 / 3), rows.length - 1])];
  for (const index of labels) {
    const at = x(index).toFixed(2);
    guides += `<path class="grid" d="M${at} ${top}V${bottom}"/><text x="${at}" y="${height - 8}" text-anchor="middle">${rows[index].iso.slice(11)}</text>`;
  }
  const spans = [];
  const points = [];
  let last = -2;
  for (const [index, value] of values.entries()) {
    if (value === null) continue;
    const px = x(index).toFixed(2);
    const py = y(value).toFixed(2);
    if (last !== index - 1) spans.push([]);
    spans[spans.length - 1].push([px, py]);
    last = index;
    points.push(`<circle class="forecast-chart-point" cx="${px}" cy="${py}" r="5">
      <title>${wallTime.format(rows[index].time)} МСК · ${numberFormat.format(value)} ${unit}</title>
    </circle>`);
  }
  const plots = spans.map(span => {
    const [firstX, firstY] = span[0];
    if (span.length === 1) return `<circle class="forecast-chart-single" cx="${firstX}" cy="${firstY}" r="2.5"/>`;
    const curve = 'M' + span.map(point => point.join(' ')).join('L');
    return `<path class="forecast-chart-area" d="${curve}V${bottom}H${firstX}Z"/><path class="plot-line forecast-chart-line" d="${curve}"/>`;
  }).join('');
  return `<svg class="graph" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="${name}, ${unit}. Точные значения приведены в таблице под графиками.">${guides}<g class="forecast-series">${plots}${points.join('')}</g></svg>`;
}

function renderHourly() {
  const rows = hourly.slice(0, selectedHours);
  const available = rows.length > 0;
  const showControls = hourly.length >= 48;
  periodControl.hidden = !showControls;
  byId('forecast-hourly-empty').hidden = available;
  byId('forecast-charts').hidden = !available;
  byId('forecast-hourly-details').hidden = !available;
  if (!available) return;

  const label = wallTime.format(rows[0].time) + ' — ' + wallTime.format(rows[rows.length - 1].time) + ' МСК';
  for (const target of document.querySelectorAll('[data-hourly-period]')) target.textContent = label;
  for (const control of periodControl.querySelectorAll('button')) {
    control.setAttribute('aria-pressed', String(Number(control.dataset.hours) === selectedHours));
  }
  byId('forecast-temperature').innerHTML = chartMarkup(rows, 0, 'Температура воздуха', '°C');
  byId('forecast-wind').innerHTML = chartMarkup(rows, 1, 'Скорость ветра', 'м/с');
  renderTable('forecast-hourly-rows', rows.map(row => ({label: wallTime.format(row.time), values: row.values})));
}

function render(response, retrieved, saved, point) {
  const rows = dailyRows(response, point);
  if (rows[0].iso !== moscowDay.format(retrieved)) throw new Error('Ответ относится к другой дате');
  const expired = rows.at(-1).iso < moscowDay.format(new Date());
  hourly = hourlyRows(response, rows[0].iso, retrieved);
  renderDays(rows);
  renderTable('forecast-rows', rows);
  renderHourly();
  byId('forecast-period').textContent = rows[0].label + ' — ' + rows[rows.length - 1].label +
    '. Получен ' + moscowTime.format(retrieved) + ' МСК.';
  source.classList.toggle('warn', saved);
  const copyLabel = expired ? 'Архивная копия' : 'Сохранённая копия';
  source.textContent = saved ? copyLabel : 'Open-Meteo';
  source.hidden = false;
  panel.hidden = false;
  return expired;
}

function showLocation(location) {
  byId('forecast-location').textContent = location.name + ' · московское время';
  byId('forecast-place').textContent = location.point
    ? (location.bundled ? 'Прогноз для района ' : 'Прогноз для станции ') +
      location.label + ' · ' + coordinates(location.point)
    : 'Прогноз строится для станции с указанными координатами.';
  for (const item of document.querySelectorAll('[data-forecast-place]')) item.textContent = location.label || '';
  byId('forecast-local-links').hidden = !location.bundled;
}

async function loadForecast() {
  const request = ++forecastRequest;
  button.disabled = true;
  panel.hidden = true;
  source.hidden = true;
  byId('forecast-local-links').hidden = true;
  byId('forecast-location').textContent = 'Краткосрочный прогноз · московское время';
  byId('forecast-place').textContent = 'Прогноз строится для станции с указанными координатами.';
  statusLine.textContent = 'Проверяем активный заповедник…';
  let location;
  try {
    location = await activeWeatherLocation();
    if (request !== forecastRequest) return;
    showLocation(location);
    if (!location.point) {
      statusLine.textContent = 'Добавьте метеостанцию с координатами в этом заповеднике, чтобы открыть прогноз.';
      return;
    }
    statusLine.textContent = 'Запрашиваем свежий прогноз Open-Meteo…';
    const endpoint = forecastEndpoint(location);
    try {
      const response = await fetch(endpoint, {
        signal: AbortSignal.timeout(5500), cache: 'no-store', credentials: 'omit',
      });
      if (!response.ok) throw new Error('Сервис недоступен');
      const data = await response.json();
      if (request !== forecastRequest) return;
      render(data, new Date(), false, location.point);
      statusLine.textContent = 'Получен актуальный прогноз от Open-Meteo.';
    } catch {
      if (request !== forecastRequest) return;
      if (!location.bundled) {
        statusLine.textContent = 'Не удалось получить прогноз для выбранной станции. Попробуйте обновить страницу позже.';
        return;
      }
      const response = await fetch('/forecast-example.json', {signal: AbortSignal.timeout(5500)});
      if (!response.ok) throw new Error('Копия недоступна');
      const snapshot = await response.json();
      if (request !== forecastRequest) return;
      const retrieved = new Date(snapshot.retrieved_utc);
      if (!Number.isFinite(retrieved.getTime()) || new URL(snapshot.source_url).href !== endpoint.href) {
        throw new Error('Копия относится к другому запросу');
      }
      const expired = render(snapshot.response, retrieved, true, location.point);
      statusLine.textContent = expired
        ? 'Свежий прогноз недоступен. Показана архивная копия за прошедшие даты.'
        : 'Свежий прогноз недоступен. Показана сохранённая копия с датами ниже.';
    }
  } catch {
    if (request === forecastRequest) {
      statusLine.textContent = location?.bundled
        ? 'Не удалось получить прогноз или открыть сохранённую копию. Попробуйте обновить страницу позже.'
        : 'Не удалось определить заповедник или загрузить его станции. Попробуйте обновить страницу.';
    }
  } finally {
    if (request === forecastRequest) button.disabled = false;
  }
}

for (const control of document.querySelectorAll('.forecast-legend')) {
  control.addEventListener('click', () => {
    const visible = control.getAttribute('aria-pressed') !== 'true';
    control.setAttribute('aria-pressed', String(visible));
    byId(control.getAttribute('aria-controls')).classList.toggle('is-muted', !visible);
  });
}
for (const control of periodControl.querySelectorAll('button')) {
  control.addEventListener('click', () => {
    selectedHours = Number(control.dataset.hours);
    renderHourly();
  });
}
button.addEventListener('click', loadForecast);
window.addEventListener('meteoarchive:account-change', loadForecast);
window.addEventListener('pageshow', event => { if (event.persisted) loadForecast(); });
loadForecast();
