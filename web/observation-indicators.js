const storageKey = 'meteoarchive.indicators.v1';
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const numbers = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const groups = [
  ['Температура воздуха', [
    ['air_mean', 'Средняя температура воздуха', 'air', 't_air', 'mean', '°C'],
    ['air_min', 'Минимальная температура воздуха', 'air', 't_air', 'min', '°C'],
    ['air_max', 'Максимальная температура воздуха', 'air', 't_air', 'max', '°C'],
  ]],
  ['Температура почвы', [
    ['soil_05_mean', 'Средняя температура почвы, 5 см', 'soil_05', 'soil_05', 'mean', '°C'],
    ['soil_10_mean', 'Средняя температура почвы, 10 см', 'soil_10', 'soil_10', 'mean', '°C'],
    ['soil_20_mean', 'Средняя температура почвы, 20 см', 'soil_20', 'soil_20', 'mean', '°C'],
  ]],
  ['Влажность', [
    ['rh_mean', 'Средняя влажность', 'rh', 'rh', 'mean', '%'],
    ['rh_min', 'Минимальная влажность', 'rh', 'rh', 'min', '%'],
    ['rh_max', 'Максимальная влажность', 'rh', 'rh', 'max', '%'],
  ]],
  ['Скорость и порывы ветра', [
    ['wind_mean', 'Средняя скорость ветра', 'wind_combined', 'wind_speed', 'mean', 'м/с'],
    ['wind_gust_max', 'Максимальный порыв ветра', 'wind_combined', 'wind_gust', 'max', 'м/с'],
  ]],
  ['Атмосферное давление', [
    ['pressure_mean', 'Среднее атмосферное давление', 'pressure', 'pressure_hpa', 'mean', 'гПа'],
    ['pressure_min', 'Минимальное атмосферное давление', 'pressure', 'pressure_hpa', 'min', 'гПа'],
    ['pressure_max', 'Максимальное атмосферное давление', 'pressure', 'pressure_hpa', 'max', 'гПа'],
  ]],
];
const indicators = groups.flatMap(([, items]) => items);
const known = new Set(indicators.map(([id]) => id));
const icons = {
  plus: 'M12 5v14M5 12h14',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M18 6 6 18',
  chevron: 'm7 10 5 5 5-5',
};
let nextMount = 0;

function node(tag, content = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}

function icon(name) {
  const namespace = 'http://www.w3.org/2000/svg';
  const element = document.createElementNS(namespace, 'svg');
  const settings = { width: 15, height: 15, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': 1.7, 'stroke-linecap': 'round', 'aria-hidden': 'true' };
  for (const [key, value] of Object.entries(settings)) element.setAttribute(key, value);
  const path = document.createElementNS(namespace, 'path');
  path.setAttribute('d', icons[name]);
  element.append(path);
  return element;
}

function restoreSelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    return new Set(Array.isArray(saved) ? saved.filter(id => known.has(id)) : []);
  } catch {
    return new Set();
  }
}

function saveSelection(selected) {
  try {
    localStorage.setItem(storageKey, JSON.stringify([...selected]));
  } catch {
    // Selection remains available for the current view.
  }
}

function isDay(value) {
  if (typeof value !== 'string' || !datePattern.test(value)) return false;
  const day = new Date(value);
  return Number.isFinite(day.valueOf()) && day.toISOString().slice(0, 10) === value;
}

function intervalKey(start, end) {
  return `${start}:${end}`;
}

function contextOf({ stations = [], start, end, intervals }) {
  const byId = new Map();
  for (const station of stations) {
    const item = typeof station === 'string' ? { id: station, label: station } : station;
    if (item?.id && !byId.has(item.id)) byId.set(item.id, item);
  }
  const periods = new Map();
  const ranges = intervals?.length ? intervals : [{ start, end }];
  for (const range of ranges) {
    if (!isDay(range.start) || !isDay(range.end) || range.start > range.end) continue;
    periods.set(intervalKey(range.start, range.end), { start: range.start, end: range.end });
  }
  return { stations: [...byId.values()], intervals: [...periods.values()],
    key: JSON.stringify([[...byId.keys()], [...periods.keys()]]) };
}

