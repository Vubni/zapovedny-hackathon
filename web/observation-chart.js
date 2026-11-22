import { mountCoverageOverview } from './coverage-overview.js';

const cards = new WeakMap();
const svgNamespace = 'http://www.w3.org/2000/svg';
const DAY = 86_400_000;
const palette = ['#2b7650', '#426583', '#735579', '#945320', '#507f76', '#ae4b55', '#646f2e', '#59618a'];
const speedPalette = ['#b4d5bc', '#5d9a75', '#2b7650', '#426583', '#735579', '#945320'];
const imageFormats = new Map([['svg', 'SVG'], ['png', 'PNG'], ['jpeg', 'JPEG / JPG']]);
const compass = ['С', 'ССВ', 'СВ', 'ВСВ', 'В', 'ВЮВ', 'ЮВ', 'ЮЮВ', 'Ю', 'ЮЮЗ', 'ЮЗ', 'ЗЮЗ', 'З', 'ЗСЗ', 'СЗ', 'ССЗ'];
const grainLabels = { day: 'По суткам', decade: 'По декадам', month: 'По месяцам', year: 'По годам' };
const neutral = { ink: '#162820', mute: '#53645a', line: '#dce5db', grid: '#e5ebe3', median: '#c8d6c8', lime: '#d7e950' };
const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
const date = new Intl.DateTimeFormat('ru-RU', { timeZone: 'UTC', day: '2-digit', month: '2-digit', year: 'numeric' });
const monthDate = new Intl.DateTimeFormat('ru-RU', { timeZone: 'UTC', month: 'short', year: '2-digit' });
const shortDate = new Intl.DateTimeFormat('ru-RU', { timeZone: 'UTC', day: '2-digit', month: '2-digit' });
let nextCard = 0;
let rasterQueue = Promise.resolve();

function html(tag, className = '', content = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = content;
  return node;
}

