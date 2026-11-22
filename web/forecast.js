'use strict';

const fields = [
  ['temperature_2m_min', '°C'], ['temperature_2m_max', '°C'],
  ['precipitation_sum', 'mm'], ['wind_speed_10m_max', 'm/s'],
];
const endpoint = new URL('https://api.open-meteo.com/v1/forecast');
endpoint.search = new URLSearchParams({
  latitude: '56.52', longitude: '32.87', daily: fields.map(([key]) => key).join(','),
  wind_speed_unit: 'ms', timezone: 'Europe/Moscow', forecast_days: '3',
});
const numberFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const dateFormat = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'long', timeZone: 'UTC'});
const moscowDay = new Intl.DateTimeFormat('sv-SE', {
  year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Europe/Moscow',
});
const moscowTime = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'short', timeStyle: 'short', timeZone: 'Europe/Moscow',
});
const statusLine = document.querySelector('#forecast-status');
const panel = document.querySelector('#forecast-data');
const button = document.querySelector('#refresh');

function dailyRows(response) {
  const daily = response.daily;
  const days = daily?.time;
  if (!Array.isArray(days) || days.length !== 3 || response.timezone !== 'Europe/Moscow') {
    throw new Error('Провайдер вернул неполный период');
  }
  for (const [key, unit] of fields) {
    if (response.daily_units?.[key] !== unit || !Array.isArray(daily[key]) ||
        daily[key].length !== days.length || !daily[key].every(value => value === null || Number.isFinite(value))) {
      throw new Error('Провайдер вернул неполную таблицу');
    }
  }
  let previous;
  return days.map((iso, index) => {
    const time = Date.parse(iso + 'T00:00:00Z');
    if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== iso ||
        (index > 0 && time - previous !== 86400000)) {
      throw new Error('Провайдер вернул несогласованные даты');
    }
    previous = time;
    return {iso, label: dateFormat.format(time), values: fields.map(([key]) => daily[key][index])};
  });
}

function render(rows, retrieved, saved) {
  const body = document.querySelector('#forecast-rows');
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const tr = document.createElement('tr');
    const heading = document.createElement('th');
    heading.scope = 'row';
    heading.textContent = row.label;
    tr.append(heading);
    for (const value of row.values) {
      const cell = document.createElement('td');
      cell.textContent = value === null ? '—' : numberFormat.format(value);
      tr.append(cell);
    }
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
  document.querySelector('#forecast-title').textContent = saved ? 'Сохранённый прогноз' : 'Суточный прогноз';
  document.querySelector('#forecast-period').textContent =
    rows[0].label + ' — ' + rows[rows.length - 1].label + '. Получен ' + moscowTime.format(retrieved) + ' МСК.';
  panel.hidden = false;
}

async function loadForecast() {
  button.disabled = true;
  panel.hidden = true;
  statusLine.textContent = 'Запрашиваем свежий прогноз Open-Meteo…';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5500);
  try {
    const response = await fetch(endpoint, {signal: controller.signal, cache: 'no-store', credentials: 'omit'});
    if (!response.ok) throw new Error('Сервис недоступен');
    const rows = dailyRows(await response.json());
    const now = new Date();
    if (rows[0].iso !== moscowDay.format(now)) throw new Error('Ответ относится к другой дате');
    render(rows, now, false);
    statusLine.textContent = 'Получен актуальный прогноз от Open-Meteo.';
  } catch {
    try {
      const response = await fetch('/forecast-example.json');
      if (!response.ok) throw new Error('Копия недоступна');
      const snapshot = await response.json();
      const retrieved = new Date(snapshot.retrieved_utc);
      if (!Number.isFinite(retrieved.getTime())) throw new Error('Неизвестная дата сохранения');
      render(dailyRows(snapshot.response), retrieved, true);
      statusLine.textContent = 'Свежий прогноз недоступен. Показана сохранённая копия с датами ниже.';
    } catch {
      statusLine.textContent = 'Не удалось получить прогноз или открыть сохранённую копию. Попробуйте обновить страницу позже.';
    }
  } finally {
    clearTimeout(timer);
    button.disabled = false;
  }
}

button.addEventListener('click', loadForecast);
loadForecast();