function queryFor(context, chart) {
  const { stations, intervals } = context;
  const start = intervals.reduce((earliest, range) => range.start < earliest ? range.start : earliest, intervals[0].start);
  const end = intervals.reduce((latest, range) => range.end > latest ? range.end : latest, intervals[0].end);
  const query = new URLSearchParams({ chart, stations: stations.map(station => station.id).join(','), start, end, grain: 'year' });
  if (intervals.length > 1) query.set('intervals', intervals.map(range => intervalKey(range.start, range.end)).join(','));
  return query;
}

function keyFor(station, parameter, range) {
  return JSON.stringify([station, parameter, range.start, range.end]);
}

function indexStatistics(statistics, intervals) {
  const index = new Map();
  for (const stat of statistics) {
    const range = stat.interval_start && stat.interval_end ? { start: stat.interval_start, end: stat.interval_end } :
      intervals.length === 1 ? intervals[0] : null;
    if (range) index.set(keyFor(stat.station, stat.parameter, range), stat);
  }
  return index;
}

const dayLabel = day => day.split('-').reverse().join('.');
const periodLabel = ({ start, end }) => start === end ? dayLabel(start) : `${dayLabel(start)} – ${dayLabel(end)}`;

function stationLabel(station, stat) {
  return stat?.label || station.label || station.id;
}

function displayValue(stat, metric, fallbackUnit) {
  const value = stat?.[metric];
  if (!Number.isFinite(value)) return node('span', 'Нет данных', 'observation-indicator__missing');
  const unit = stat.unit || fallbackUnit;
  const summary = node('span', '', 'observation-indicator__value');
  summary.append(node('strong', numbers.format(Math.abs(value) < 0.05 ? 0 : value)));
  if (unit) summary.append(node('span', unit, 'observation-indicator__unit'));
  if (metric === 'mean' && Number.isInteger(stat.years)) summary.append(node('small', 'Лет в расчёте — ' + stat.years));
  const date = stat[`${metric}_day`];
  if (isDay(date)) summary.title = dayLabel(date);
  return summary;
}

function periodValues(definition, context, index) {
  const [, , , parameter, metric, unit] = definition;
  const { stations, intervals } = context;
  const lookup = (station, range) => index.get(keyFor(station.id, parameter, range));
  if (intervals.length === 1) {
    const list = node('dl', '', 'observation-indicator__values');
    for (const station of stations) {
      const stat = lookup(station, intervals[0]);
      const row = node('div');
      const value = node('dd');
      value.append(displayValue(stat, metric, unit));
      row.append(node('dt', stationLabel(station, stat)), value);
      list.append(row);
    }
    const content = document.createDocumentFragment();
    content.append(node('p', periodLabel(intervals[0]), 'observation-indicator__period'), list);
    return content;
  }

  const wrap = node('div', '', 'observation-indicator__table-wrap');
  wrap.tabIndex = 0;
  const table = node('table', '', 'observation-indicator__table');
  table.setAttribute('aria-label', `${definition[1]} по выбранным периодам`);
  const head = table.createTHead().insertRow();
  for (const label of ['Станция', ...intervals.map(periodLabel)]) {
    const heading = node('th', label);
    heading.scope = 'col';
    head.append(heading);
  }
  const body = table.createTBody();
  for (const station of stations) {
    const row = body.insertRow();
    const statistics = intervals.map(range => lookup(station, range));
    const heading = node('th', stationLabel(station, statistics.find(Boolean)));
    heading.scope = 'row';
    row.append(heading);
    for (const stat of statistics) row.insertCell().append(displayValue(stat, metric, unit));
  }
  wrap.append(table);
  return wrap;
}

function catalogControl([id, title], change) {
  const label = node('label', '', 'catalog-item observation-indicator-catalog__item');
  const input = node('input');
  input.type = 'checkbox';
  input.dataset.toggleIndicator = id;
  input.addEventListener('change', () => change(id, input.checked));
  const mark = node('span', '', 'catalog-toggle observation-indicator-catalog__mark');
  mark.append(icon('plus'), icon('check'));
  label.append(input, node('span', title), mark);
  return { label, input };
}

function boardCard([id, title], mountId, change) {
  const article = node('article', '', 'card observation-indicator');
  article.dataset.observationIndicator = id;
  const header = node('header', '', 'observation-indicator__header');
  const heading = node('h4', title);
  heading.id = `observation-indicator-${mountId}-${id}`;
  article.setAttribute('aria-labelledby', heading.id);
  const close = node('button', '', 'icon-btn observation-indicator__close');
  close.type = 'button';
  close.setAttribute('aria-label', `Скрыть карточку «${title}»`);
  close.append(icon('close'));
  close.addEventListener('click', () => change(id, false));
  const body = node('div', '', 'observation-indicator__body');
  header.append(heading, close);
  article.append(header, body);
  return { article, body };
}