function svg(tag, attrs, text) {
  const node = document.createElementNS(svgNamespace, tag);
  for (const [name, value] of Object.entries(attrs || {})) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

const iconPaths = {
  fullscreen: 'M7 3H3v4m10-4h4v4M3 13v4h4m10-4v4h-4',
  minimize: 'M3 7h4V3m6 0v4h4M7 17v-4H3m14 0h-4v4',
  download: 'M10 2.5v10m-4-4 4 4 4-4M3.5 13v3.5h13V13',
  table: 'M3 4h14v12H3zM3 8h14M3 12h14M8 4v12',
  close: 'm5 5 10 10M15 5 5 15',
  chart: 'M3 3v14h14M5.5 12.5l3.4-4.3 3.4 2.2 4.2-5.5',
  compass: 'M10 2.75a7.25 7.25 0 1 0 0 14.5 7.25 7.25 0 0 0 0-14.5Zm2.9 4.35-1.4 4.4-4.4 1.4 1.4-4.4Z',
  plus: 'M10 4v12M4 10h12',
  minus: 'M4 10h12',
  chevron: 'm5 8 5 5 5-5',
};

function icon(name, size = 16) {
  const node = svg('svg', { width: size, height: size, viewBox: '0 0 20 20', fill: 'none', 'aria-hidden': 'true' });
  node.append(svg('path', { d: iconPaths[name], stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
  return node;
}

function iconButton(name, label) {
  const node = html('button', 'icon-btn obs-chart__icon-button');
  node.type = 'button';
  labelIconButton(node, name, label);
  return node;
}

function labelIconButton(node, name, label) {
  node.title = label;
  node.setAttribute('aria-label', label);
  node.replaceChildren(icon(name));
}

function direction(x) {
  const index = compass.indexOf(String(x).toUpperCase());
  if (index !== -1) return index;
  const value = x === null || x === '' ? NaN : Number(x);
  if (!Number.isFinite(value)) return NaN;
  if (Number.isInteger(value) && value >= 0 && value < compass.length) return value;
  return Math.round(((value % 360 + 360) % 360) / 22.5) % compass.length;
}

function timestamp(x) {
  if (typeof x !== 'string') return NaN;
  // Archive times with no offset are calendar coordinates, independent of the browser clock.
  return Date.parse(/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?$/.test(x) ? `${x}Z` : x);
}

function keyOf(x, axis) {
  if (axis === 'time') return timestamp(x);
  if (axis === 'direction') return direction(x);
  return x === null || x === '' ? NaN : Number(x);
}

function addTo(map, key, item) {
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(item);
}

function lighter(color) {
  const input = color.length === 4 ? color.slice(1).split('').map(char => char.repeat(2)).join('') : color.slice(1);
  const values = input.match(/../g).map(channel => Math.round(43 + .83 * Number.parseInt(channel, 16)));
  return `#${values.map(value => value.toString(16).padStart(2, '0')).join('')}`;
}

function modelOf(data, options) {
  const axis = data.axis || 'time';
  const mode = options.mode || data.mode || 'modern';
  const grain = data.grain || options.grain || 'month';
  const series = (data.series || []).map((item, index) => {
    const id = String(item.id ?? index);
    const unit = item.unit ?? data.unit ?? '';
    const color = /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i.test(item.color) ? item.color : palette[index % palette.length];
    const points = (item.points || []).flatMap(point => {
      const key = keyOf(point.x, axis);
      return Number.isFinite(key) ? [{ ...point, key, y: Number.isFinite(point.y) ? point.y : null }] : [];
    }).sort((a, b) => a.key - b.key);
    return { ...item, id, unit, color, points, label: item.label || item.station || item.parameter || id, index,
      role: item.role || item.range, speedClass: item.speed_class || item.speedClass,
      byKey: new Map(points.map(point => [point.key, point])), hasValues: points.some(point => point.y !== null) };
  });
  const speedColors = new Map();
  const stationColors = new Set();
  for (const item of series) {
    if (item.speedClass) {
      if (!speedColors.has(item.speedClass)) speedColors.set(item.speedClass, speedPalette[speedColors.size % speedPalette.length]);
      item.color = speedColors.get(item.speedClass);
      continue;
    }
    if (axis === 'direction') continue;
    const key = `${item.station}|${item.interval ?? ''}|${item.color}`;
    if (stationColors.has(key)) {
      item.alternate = true;
      item.color = lighter(item.color);
    }
    stationColors.add(key);
  }
  const points = new Map();
  for (const item of series) {
    for (const point of item.points) if (!points.has(point.key)) points.set(point.key, point);
  }
  const keys = Array.from(points.keys()).sort((a, b) => a - b);
  const from = timestamp(options.from ?? data.from ?? data.start);
  const to = timestamp(options.to ?? data.to ?? data.end);
  const archiveWindow = Number.isFinite(from) && Number.isFinite(to) && to >= from ? [from, to] : null;
  const ends = [keys[0], keys.at(-1)].filter(Number.isFinite);
  let domain = ends.length ? [ends[0], ends.at(-1)] : [0, 0];
  if (axis === 'hour') domain = [keys[0] === 1 && keys.at(-1) === 24 ? 1 : 0, keys.at(-1) === 24 ? 24 : 23];
  if (axis === 'direction') domain = [-0.5, compass.length - 0.5];
  if (axis === 'time' && archiveWindow) {
    domain = [Math.min(from, ends[0] ?? from), Math.max(to, ends.at(-1) ?? to)];
  }
  const stations = [...new Set((data.coverage || []).map(row => row.label || row.station).filter(Boolean))];
  const period = axis === 'hour' ? 'Средний суточный ход' : axis === 'time' ? (grainLabels[grain] || '') : '';
  const intervals = options.intervals ?? data.intervals ?? [];
  const intervalCount = intervals.length > 1 ? `${intervals.length} ${intervals.length < 5 ? 'интервала' : 'интервалов'}` : '';
  const subtitle = [options.subtitle ?? data.subtitle ?? [stations.join(', '), period].filter(Boolean).join(' · '), intervalCount].filter(Boolean).join(' · ');
  return { ...data, id: data.id ?? data.chart, axis, grain, mode, series, points, domain, archiveWindow, stations, subtitle, intervals,
    hasValues: series.some(item => item.hasValues) };
}

function lowerBound(array, target, value = number => number) {
  let left = 0;
  let right = array.length;
  while (left < right) {
    const middle = (left + right) >>> 1;
    if (value(array[middle]) < target) left = middle + 1;
    else right = middle;
  }
  return left;
}

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const coord = value => Math.round(value * 10) / 10;
const valueOf = point => point.key;

function visiblePoints(item, [start, end]) {
  const first = lowerBound(item.points, start, valueOf);
  let last = lowerBound(item.points, end, valueOf);
  while (item.points[last]?.key === end) last++;
  return item.points.slice(first, last);
}

function prettySpan(point, axis, key) {
  if (axis === 'hour') return `${String(key).padStart(2, '0')}:00`;
  if (axis === 'direction') return compass[key];
  if (point?.label) return point.label;
  const end = timestamp(point?.x_end);
  return Number.isFinite(end) && end !== key ? `${date.format(key)} – ${date.format(end)}` : date.format(key);
}

function stepSize(raw) {
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(raw));
  return ([1, 2, 2.5, 5, 10].find(x => raw / power <= x) || 10) * power;
}

function yTicks(series, renderBars) {
  let low = renderBars ? 0 : Infinity;
  let high = renderBars ? 0 : -Infinity;
  for (const points of series) {
    for (const { y } of points) {
      if (y === null) continue;
      if (y < low) low = y;
      if (y > high) high = y;
    }
  }
  if (!Number.isFinite(low)) return [0, 1];
  if (high === low) {
    const margin = Math.max(1, Math.abs(high) * 0.05);
    low = renderBars ? 0 : low - margin;
    high += margin;
  }
  const step = stepSize((high - low) / 4);
  const first = Math.floor(low / step);
  const last = Math.ceil(high / step);
  return Array.from({ length: Math.max(2, last - first + 1) }, (_, index) => (first + index) * step);
}

function dateTicks([start, end], target) {
  const span = end - start;
  if (span === 0) return [[start, date.format(start)]];
  const ticks = [];
  const earliest = new Date(start);
  if (span >= 60 * DAY) {
    const startMonth = earliest.getUTCFullYear() * 12 + earliest.getUTCMonth();
    const raw = span / (30.44 * DAY * target);
    const step = [1, 2, 3, 6, 12, 24, 36, 60, 120, 240, 600].find(value => value >= raw) || Math.ceil(raw / 120) * 120;
    for (let current = Math.ceil(startMonth / step) * step; ; current += step) {
      const tick = Date.UTC(Math.floor(current / 12), current % 12, 1);
      if (tick > end) break;
      if (tick >= start) ticks.push([tick, step >= 12 ? String(new Date(tick).getUTCFullYear()) : monthDate.format(tick).replace(' г.', '')]);
    }
  } else {
    const step = Math.max(1, Math.ceil(span / (target * DAY))) * DAY;
    for (let tick = Math.ceil(start / step) * step; tick <= end; tick += step) ticks.push([tick, shortDate.format(tick)]);
  }
  return ticks.length ? ticks : [[start, date.format(start)]];
}

function axisTicks(model, domain, width) {
  if (model.axis === 'direction') {
    const directions = compass.map((name, index) => [index, name]);
    return width < 310 ? directions.filter(([index]) => index % 2 === 0) : directions;
  }
  if (model.axis === 'hour') {
    const step = width < 390 ? 4 : 3;
    return Array.from({ length: Math.floor((domain[1] - domain[0]) / step) + 1 }, (_, index) => {
      const key = domain[0] + index * step;
      return [key, String(key).padStart(2, '0')];
    });
  }
  return dateTicks(domain, Math.max(2, Math.floor(width / 76)));
}

function calendarPeriod(key, grain) {
  const day = new Date(key);
  const year = day.getUTCFullYear();
  const month = day.getUTCMonth();
  return grain === 'year' ? year : grain === 'month' ? year * 12 + month :
    (year * 12 + month) * 3 + Math.min(2, Math.floor((day.getUTCDate() - 1) / 10));
}

function separates(previous, point, model) {
  if (!previous) return false;
  if (model.axis === 'hour') return point.key - previous.key > 1;
  if (model.axis !== 'time') return false;
  if (model.grain === 'day') return point.key - previous.key > DAY;
  if (['decade', 'month', 'year'].includes(model.grain)) {
    return calendarPeriod(point.key, model.grain) - calendarPeriod(previous.key, model.grain) > 1;
  }
  return false;
}

function runs(points, model) {
  const result = [];
  let run = [];
  let previous;
  for (const point of points) {
    if (point.y === null || separates(previous, point, model)) {
      if (run.length) result.push(run);
      run = [];
    }
    if (point.y !== null) run.push(point);
    previous = point;
  }
  if (run.length) result.push(run);
  return result;
}

function reduceRun(run, x, width) {
  if (run.length <= width * 2) return run;
  const reduced = [];
  let pixel = -Infinity;
  let first, last, low, high;
  const flush = () => {
    if (!first) return;
    const retained = [...new Set([first, last, low, high])].sort((a, b) => a.key - b.key);
    reduced.push(...retained);
  };
  for (const point of run) {
    const position = Math.floor(x(point.key));
    if (position !== pixel) {
      flush();
      pixel = position;
      first = low = high = point;
    }
    if (point.y < low.y) low = point;
    if (point.y > high.y) high = point;
    last = point;
  }
  flush();
  return reduced;
}

function linePath(points, x, y) {
  return points.map(({ key, y: value }, index) => `${index ? 'L' : 'M'}${coord(x(key))} ${coord(y(value))}`).join('');
}

function plotText(x, y, text, attrs = {}) {
  return svg('text', { x, y, fill: neutral.mute, 'font-size': 11, 'font-weight': 700, ...attrs }, text);
}

function lineRanges(root, entries, model, x, y) {
  const keyOfRange = item => `${item.station}|${item.parameter}|${item.interval ?? ''}`;
  const highest = new Map(entries.filter(([item]) => item.role === 'max').map(([item]) => [keyOfRange(item), item]));
  for (const [item, points] of entries) {
    if (item.role !== 'min') continue;
    const maximum = highest.get(keyOfRange(item));
    if (!maximum) continue;
    const paired = points.map(point => {
      const high = maximum.byKey.get(point.key)?.y;
      return { ...point, y: high == null ? null : point.y, high };
    });
    for (const run of runs(paired, model)) {
      if (run.length < 2) continue;
      const inverse = run.slice().reverse().map(point => ({ key: point.key, y: point.high }));
      const path = `${linePath(run, x, y)}${linePath(inverse, x, y).replace(/^M/, 'L')}Z`;
      root.append(svg('path', { d: path, fill: item.color, 'fill-opacity': 0.14 }));
    }
  }
}

function lineMarks(root, entries, model, dimensions) {
  const { x, y, width, bottom } = dimensions;
  const fillSeries = model.mode === 'modern' && entries.length === 1 && !['min', 'max', 'trend', 'estimate'].includes(entries[0][0].role);
  for (const [item, points] of entries) {
    const line = [];
    const area = [];
    const dots = [];
    const showPoints = points.length <= 2 || (model.mode === 'togu' && points.length <= 180);
    for (const run of runs(points, model)) {
      const sampled = reduceRun(run, x, width);
      const path = linePath(sampled, x, y);
      if (sampled.length > 1) {
        line.push(path);
        if (fillSeries) area.push(`${path}L${coord(x(sampled.at(-1).key))} ${bottom}L${coord(x(sampled[0].key))} ${bottom}Z`);
      }
      if (showPoints || sampled.length === 1) {
        for (const point of sampled) {
          const px = coord(x(point.key));
          const py = coord(y(point.y));
          dots.push(`M${px - 2.6} ${py}a2.6 2.6 0 1 0 5.2 0a2.6 2.6 0 1 0 -5.2 0`);
        }
      }
    }
    if (area.length) root.append(svg('path', { d: area.join(''), fill: item.color, 'fill-opacity': 0.085 }));
    const attrs = { stroke: item.color, 'stroke-width': model.mode === 'modern' ? 2.25 : 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
    if (dashed(item, model.mode)) attrs['stroke-dasharray'] = '5 4';
    if (line.length) root.append(svg('path', { ...attrs, d: line.join(''), fill: 'none' }));
    if (dots.length) root.append(svg('path', { d: dots.join(''), fill: '#ffffff', stroke: item.color, 'stroke-width': 1.4 }));
  }
}

function barMarks(root, entries, model, dimensions) {
  const { x, y } = dimensions;
  const groupWidth = (x(1) - x(0)) * 0.78;
  const width = groupWidth / entries.length;
  for (const [index, [item, points]] of entries.entries()) {
    for (const point of points) {
      if (point.y === null) continue;
      const baseline = y(0);
      root.append(svg('rect', { x: x(point.key) - groupWidth / 2 + width * index, y: Math.min(baseline, y(point.y)),
        width: width * .88, height: Math.abs(baseline - y(point.y)), rx: model.mode === 'modern' ? 3 : 0.5, fill: item.color }));
    }
  }
}

function rectangularPlot(root, model, series, domain, width, height, origin = 0) {
  const left = 46;
  const right = width - 23;
  const top = origin + 24;
  const bottom = origin + height - 28;
  const unit = model.unit || series[0]?.unit;
  const entries = series.map(item => [item, visiblePoints(item, domain)]);
  const renderBars = model.axis === 'direction';
  const ticks = yTicks(entries.map(([, points]) => points), renderBars);
  const [start, end] = domain;
  const x = key => start === end ? (left + right) / 2 : left + (key - start) / (end - start) * (right - left);
  const y = value => bottom - (value - ticks[0]) / (ticks.at(-1) - ticks[0]) * (bottom - top);
  if (unit) root.append(plotText(left, top - 10, unit));
  for (const tick of ticks) {
    root.append(svg('path', { d: `M${left} ${y(tick)}H${right}`, stroke: tick === 0 ? neutral.median : neutral.grid, 'stroke-width': tick === 0 ? 1.25 : 1 }));
    root.append(plotText(left - 9, y(tick) + 4, number.format(tick), { 'text-anchor': 'end' }));
  }
  if (model.mode !== 'modern') {
    root.append(svg('path', { d: `M${left} ${top}V${bottom}H${right}`, fill: 'none', stroke: neutral.median }));
  }
  for (const [key, label] of axisTicks(model, domain, right - left)) {
    const size = model.axis === 'direction' && width < 510 ? 8.5 : 11;
    root.append(plotText(x(key), bottom + 19, label, { 'text-anchor': 'middle', 'font-size': size }));
  }
  const dimensions = { x, y, width: right - left, left, right, top, bottom };
  if (renderBars) barMarks(root, entries, model, dimensions);
  else {
    if (model.mode === 'modern') lineRanges(root, entries, model, x, y);
    lineMarks(root, entries, model, dimensions);
  }
  return dimensions;
}

function isRose(model) {
  return model.kind === 'rose' && (model.id !== 'wind_freq' || model.mode === 'togu');
}

function roseGroups(model, series) {
  const groups = new Map();
  for (const item of series) {
    const scale = item.role === 'speed' ? 'speed' : 'frequency';
    addTo(groups, `${item.station ?? ''}|${item.interval ?? ''}|${scale}|${item.unit}`, item);
  }
  const stationLabels = new Map((model.coverage || []).map(item => [item.station, item.label]));
  const scales = new Set(Array.from(groups.values(), group => group[0].role === 'speed' ? 'speed' : 'frequency'));
  return Array.from(groups, ([id, items]) => {
    const { station, unit, role, interval } = items[0];
    const stationLabel = stationLabels.get(station) || items[0].station_label || items[0].label.split(' · ')[0];
    const measure = role === 'speed' ? 'Средняя скорость' : 'Повторяемость';
    const periodLabel = items[0].interval_label;
    const subtitle = [[scales.size > 1 ? measure : '', unit].filter(Boolean).join(', '), periodLabel].filter(Boolean).join(' · ');
    return { id, items, stationLabel, periodLabel, subtitle, unit, stack: items.some(item => item.speedClass), hasValues: items.some(item => item.hasValues),
      calm: model.calm_by_interval?.[interval]?.[station] ?? model.calm_by_station?.[station] ?? items[0].calm };
  });
}

function roseBounds(groups) {
  const bounds = new Map();
  for (const group of groups) {
    let largest = bounds.get(group.unit) || 0;
    for (let key = 0; key < compass.length; key++) {
      const values = group.items.map(item => item.byKey.get(key)?.y || 0);
      const extent = group.stack ? values.reduce((sum, value) => sum + value, 0) : Math.max(...values);
      largest = Math.max(largest, extent);
    }
    bounds.set(group.unit, largest);
  }
  return bounds;
}

function roseLayout(groups, width, big) {
  const fit = Math.max(1, Math.min(groups.length, Math.floor(width / 214)));
  let columns = fit;
  for (let candidate = fit; candidate > 1; candidate--) {
    if (groups.length % candidate === 0) {
      columns = candidate;
      break;
    }
  }
  const size = width / columns;
  const rowHeight = Math.min(big ? 342 : 266, Math.max(239, size));
  return { columns, size, rowHeight, height: Math.ceil(groups.length / columns) * rowHeight };
}

function roseSector(polar, radius, index, low, high) {
  const offset = .42;
  const a = polar(index - offset, high).join(' ');
  const b = polar(index + offset, high).join(' ');
  const c = polar(index + offset, low).join(' ');
  const d = polar(index - offset, low).join(' ');
  return `M${a}A${radius(high)} ${radius(high)} 0 0 1 ${b}L${c}${low ? `A${radius(low)} ${radius(low)} 0 0 0 ${d}` : ''}Z`;
}

function roseMarks(root, group, polar, radius, mode) {
  if (group.stack || mode === 'modern') {
    for (let i = 0; i < compass.length; i++) {
      let start = 0;
      for (const item of group.items) {
        const value = item.byKey.get(i)?.y;
        if (!(value > 0)) continue;
        root.append(svg('path', { d: roseSector(polar, radius, i, start, start + value), fill: item.color,
          stroke: '#fff', 'stroke-width': .65 }));
        start += value;
      }
    }
    return;
  }
  for (const item of group.items) {
    const points = compass.map((_name, index) => ({ key: index, y: item.byKey.get(index)?.y ?? null }));
    const complete = points.every(point => point.y !== null);
    for (const run of runs(points, { axis: 'direction' })) {
      const path = run.map(({ key, y }, index) => `${index ? 'L' : 'M'}${polar(key, y).join(' ')}`).join('');
      root.append(svg('path', { d: complete ? `${path}Z` : path, stroke: item.color, 'stroke-width': 1.9,
        fill: complete ? item.color : 'none', 'fill-opacity': .12, 'stroke-linejoin': 'round' }));
      for (const { key, y } of run) {
        const [cx, cy] = polar(key, y);
        root.append(svg('circle', { cx, cy, r: 2.6, fill: '#fff', stroke: item.color, 'stroke-width': 1.3 }));
      }
    }
  }
}

function radialPlot(root, model, groups, layout, origin = 0, onSector) {
  const { size, columns, rowHeight } = layout;
  const bounds = roseBounds(groups);
  for (const [index, group] of groups.entries()) {
    const cx = index % columns * size + size / 2;
    const top = origin + Math.floor(index / columns) * rowHeight;
    const cy = top + rowHeight / 2 + 14;
    const maxR = Math.min(size / 2 - 35, rowHeight / 2 - 50);
    const step = stepSize((bounds.get(group.unit) || 4) / 4);
    const max = Math.ceil((bounds.get(group.unit) || step * 4) / step) * step;
    const radius = value => coord(Math.max(0, value) * maxR / max);
    const polar = (i, v) => {
      const angle = i / compass.length * Math.PI * 2 - Math.PI / 2;
      return [coord(cx + radius(v) * Math.cos(angle)), coord(cy + radius(v) * Math.sin(angle))];
    };
    const centered = { 'text-anchor': 'middle', fill: neutral.ink };
    root.append(plotText(cx, top + 16, group.stationLabel, { ...centered, 'font-size': 13, 'font-weight': 800 }));
    if (group.subtitle) root.append(plotText(cx, top + 33, group.subtitle, { 'text-anchor': 'middle' }));
    if (!group.hasValues) {
      root.append(plotText(cx, cy, 'Нет данных', { 'text-anchor': 'middle' }));
      continue;
    }
    const ringLabels = [];
    for (let i = 1; i <= Math.round(max / step); i++) {
      const value = i * step;
      const r = radius(value);
      root.append(svg('circle', { cx, cy, r, fill: 'none', stroke: neutral.grid }));
      ringLabels.push(plotText(cx + 5, cy - r + 10, number.format(value), { 'font-size': 9, 'paint-order': 'stroke', stroke: '#fff', 'stroke-width': 2.5 }));
    }
    for (const [i, name] of compass.entries()) {
      const [x, y] = polar(i, max);
      const [lx, ly] = polar(i, max * 1.19);
      root.append(svg('path', { d: `M${cx} ${cy}L${x} ${y}`, stroke: neutral.grid }));
      root.append(plotText(lx, ly + 4, name, { ...centered, 'font-size': 10 }));
    }
    roseMarks(root, group, polar, radius, model.mode);
    root.append(...ringLabels);
    if (group.unit === '%' && Number.isFinite(group.calm)) {
      root.append(svg('circle', { cx, cy, r: 16, fill: '#fff', stroke: neutral.line }));
      const calm = plotText(cx, cy + 3.5, `${number.format(Math.round(group.calm * 10) / 10)}%`, { ...centered, 'font-size': 9.5 });
      calm.append(svg('title', {}, `Доля штиля: ${number.format(group.calm)}%`));
      root.append(calm);
    }
    if (onSector) {
      const targets = svg('g');
      for (const [i, name] of compass.entries()) {
        const target = svg('path', { d: roseSector(polar, radius, i, 0, max * 1.14), fill: 'transparent',
          class: 'obs-chart__sector', tabindex: i === 0 ? 0 : -1, 'aria-label': [group.stationLabel, group.periodLabel, name].filter(Boolean).join(' · ') });
        onSector(target, i, group, polar(i, max * .8));
        target.addEventListener('keydown', event => {
          const step = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
          if (!step) return;
          event.preventDefault();
          target.setAttribute('tabindex', -1);
          const next = targets.children[(i + step + compass.length) % compass.length];
          next.setAttribute('tabindex', 0);
          next.focus();
        });
        targets.append(target);
      }
      root.append(targets);
    }
  }
}

function graphic(width, height, title) {
  return svg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': title,
    'font-family': 'Manrope, Avenir Next, Arial, sans-serif' });
}

function emptyContent(message, axis, detail = '') {
  const empty = html('div', 'empty-chart obs-chart__empty');
  empty.append(icon(axis === 'direction' ? 'compass' : 'chart', 29), html('h3', '', message));
  if (detail) empty.append(html('p', '', detail));
  return empty;
}

function coverageContent(state) {
  const { model } = state;
  const open = state.coverage?.open;
  state.coverageView?.setEnabled(false);
  state.coverageView = null;
  if (!model.coverage?.length && !model.notes?.length) return null;
  const content = html('details', 'obs-chart__coverage');
  content.open = Boolean(open);
  const summary = html('summary');
  summary.append(html('span', '', 'Покрытие по месяцам'), icon('chevron', 13));
  content.append(summary);
  const body = html('div', 'obs-chart__coverage-body');
  if (model.coverage?.length) {
    const table = html('div');
    const tableView = mountCoverageOverview(table, { compact: true });
    tableView.setEnabled(Boolean(open));
    tableView.update({
      stations: model.coverage_stations,
      start: model.start,
      end: model.end,
      intervals: model.intervals,
      chart: model.chart,
    });
    content.addEventListener('toggle', () => tableView.setEnabled(content.isConnected && content.open));
    state.coverageView = tableView;
    body.append(table);
  }
  for (const note of model.notes || []) body.append(html('p', 'obs-chart__coverage-note', note));
  content.append(body);
  return content;
}

function chartLegendEntries(model, series) {
  const singleStation = new Set(model.series.map(item => item.station)).size === 1;
  return series.map(item => ({ ...item, label: singleStation && item.speedClass
    ? [item.speedClass, item.interval_label].filter(Boolean).join(' · ') : item.label }));
}

function dashed(item, mode) {
  return item.alternate || ['trend', 'estimate'].includes(item.role) || (mode !== 'modern' && item.index % 2 === 1 && !item.speedClass);
}

function renderLegend(model, hidden, onToggle) {
  const legend = html('div', 'legend obs-chart__legend');
  legend.setAttribute('aria-label', 'Ряды графика');
  for (const item of chartLegendEntries(model, model.series)) {
    const key = html('button', 'legend-key');
    key.type = 'button';
    key.setAttribute('aria-pressed', String(!hidden.has(item.id)));
    const mark = html('i', `swatch${item.speedClass ? ' block' : dashed(item, model.mode) ? ' dash' : ''}`);
    mark.setAttribute('aria-hidden', 'true');
    mark.style.setProperty('--dot', item.color);
    key.append(mark, html('span', '', item.label));
    if (!item.hasValues) key.title = `Нет данных: ${item.label}`;
    key.addEventListener('click', () => {
      const pressed = key.getAttribute('aria-pressed') === 'true';
      key.setAttribute('aria-pressed', String(!pressed));
      onToggle(item.id, pressed);
    });
    legend.append(key);
  }
  return legend;
}

function exportLegend(model, series, width, start) {
  const nodes = [];
  let x = 26;
  let y = start;
  const ensureRoom = size => {
    if (x + size <= width - 24 || x === 26) return;
    x = 26;
    y += 22;
  };
  for (const item of chartLegendEntries(model, series)) {
    const textWidth = item.label.length * 7 + 38;
    ensureRoom(textWidth);
    if (dashed(item, model.mode)) {
      nodes.push(svg('path', { d: `M${x} ${y - 4}h16`, stroke: item.color, 'stroke-width': 2, 'stroke-dasharray': '5 3' }));
    } else if (item.speedClass) {
      nodes.push(svg('rect', { x, y: y - 9, width: 10, height: 10, rx: 2, fill: item.color }));
    } else {
      nodes.push(svg('circle', { cx: x + 5, cy: y - 4, r: 4, fill: item.color }));
    }
    nodes.push(plotText(x + (dashed(item, model.mode) ? 21 : 16), y, item.label, { 'font-size': 12 }));
    x += textWidth;
  }
  return { nodes, bottom: y + 24 };
}

function exportImage(model, series, domain, measuredWidth, periods, empty) {
  const width = Math.max(780, measuredWidth);
  const rose = isRose(model);
  const groups = rose ? roseGroups(model, series) : [];
  const layout = empty ? { height: 150 } : rose ? roseLayout(groups, width, true) : { height: 365 };
  const captions = [model.subtitle, ...periods].filter(Boolean);
  const top = 66 + captions.length * 18;
  const { nodes, bottom } = exportLegend(model, series, width, top + layout.height + 21);
  const root = graphic(width, bottom, model.title);
  root.append(svg('title', {}, model.title), svg('rect', { x: 0, y: 0, width, height: bottom, fill: '#fff' }));
  root.append(plotText(24, 35, model.title, { fill: neutral.ink, 'font-size': 24, 'font-weight': 800 }));
  for (const [index, caption] of captions.entries()) root.append(plotText(24, 57 + index * 17, caption, { 'font-size': 12 }));
  if (empty) {
    const centered = { 'text-anchor': 'middle' };
    root.append(plotText(width / 2, top + 65, empty.message, { ...centered, 'font-size': 17, fill: neutral.ink }));
    if (empty.detail) root.append(plotText(width / 2, top + 88, empty.detail, { ...centered, 'font-size': 12 }));
  } else if (rose) radialPlot(root, model, groups, layout, top);
  else rectangularPlot(root, model, series, domain, width, layout.height, top);
  root.append(...nodes);
  const source = new XMLSerializer().serializeToString(root);
  return { width, height: bottom, blob: new Blob([source], { type: 'image/svg+xml;charset=utf-8' }) };
}

export function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = html('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

async function rasterize({ blob, width, height }, type) {
  const url = URL.createObjectURL(blob);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = reject;
      image.src = url;
    });
    const scale = Math.min(2, 4096 / Math.max(width, height));
    const canvas = html('canvas');
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise((resolve, reject) => {
      canvas.toBlob(result => result ? resolve(result) : reject(new Error('Ошибка экспорта графика')), type, .94);
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function periodFilename(intervals) {
  if (intervals.length > 4) return `${intervals[0].start}_${intervals.at(-1).end}_интервалов-${intervals.length}`;
  return intervals.map(({ start, end }) => `${start}_${end}`).join("__");
}

function filename(model, format) {
  const title = String(model.title || 'график').replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '').toLowerCase() || 'график';
  const dates = model.intervals.length > 1 ? `_${periodFilename(model.intervals)}` : '';
  return `${title}${dates}.${format}`;
}

function csvField(value) {
  if (value == null) return '';
  if (typeof value === 'number') return String(value);
  let text = String(value);
  if (/^\s*[=+\-@]|^[\t\r\n]/.test(text)) text = `'${text}`;
  return /[;,"\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function chartCsv(model, series, domain) {
  const fields = ['Дата или интервал', 'Конец интервала', 'Станция', 'Параметр', 'Ряд', 'Единица', 'Значение'];
  const multiple = model.intervals.length > 1;
  if (model.axis === 'hour') fields[0] = 'Час';
  if (model.axis === 'direction') fields[0] = 'Направление';
  if (multiple) fields.push('Начало выбранного интервала', 'Конец выбранного интервала');
  const stationLabels = new Map((model.coverage || []).map(row => [row.station, row.label]));
  const rows = [fields];
  for (const item of series) {
    for (const point of visiblePoints(item, domain)) {
      const values = [point.x, point.x_end, stationLabels.get(item.station) || item.station_label || item.station || item.label,
        item.parameter_label || item.parameter || model.title, item.label, item.unit, point.y];
      if (multiple) values.push(item.interval_start, item.interval_end);
      rows.push(values);
    }
  }
  const csv = rows.map(row => row.map(csvField).join(';')).join('\r\n');
  return new Blob(['\uFEFF', csv, '\r\n'], { type: 'text/csv;charset=utf-8' });
}

function tooltipRow(item, key, label = item.label) {
  const value = item.byKey.get(key)?.y;
  const row = html('div', 'hrow obs-chart__tooltip-row');
  const name = html('span');
  const swatch = html('i', 'swatch');
  swatch.style.setProperty('--dot', item.color);
  name.append(swatch, html('span', '', label));
  const text = value == null ? 'Нет данных' : `${number.format(value)}${item.unit ? ` ${item.unit}` : ''}`;
  row.append(name, html('b', '', text));
  return row;
}

function showTooltip(tooltip, stage, heading, content, x, y) {
  tooltip.replaceChildren(html('strong', '', heading), content);
  tooltip.hidden = false;
  tooltip.classList.add('visible');
  const horizontally = x + tooltip.offsetWidth + 16 > stage.clientWidth ? x - tooltip.offsetWidth - 12 : x + 12;
  tooltip.style.left = `${clamp(horizontally, 3, Math.max(3, stage.clientWidth - tooltip.offsetWidth - 3))}px`;
  tooltip.style.top = `${Math.min(Math.max(3, y - 20), Math.max(3, stage.clientHeight - tooltip.offsetHeight - 3))}px`;
}

function hideTooltip(tooltip) {
  tooltip.hidden = true;
  tooltip.classList.remove('visible');
}

function nearestKey(keys, value) {
  const index = lowerBound(keys, value);
  if (index <= 0) return keys[0];
  if (index === keys.length) return keys.at(-1);
  return value - keys[index - 1] <= keys[index] - value ? keys[index - 1] : keys[index];
}

function bindLineHover(state, root, model, series, domain, dimensions, zoom) {
  const { x, y, left, top, bottom, right } = dimensions;
  const { stage, tooltip } = state;
  const assigned = new Set();
  for (const item of series) for (const point of visiblePoints(item, domain)) assigned.add(point.key);
  const keys = [...assigned].sort((a, b) => a - b);
  if (!keys.length) return;
  const overlay = svg('g', { 'pointer-events': 'none', 'aria-hidden': 'true' });
  const cross = svg('g', { visibility: 'hidden' });
  const crossLine = svg('path', { stroke: neutral.mute, 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: .65 });
  const dots = svg('g');
  cross.append(crossLine, dots);
  const selection = svg('rect', { y: top, height: bottom - top, fill: neutral.lime, 'fill-opacity': .22, stroke: '#82a04d', visibility: 'hidden' });
  overlay.append(selection, cross);
  const target = svg('rect', { x: left - 3, y: top, width: right - left + 6, height: bottom - top + 1, fill: 'transparent',
    class: 'obs-chart__target', tabindex: 0, role: 'group', 'aria-label': 'Значения графика. Стрелки влево и вправо меняют интервал.' });
  root.setAttribute('role', 'group');
  root.append(overlay, target);
  let shown;
  let drag;
  let frame = 0;
  const svgX = clientX => {
    const rect = root.getBoundingClientRect();
    return clamp((clientX - rect.left) * Number(root.getAttribute('width')) / rect.width, left, right);
  };
  const keyAt = x => domain[0] + (x - left) / (right - left) * (domain[1] - domain[0]);
  const clear = () => {
    shown = undefined;
    cross.setAttribute('visibility', 'hidden');
    hideTooltip(tooltip);
  };
  const display = (key, topOffset) => {
    if (key === shown && !tooltip.hidden) return;
    shown = key;
    const current = x(key);
    crossLine.setAttribute('d', `M${current} ${top}V${bottom}`);
    const content = html('div', 'obs-chart__tooltip-values');
    dots.replaceChildren();
    const comparable = model.axis === 'time' && model.intervals.length > 1 ? series.filter(item => item.byKey.has(key)) : series;
    for (const [index, item] of comparable.entries()) {
      const value = item.byKey.get(key)?.y;
      content.append(tooltipRow(item, key));
      if (value != null) dots.append(svg('circle', { cx: current, cy: y(value), r: 4, stroke: item.color, 'stroke-width': 1.5, fill: index === 0 ? neutral.lime : '#fff' }));
    }
    cross.setAttribute('visibility', 'visible');
    const htmlX = current * root.clientWidth / Number(root.getAttribute('width'));
    showTooltip(tooltip, stage, prettySpan(model.points.get(key), model.axis, key), content, htmlX, topOffset);
  };
  target.addEventListener('pointermove', event => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!target.isConnected) return;
      const position = svgX(event.clientX);
      if (drag) {
        clear();
        selection.setAttribute('x', Math.min(drag.start, position));
        selection.setAttribute('width', Math.abs(drag.start - position));
        selection.setAttribute('visibility', 'visible');
        return;
      }
      display(nearestKey(keys, keyAt(position)), event.clientY - stage.getBoundingClientRect().top);
    });
  });
  const leave = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    if (!drag) clear();
  };
  target.addEventListener('pointerleave', leave);
  target.addEventListener('blur', leave);
  target.addEventListener('click', event => {
    if (tooltip.hidden) display(nearestKey(keys, keyAt(svgX(event.clientX))), top);
  });
  if (zoom) {
    target.classList.add('is-zoomable');
    target.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.pointerType === 'touch') return;
      drag = { start: svgX(event.clientX), pointer: event.pointerId };
      target.setPointerCapture(event.pointerId);
    });
    target.addEventListener('pointerup', event => {
      if (drag?.pointer !== event.pointerId) return;
      const start = drag.start;
      const end = svgX(event.clientX);
      drag = null;
      selection.setAttribute('visibility', 'hidden');
      if (Math.abs(end - start) >= 9) zoom(keyAt(Math.min(start, end)), keyAt(Math.max(start, end)));
    });
    target.addEventListener('lostpointercapture', () => {
      drag = null;
      selection.setAttribute('visibility', 'hidden');
    });
  }
  target.addEventListener('focus', () => display(keys[0], top));
  target.addEventListener('keydown', event => {
    const direction = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (!direction && event.key !== 'Home' && event.key !== 'End') return;
    event.preventDefault();
    const current = lowerBound(keys, shown ?? keys[0]);
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : clamp(current + direction, 0, keys.length - 1);
    display(keys[index], top);
  });
}

function bindRoseHover(state, root, groups, node, key, group, [x, y]) {
  const { tooltip, stage } = state;
  const display = () => {
    const content = html('div', 'obs-chart__tooltip-groups');
    const comparable = groups.filter(item => item.unit === group.unit && item.hasValues);
    for (const group of comparable) {
      const values = html('div', 'obs-chart__tooltip-values');
      if (group.stack || comparable.length > 1) {
        values.append(html('div', 'obs-chart__tooltip-station', [group.stationLabel, group.periodLabel].filter(Boolean).join(' · ')));
      }
      for (const item of group.items) {
        values.append(tooltipRow(item, key, item.speedClass || group.subtitle || item.label));
      }
      content.append(values);
    }
    const width = Number(root.getAttribute('width'));
    const scale = root.clientWidth / width;
    showTooltip(tooltip, stage, compass[key], content, x * scale, y * scale);
  };
  node.addEventListener('pointerenter', display);
  node.addEventListener('pointerleave', () => hideTooltip(tooltip));
  node.addEventListener('focus', display);
  node.addEventListener('blur', () => hideTooltip(tooltip));
}

function currentWindow(state) {
  return state.zoom || state.model.domain;
}

function activeSeries(state) {
  return state.model.series.filter(item => !state.hidden.has(item.id));
}