/** Mounts the numeric card catalog and board, independently of displayed charts. */
export function mountObservationIndicators({ catalog, container, onChange }) {
  const selected = restoreSelection();
  const mountId = ++nextMount;
  const details = node('details', '', 'observation-indicator-catalog');
  const summary = node('summary', 'Числовые карточки');
  summary.append(icon('chevron'));
  details.append(summary);
  const board = node('section', '', 'observation-indicators');
  const heading = node('h2', 'Числовые показатели');
  heading.id = `observation-indicators-${mountId}`;
  board.setAttribute('aria-labelledby', heading.id);
  board.append(heading);
  catalog.replaceChildren(details);
  container.replaceChildren(board);
  const requests = new Map();
  let context = contextOf({});
  const sections = groups.map(([theme, items]) => {
    const choices = node('section', '', 'catalog-group observation-indicator-catalog__group');
    choices.append(node('h4', theme));
    details.append(choices);
    const section = node('section', '', 'observation-indicators__group');
    section.append(node('h3', theme));
    const grid = node('div', '', 'observation-indicators__grid');
    section.append(grid);
    board.append(section);
    const entries = items.map(definition => {
      const { label, input } = catalogControl(definition, change);
      const card = boardCard(definition, mountId, change);
      choices.append(label);
      grid.append(card.article);
      return { definition, input, ...card };
    });
    return { section, entries };
  });

  function render() {
    container.hidden = selected.size === 0;
    board.classList.toggle('observation-indicators--comparison', context.intervals.length > 1);
    for (const { section, entries } of sections) {
      section.hidden = entries.every(({ definition }) => !selected.has(definition[0]));
      for (const { definition, article, input, body } of entries) {
        const [id, , source] = definition;
        input.checked = selected.has(id);
        article.hidden = !input.checked;
        if (article.hidden) continue;
        const request = requests.get(source);
        let message;
        if (!context.stations.length) message = 'Выберите хотя бы одну станцию';
        else if (!context.intervals.length) message = 'Укажите период наблюдений';
        else if (request?.error) message = 'Не удалось загрузить показатель. Нажмите «Показать», чтобы повторить.';
        else if (!request?.statistics) message = 'Загружаем наблюдения…';
        if (message) {
          const status = node('p', message, 'observation-indicator__status');
          status.setAttribute('role', 'status');
          body.replaceChildren(status);
        } else {
          body.replaceChildren(periodValues(definition, context, request.statistics));
        }
      }
    }
  }

  async function load(source) {
    const controller = new AbortController();
    const request = { controller };
    const requested = context;
    requests.set(source, request);
    try {
      const response = await fetch(`/api/observations?${queryFor(requested, source)}`, { signal: controller.signal });
      if (!response.ok) throw new Error('HTTP');
      const payload = await response.json();
      if (!Array.isArray(payload.statistics)) throw new Error('Statistics');
      request.statistics = indexStatistics(payload.statistics, requested.intervals);
    } catch {
      if (!controller.signal.aborted) request.error = true;
    }
    if (requests.get(source) === request) render();
  }

  function resetRequests() {
    for (const request of requests.values()) request.controller.abort();
    requests.clear();
  }

  function loadSelection() {
    const needed = new Set(indicators.filter(([id]) => selected.has(id)).map(([, , source]) => source));
    for (const [source, request] of requests) {
      if (needed.has(source) || request.statistics) continue;
      request.controller.abort();
      requests.delete(source);
    }
    if (context.stations.length && context.intervals.length) {
      for (const source of needed) if (!requests.has(source)) load(source);
    }
    render();
  }

  function change(id, enabled) {
    if (enabled) selected.add(id);
    else selected.delete(id);
    saveSelection(selected);
    loadSelection();
    onChange?.(selected.size);
  }

  render();
  return {
    get count() { return selected.size; },
    update(filters) {
      const next = contextOf(filters);
      if (next.key !== context.key || filters.refresh) resetRequests();
      if (filters.retry) for (const [source, request] of requests) if (request.error) requests.delete(source);
      context = next;
      loadSelection();
    },
    destroy() {
      resetRequests();
      details.remove();
      board.remove();
      container.hidden = true;
    },
  };
}