function hasVisibleValues(series, domain) {
  return series.some(item => item.hasValues && visiblePoints(item, domain).some(point => point.y !== null));
}

function emptyChartText(state, series) {
  const { model, options } = state;
  const emptySelection = model.series.length === 0 && model.notes?.includes('Выберите хотя бы одну станцию');
  const emptyMessage = options.emptyMessage || (emptySelection ? 'Выберите хотя бы одну станцию' : '');
  const multiple = model.intervals.length > 1;
  const period = multiple ? 'в выбранных интервалах' : 'за выбранный период';
  const noData = multiple ? 'Нет данных в выбранных интервалах' : 'Нет данных за этот период';
  const message = emptyMessage || (model.hasValues && series.length === 0 ? 'Все ряды скрыты' : noData);
  const detail = !emptyMessage && !state.zoom && !model.hasValues && model.stations.length === 1
    ? `По станции ${model.stations[0]} нет наблюдений ${period}.`
    : '';
  return { message, detail };
}

function exportPeriods(state, domain) {
  const { model } = state;
  const timeAxis = model.axis === 'time' && model.points.size;
  const dates = state.zoom || model.archiveWindow || (timeAxis ? domain : null);
  const span = ([start, end]) => start === end ? date.format(start) : `${date.format(start)} – ${date.format(end)}`;
  if (model.intervals.length > 1) {
    const periods = model.intervals.map(({ start, end }) => span([timestamp(start), timestamp(end)]));
    if (state.zoom) periods.push(`Масштаб: ${span(state.zoom)}`);
    return periods;
  }
  return dates ? [span(dates)] : [];
}

async function exportChartFile(state, format, { includeEmpty = false } = {}) {
  if (!imageFormats.has(format)) throw new TypeError(`Неизвестный формат графика: ${format}`);
  if (cards.get(state.container) !== state) throw new Error('Карточка графика закрыта.');
  const { model, lastWidth } = state;
  const series = activeSeries(state);
  const domain = currentWindow(state);
  const empty = hasVisibleValues(series, domain) ? null : emptyChartText(state, series);
  if (empty && !includeEmpty) throw new Error('В видимой области графика нет данных.');

  // Rasterize the same SVG even if the card is updated while the image loads.
  const graphic = exportImage(model, series, domain, lastWidth, exportPeriods(state, domain), empty);
  const name = filename(model, format);
  if (format === 'svg') return { blob: graphic.blob, filename: name };
  const image = rasterQueue.then(() => rasterize(graphic, `image/${format}`));
  rasterQueue = image.then(() => undefined, () => undefined);
  return { blob: await image, filename: name };
}

function draw(state) {
  const { model, stage, plot, tooltip, options, controls, enlarged } = state;
  const domain = currentWindow(state);
  const series = activeSeries(state);
  const hasValues = hasVisibleValues(series, domain);
  const canZoom = model.axis === 'time' && model.hasValues && model.domain[1] - model.domain[0] > DAY;
  if (controls) {
    controls.image.setAttribute('aria-disabled', String(!hasValues));
    if (!hasValues) controls.image.parentElement.open = false;
    controls.csv.disabled = !series.some(item => visiblePoints(item, domain).length);
    controls.zoom.hidden = !canZoom;
    controls.zoomIn.disabled = !hasValues || domain[1] - domain[0] <= DAY;
    controls.zoomOut.disabled = !state.zoom;
    controls.reset.hidden = !state.zoom;
  }
  hideTooltip(tooltip);
  if (!hasValues) {
    const { message, detail } = emptyChartText(state, series);
    plot.replaceChildren(emptyContent(message, model.axis, detail));
    return;
  }
  const width = Math.max(220, Math.floor(stage.clientWidth || state.lastWidth || 640));
  state.lastWidth = width;
  const rose = isRose(model);
  const groups = rose ? roseGroups(model, series) : [];
  const height = enlarged ? Math.max(320, Math.min(660, window.innerHeight - 190)) : options.primary === false ? 182 : 250;
  const layout = rose ? roseLayout(groups, width, enlarged) : { height };
  const root = graphic(width, layout.height, model.title);
  root.classList.add('obs-chart__svg');
  plot.replaceChildren(root);
  if (rose) {
    root.setAttribute('role', 'group');
    radialPlot(root, model, groups, layout, 0, (...args) => bindRoseHover(state, root, groups, ...args));
  } else {
    const dims = rectangularPlot(root, model, series, domain, width, layout.height);
    bindLineHover(state, root, model, series, domain, dims, canZoom && ((start, end) => setZoom(state, start, end)));
  }
}

function setZoom(state, from, to) {
  const [start, end] = state.model.domain;
  const duration = Math.min(end - start, Math.max(DAY / 2, to - from));
  const left = clamp((from + to - duration) / 2, start, end - duration);
  const right = left + duration;
  state.zoom = left <= start && right >= end ? null : [left, right];
  draw(state);
}

function scaleZoom(state, factor) {
  const [start, end] = currentWindow(state);
  const offset = (end - start) * (factor - 1) / 2;
  setZoom(state, start - offset, end + offset);
}

function downloadMenu(state) {
  const menu = html('details', 'obs-chart__download');
  const image = html('summary', 'icon-btn obs-chart__icon-button obs-chart__download-trigger');
  image.dataset.chartAction = 'download';
  image.setAttribute('role', 'button');
  image.append(icon('download'), html('span', '', 'Скачать график'));
  menu.append(image);
  const options = html('div', 'obs-chart__download-list');
  image.addEventListener('click', event => {
    if (image.getAttribute('aria-disabled') === 'true') event.preventDefault();
  });
  menu.addEventListener('focusout', event => {
    if (!menu.contains(event.relatedTarget)) menu.open = false;
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open) {
      event.preventDefault();
      event.stopPropagation();
      menu.open = false;
      image.focus();
    }
  });
  for (const [format, label] of imageFormats) {
    const item = html('button', '', label);
    item.type = 'button';
    item.dataset.chartFormat = format;
    item.addEventListener('click', async () => {
      const { status } = state;
      status.textContent = '';
      menu.open = false;
      image.focus();
      try {
        const file = await exportChartFile(state, format);
        download(file.blob, file.filename);
      } catch {
        status.textContent = 'Не удалось скачать график. Попробуйте другой формат.';
      }
    });
    options.append(item);
  }
  menu.append(options);
  return { menu, image };
}

function toggleFullscreen(state) {
  const { dialog, panel } = state;
  if (state.enlarged) {
    dialog.close();
    return;
  }
  dialog.append(panel);
  dialog.showModal();
  state.enlarged = true;
  labelIconButton(state.controls.fullscreen, 'minimize', 'Выйти из полноэкранного режима');
  state.controls.fullscreen.focus();
  draw(state);
}

function closeFullscreen(state) {
  const { panel, container, dialog } = state;
  if (!state.enlarged) return;
  state.enlarged = false;
  container.prepend(panel);
  if (dialog.open) dialog.close();
  labelIconButton(state.controls.fullscreen, 'fullscreen', 'На весь экран');
  draw(state);
  state.controls.fullscreen.focus({ preventScroll: true });
}

function cardControls(state) {
  const actions = html('div', 'row chart-tools obs-chart__actions');
  const unit = state.model.unit;
  if (unit) actions.append(html('span', 'tag', unit));
  const fullscreen = iconButton(state.enlarged ? 'minimize' : 'fullscreen', state.enlarged ? 'Выйти из полноэкранного режима' : 'На весь экран');
  fullscreen.dataset.chartAction = 'fullscreen';
  fullscreen.addEventListener('click', () => toggleFullscreen(state));
  const { menu, image } = downloadMenu(state);
  const csv = iconButton('table', 'Скачать таблицу');
  csv.dataset.chartAction = 'csv';
  csv.addEventListener('click', () => download(chartCsv(state.model, activeSeries(state), currentWindow(state)), filename(state.model, 'csv')));
  actions.append(fullscreen, menu, csv);
  if (state.options.onHide) {
    const hide = iconButton('close', 'Скрыть график');
    hide.dataset.chartAction = 'hide';
    hide.addEventListener('click', () => {
      if (state.enlarged) closeFullscreen(state);
      state.options.onHide();
    });
    actions.append(hide);
  }
  const zoom = html('div', 'obs-chart__zoom');
  const zoomOut = iconButton('minus', 'Уменьшить масштаб');
  const zoomIn = iconButton('plus', 'Увеличить масштаб');
  zoomIn.addEventListener('click', () => scaleZoom(state, .55));
  zoomOut.addEventListener('click', () => scaleZoom(state, 1 / .55));
  const reset = html('button', 'obs-chart__reset', 'Сбросить масштаб');
  reset.type = 'button';
  reset.addEventListener('click', () => {
    state.zoom = null;
    draw(state);
  });
  zoom.append(reset, zoomOut, zoomIn);
  return { actions, fullscreen, image, csv, zoom, zoomOut, zoomIn, reset };
}

function paintCard(state) {
  const { model, panel, options } = state;
  const heading = html('header', 'card-head obs-chart__head');
  const text = html('div');
  const title = html(options.primary === false ? 'h3' : 'h2', '', model.title);
  title.id = state.titleId;
  text.append(title);
  if (model.subtitle) text.append(html('p', '', model.subtitle));
  state.controls = cardControls(state);
  heading.append(text, state.controls.actions);
  const stage = html('div', 'chart-host obs-chart__stage');
  const plot = html('div', 'obs-chart__plot');
  const tooltip = html('div', 'hover-box obs-chart__tooltip');
  tooltip.setAttribute('role', 'status');
  stage.append(plot, tooltip);
  state.stage = stage;
  state.plot = plot;
  state.tooltip = tooltip;
  const footer = html('div', 'obs-chart__footer');
  const legend = renderLegend(model, state.hidden, (id, visible) => {
    if (visible) state.hidden.add(id);
    else state.hidden.delete(id);
    draw(state);
  });
  footer.append(legend, state.controls.zoom);
  const coverage = coverageContent(state);
  state.coverage = coverage;
  const status = html('div', 'inline-status obs-chart__status');
  status.setAttribute('role', 'status');
  state.status = status;
  panel.replaceChildren(heading, stage, footer, status);
  if (coverage) panel.append(coverage);
  state.observer?.disconnect();
  state.observer?.observe(stage);
  draw(state);
}

/** Renders one server-provided observation chart and retains its local controls across updates. */
export function renderChartCard(container, data, options = {}) {
  let state = cards.get(container);
  if (!state) {
    const panel = html('div', 'card dashboard-chart obs-chart__panel');
    const dialog = html('dialog', 'obs-chart__fullscreen');
    const titleId = `observation-chart-title-${++nextCard}`;
    dialog.setAttribute('aria-labelledby', titleId);
    container.classList.add('obs-chart');
    container.setAttribute('aria-labelledby', titleId);
    container.replaceChildren(panel, dialog);
    state = { titleId, container, panel, dialog, hidden: new Set(), zoom: null, lastWidth: 0, enlarged: false };
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      closeFullscreen(state);
    });
    dialog.addEventListener('close', () => closeFullscreen(state));
    if (typeof ResizeObserver === 'function') {
      state.observer = new ResizeObserver(entries => {
        const width = Math.max(220, Math.floor(entries[0].contentRect.width));
        if (Math.abs(width - state.lastWidth) > 2 && state.model?.hasValues) draw(state);
      });
    }
    cards.set(container, state);
  }
  const model = modelOf(data, options);
  const windowKey = [model.id || model.title, model.axis, model.grain, ...model.domain,
    ...model.intervals.map(interval => `${interval.start}:${interval.end}`)].join('|');
  if (state.windowKey !== windowKey) state.zoom = null;
  if (state.model && state.model.id !== model.id) state.hidden.clear();
  state.model = model;
  state.windowKey = windowKey;
  state.options = options;
  paintCard(state);
  return {
    canExport() {
      return cards.get(container) === state && hasVisibleValues(activeSeries(state), currentWindow(state));
    },
    exportFile(format, options) {
      return exportChartFile(state, format, options);
    },
    destroy() {
      if (cards.get(container) !== state) return;
      state.observer?.disconnect();
      state.coverageView?.setEnabled(false);
      if (state.enlarged) closeFullscreen(state);
      cards.delete(container);
      container.replaceChildren();
      container.removeAttribute('aria-labelledby');
    },
  };
}
