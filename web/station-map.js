import {land, lakes, places, rivers, roads} from './station-map-data.js';

const reserve = {latitude: 56.52, longitude: 32.87};
const initialScale = 410;
const maxScale = 9600;
const degrees = Math.PI / 180;
let mapId = 0;
const coordinateFormat = new Intl.NumberFormat('ru', {maximumFractionDigits: 5});
const commonParams = [
  ['t_air', 'Температура воздуха'], ['rh', 'Относительная влажность'],
  ['wind_speed', 'Скорость ветра'], ['wind_gust', 'Порывы ветра'], ['wind_dir', 'Направление ветра'],
];
const profileParams = {
  kr1: [['pressure_hpa', 'Атмосферное давление']],
  ame60: [['soil_05', 'Температура почвы · 5 см'], ['soil_10', 'Температура почвы · 10 см'],
    ['soil_20', 'Температура почвы · 20 см']],
};
const icons = {
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  locate: '<circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/><path d="M12 2v4m0 12v4M2 12h4m12 0h4"/>',
  fit: '<path d="M9 4H4v5m11-5h5v5M4 15v5h5m11-5v5h-5"/>',
  pin: '<path d="M19 10c0 5-7 10-7 10s-7-5-7-10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2"/>',
  select: '<path d="M8 4H4v4m6-4h4m2 0h4v4m0 2v4m0 2v4h-4m-2 0h-4m-2 0H4v-4m0-2v-4"/>',
  pan: '<path d="M12 4v16M4 12h16M9 7l3-3 3 3M9 17l3 3 3-3M7 9l-3 3 3 3m10-6 3 3-3 3"/>',
};

function icon(name) {
  return `<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${icons[name]}</svg>`;
}

function location(station) {
  const {latitude, longitude} = station;
  if (Number.isFinite(latitude) && Math.abs(latitude) <= 90 &&
      Number.isFinite(longitude) && Math.abs(longitude) <= 180) return {latitude, longitude};
  return station.location_precision === 'area' ? reserve : null;
}

function coordinates({latitude, longitude}) {
  const lat = `${coordinateFormat.format(Math.abs(latitude))}° ${latitude < 0 ? 'ю.' : 'с.'} ш.`;
  const lon = `${coordinateFormat.format(Math.abs(longitude))}° ${longitude < 0 ? 'з.' : 'в.'} д.`;
  return `${lat}, ${lon}`;
}

function stationGroups(stations) {
  const groups = new Map();
  for (const station of stations) {
    const point = location(station);
    if (!point) continue;
    const area = station.location_precision === 'area';
    const key = `${area}:${point.latitude},${point.longitude}`;
    if (!groups.has(key)) groups.set(key, {point, area, stations: []});
    groups.get(key).stations.push(station);
  }
  return [...groups.values()];
}

function createMap(viewport, onPick, onSelect) {
  const canvas = viewport.querySelector('canvas');
  const overlay = viewport.querySelector('.station-map-markers');
  const scaleLine = viewport.querySelector('.station-map-scale');
  const frame = viewport.querySelector('.station-map-frame');
  const modes = [...viewport.querySelectorAll('[data-map-mode]')];
  const controls = viewport.querySelectorAll('.station-map-modes, .station-map-tools');
  const context = canvas.getContext('2d');
  const bound = (value, low, high) => Math.min(Math.max(value, low), high);
  const worldScale = () => Math.min(viewport.clientWidth / 370, viewport.clientHeight / 190);
  const cosine = latitude => Math.max(0.25, Math.cos(latitude * degrees));
  let center = reserve;
  let scale = initialScale;
  let width = 0;
  let height = 0;
  let pendingFrame;
  let markers = [];
  let draft = null;
  let mode = 'select';
  let picking = false;
  let pointer = null;

  const project = (longitude, latitude) => [
    width / 2 + (longitude - center.longitude) * scale * cosine(center.latitude),
    height / 2 - (latitude - center.latitude) * scale,
  ];
  const unproject = (x, y) => ({
    longitude: bound(center.longitude + (x - width / 2) / (scale * cosine(center.latitude)), -180, 180),
    latitude: bound(center.latitude - (y - height / 2) / scale, -90, 90),
  });

  function setCenter(latitude, longitude) {
    center = {latitude: bound(latitude, -90, 90), longitude: bound(longitude, -180, 180)};
  }

  function trace(shapes, close = false) {
    context.beginPath();
    for (const shape of shapes) {
      if (shape[2] < west || shape[0] > east || shape[3] < south || shape[1] > north) continue;
      const points = shape[4];
      context.moveTo(...project(points[0], points[1]));
      for (let i = 2; i < points.length; i += 2) context.lineTo(...project(points[i], points[i + 1]));
      if (close) context.closePath();
    }
  }

  let west, east, south, north;
  function draw() {
    pendingFrame = null;
    width = viewport.clientWidth;
    height = viewport.clientHeight;
    if (!width || !height) return;
    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
    const pixelWidth = Math.round(width * pixelRatio);
    const pixelHeight = Math.round(height * pixelRatio);
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    context.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0);
    context.fillStyle = '#d7e8ea';
    context.fillRect(0, 0, width, height);
    ({longitude: west, latitude: north} = unproject(0, 0));
    ({longitude: east, latitude: south} = unproject(width, height));

    trace(land, true);
    context.fillStyle = '#edf0de';
    context.fill('evenodd');
    context.strokeStyle = '#aabfb4';
    context.lineWidth = 0.75;
    context.stroke();

    const cell = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 45, 90]
      .find(value => value * scale * cosine(center.latitude) >= 48) || 90;
    context.beginPath();
    for (let lon = Math.ceil(west / cell) * cell; lon <= east; lon += cell) {
      context.moveTo(...project(lon, north));
      context.lineTo(...project(lon, south));
    }
    for (let lat = Math.ceil(south / cell) * cell; lat <= north; lat += cell) {
      context.moveTo(...project(west, lat));
      context.lineTo(...project(east, lat));
    }
    context.strokeStyle = '#5d7a661f';
    context.stroke();

    if (scale >= 10) {
      trace(rivers);
      context.strokeStyle = '#94bfc8';
      context.lineWidth = 1.1;
      context.stroke();
      trace(lakes, true);
      context.fillStyle = '#c3dfe2';
      context.fill('evenodd');
      context.strokeStyle = '#9fbfc1';
      context.lineWidth = 0.7;
      context.stroke();
    }
    if (scale >= 75) {
      trace(roads);
      context.strokeStyle = '#faf8ef';
      context.lineWidth = 3;
      context.stroke();
      context.strokeStyle = '#c5ad88';
      context.lineWidth = 1.1;
      context.stroke();
    }

    const occupied = Array.from(controls, control => [control.offsetLeft - 6, control.offsetTop - 6,
      control.offsetLeft + control.offsetWidth + 6, control.offsetTop + control.offsetHeight + 6]);
    const font = getComputedStyle(viewport).fontFamily;
    context.textBaseline = 'middle';
    for (const marker of markers) {
      const [x, y] = project(marker.point.longitude, marker.point.latitude);
      marker.element.hidden = x < 0 || x > width || y < 0 || y > height;
      marker.element.style.left = `${x}px`;
      marker.element.style.top = `${y}px`;
      if (marker.element.hidden) continue;
      occupied.push([x - 31, y - 25, x + 31, y + 25]);
      if (marker.area && scale >= 180) {
        const label = 'Район заповедника';
        context.font = `800 11px ${font}`;
        const labelWidth = context.measureText(label).width;
        const labelX = x + 31 + labelWidth < width - 8 ? x + 31 : x - 31 - labelWidth;
        if (labelX >= 8) {
          context.strokeStyle = '#f1f4e4';
          context.lineWidth = 3;
          context.strokeText(label, labelX, y);
          context.fillStyle = '#20563b';
          context.fillText(label, labelX, y);
          occupied.push([labelX - 3, y - 9, labelX + labelWidth + 3, y + 9]);
        }
      }
    }

    context.font = `600 11px ${font}`;
    for (const [name, longitude, latitude, minScale] of places) {
      if (scale < minScale || longitude < west || longitude > east || latitude < south || latitude > north) continue;
      const [x, y] = project(longitude, latitude);
      const label = [x - 4, y - 8, x + context.measureText(name).width + 10, y + 8];
      if (label[0] < 7 || label[2] > width - 8 || label[1] < 7 || label[3] > height - 7) continue;
      if (occupied.some(a => a[0] < label[2] && a[2] > label[0] && a[1] < label[3] && a[3] > label[1])) continue;
      occupied.push(label);
      context.fillStyle = '#56695b';
      context.beginPath();
      context.arc(x, y, 2.1, 0, Math.PI * 2);
      context.fill();
      context.strokeStyle = '#f1f4e4';
      context.lineWidth = 2.5;
      context.strokeText(name, x + 6, y);
      context.fillText(name, x + 6, y);
    }

    if (draft) {
      const [x, y] = project(draft.longitude, draft.latitude);
      context.beginPath();
      context.arc(x, y, 8, 0, Math.PI * 2);
      context.lineWidth = 3;
      context.strokeStyle = '#fff';
      context.stroke();
      context.lineWidth = 1.5;
      context.strokeStyle = '#25583f';
      context.stroke();
      context.fillStyle = '#cde425';
      context.fill();
    }

    const units = 111.195 * Math.cos(center.latitude * degrees) / (scale * cosine(center.latitude));
    const distance = [20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10, 5, 2, 1, 0.5]
      .find(value => value / units <= 92);
    scaleLine.hidden = !distance || units < 0.001;
    if (!scaleLine.hidden) {
      scaleLine.textContent = distance < 1 ? `${Math.round(distance * 1000)} м` : `${distance} км`;
      scaleLine.style.width = `${distance / units}px`;
    }
  }

  function redraw() {
    pendingFrame ??= requestAnimationFrame(draw);
  }

  function zoom(factor, x = width / 2, y = height / 2) {
    const point = unproject(x, y);
    scale = bound(scale * factor, worldScale(), maxScale);
    const latitude = bound(point.latitude + (y - height / 2) / scale, -90, 90);
    setCenter(latitude, point.longitude - (x - width / 2) / (scale * cosine(latitude)));
    redraw();
  }

  function pan(dx, dy) {
    const {longitude, latitude} = unproject(width / 2 - dx, height / 2 - dy);
    setCenter(latitude, longitude);
    redraw();
  }

  function focus(point) {
    center = point;
    scale = initialScale;
    redraw();
  }

  function fit(points) {
    const box = viewport.getBoundingClientRect();
    if (!points.length || !box.width || !box.height) return;
    const latitudes = points.map(p => p.latitude);
    const longitudes = points.map(p => p.longitude);
    const latRange = Math.max(...latitudes) - Math.min(...latitudes);
    const lonRange = Math.max(...longitudes) - Math.min(...longitudes);
    setCenter(Math.min(...latitudes) + latRange / 2, Math.min(...longitudes) + lonRange / 2);
    scale = bound(Math.min(initialScale, (box.height - 90) / (latRange || 1e-6),
      (box.width - 80) / ((lonRange || 1e-6) * cosine(center.latitude))), worldScale(), maxScale);
    redraw();
  }

  const position = event => {
    const rect = viewport.getBoundingClientRect();
    return [event.clientX - rect.left - viewport.clientLeft, event.clientY - rect.top - viewport.clientTop];
  };

  function selectionBounds(gesture, point) {
    const [x, y] = point;
    return [bound(Math.min(gesture.startX, x), 0, width), bound(Math.min(gesture.startY, y), 0, height),
      bound(Math.max(gesture.startX, x), 0, width), bound(Math.max(gesture.startY, y), 0, height)];
  }

  function cancelGesture() {
    if (!pointer) return false;
    const {id} = pointer;
    pointer = null;
    frame.hidden = true;
    viewport.classList.remove('is-panning');
    if (canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
    return true;
  }

  function setMode(next) {
    cancelGesture();
    mode = next;
    viewport.classList.toggle('is-pan-mode', mode === 'pan');
    for (const button of modes) button.setAttribute('aria-pressed', button.dataset.mapMode === mode);
  }

  for (const button of modes) button.addEventListener('click', () => setMode(button.dataset.mapMode));
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !event.isPrimary || pointer) return;
    const [startX, startY] = position(event);
    pointer = {id: event.pointerId, startX, startY, x: startX, y: startY, moved: false,
      kind: picking ? 'coordinates' : mode, additive: event.ctrlKey || event.metaKey};
    viewport.focus({preventScroll: true});
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', event => {
    if (pointer?.id !== event.pointerId) return;
    const [x, y] = position(event);
    const dx = x - pointer.x;
    const dy = y - pointer.y;
    if (!pointer.moved && Math.hypot(dx, dy) < 5) return;
    pointer.moved = true;
    pointer.x = x;
    pointer.y = y;
    if (pointer.kind === 'select') {
      const [left, top, right, bottom] = selectionBounds(pointer, [x, y]);
      frame.style.left = `${left}px`;
      frame.style.top = `${top}px`;
      frame.style.width = `${right - left}px`;
      frame.style.height = `${bottom - top}px`;
      frame.hidden = false;
    } else {
      viewport.classList.add('is-panning');
      pan(dx, dy);
    }
  });
  canvas.addEventListener('pointerup', event => {
    if (pointer?.id !== event.pointerId) return;
    const gesture = pointer;
    cancelGesture();
    if (gesture.kind === 'coordinates' && !gesture.moved) onPick(unproject(...position(event)));
    if (gesture.kind !== 'select' || !gesture.moved) return;
    const [left, top, right, bottom] = selectionBounds(gesture, position(event));
    const included = markers.filter(({point}) => {
      const [x, y] = project(point.longitude, point.latitude);
      return x >= left && x <= right && y >= top && y <= bottom;
    });
    onSelect(included.flatMap(group => group.stations), gesture.additive || event.ctrlKey || event.metaKey);
  });
  for (const type of ['pointercancel', 'lostpointercapture']) {
    canvas.addEventListener(type, event => {
      if (pointer?.id === event.pointerId) cancelGesture();
    });
  }
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    if (!pointer) zoom(event.deltaY < 0 ? 1.25 : 0.8, ...position(event));
  }, {passive: false});
  canvas.addEventListener('contextmenu', event => {
    if (event.ctrlKey && pointer?.kind === 'select') event.preventDefault();
  });
  viewport.addEventListener('keydown', event => {
    if (event.target !== viewport) return;
    const movement = {ArrowLeft: [44, 0], ArrowRight: [-44, 0], ArrowUp: [0, 44], ArrowDown: [0, -44]};
    if (event.key === 'Escape' && cancelGesture()) event.stopPropagation();
    else if (movement[event.key]) pan(...movement[event.key]);
    else if (['+', '='].includes(event.key)) zoom(1.5);
    else if (event.key === '-') zoom(1 / 1.5);
    else if (picking && event.key === 'Enter') onPick(center);
    else return;
    event.preventDefault();
  });

  const observer = new ResizeObserver(redraw);
  observer.observe(viewport);
  return {
    zoom, focus, fit, cancelGesture,
    setMarkers(next) {
      markers = next;
      for (const marker of markers) marker.element.disabled = picking;
      overlay.replaceChildren(...markers.map(marker => marker.element));
      redraw();
    },
    setDraft(point) {
      draft = point;
      redraw();
    },
    pickCoordinates(active) {
      cancelGesture();
      picking = active;
      for (const button of modes) button.disabled = active;
      for (const marker of markers) marker.element.disabled = active;
      viewport.classList.toggle('is-picking', active);
      if (active) viewport.focus();
    },
    destroy() {
      cancelGesture();
      observer.disconnect();
      cancelAnimationFrame(pendingFrame);
    },
  };
}

function mapContents() {
  const hintId = `station-map-shortcut-${++mapId}`;
  return `<section class="station-map-column" aria-label="Географическая карта">
  <div class="station-map-viewport" tabindex="0" role="group" aria-label="Карта станций. Стрелки перемещают карту, клавиши плюс и минус меняют масштаб" aria-describedby="${hintId}">
    <canvas aria-hidden="true"></canvas>
    <div class="station-map-frame" aria-hidden="true" hidden></div>
    <div class="station-map-markers"></div>
    <div class="station-map-modes" role="group" aria-label="Режим карты">
      <button class="btn line" type="button" data-map-mode="select" aria-label="Выделение рамкой" aria-pressed="true">${icon('select')} <span>Выделение рамкой</span></button>
      <button class="btn line" type="button" data-map-mode="pan" aria-label="Перемещение" aria-pressed="false">${icon('pan')} <span>Перемещение</span></button>
    </div>
    <div class="station-map-tools" aria-label="Управление картой">
      <button class="icon-btn" type="button" data-zoom-in title="Приблизить" aria-label="Приблизить">${icon('plus')}</button>
      <button class="icon-btn" type="button" data-zoom-out title="Отдалить" aria-label="Отдалить">${icon('minus')}</button>
      <button class="icon-btn" type="button" data-fit title="Показать все станции" aria-label="Показать все станции">${icon('fit')}</button>
    </div>
    <div class="station-map-scale" aria-hidden="true"></div>
    <p class="station-map-instruction" hidden>Укажите точку на карте или нажмите Enter для точки в центре.</p>
  </div>
  <div class="station-map-source">
    <span>Обзорная карта · <a href="https://www.naturalearthdata.com/about/terms-of-use/" target="_blank" rel="noreferrer">Natural Earth</a>, <a href="https://www.geonames.org/export/" target="_blank" rel="noreferrer">GeoNames</a></span>
    <span data-area-note>Общий маркер показывает район заповедника.</span>
  </div>
  <p id="${hintId}" class="station-map-shortcut">Удерживайте Ctrl или ⌘, чтобы добавить станции рамкой.</p>
</section>`;
}

function stationListContents(action = '') {
  return `<section data-picker-list aria-label="Список метеостанций">
    <div class="station-picker-list-head"><h3>Станции архива</h3>${action}</div>
    <div class="station-picker-list" data-list></div>
  </section>`;
}

function createStationSelection(root, {onPick, onChange, emptyText}) {
  const get = selector => root.querySelector(selector);
  const viewport = get('.station-map-viewport');
  const fitButton = get('[data-fit]');
  let stations = [];
  let selected = new Set();
  let items = [];
  let markers = [];
  let catalogKey;
  let positioned = false;
  const map = createMap(viewport, onPick, (included, additive) => {
    const next = new Set(additive ? selected : []);
    for (const station of included) next.add(station.id);
    if (next.size === selected.size && [...next].every(id => selected.has(id))) return;
    selected = next;
    refreshSelection();
    onChange([...selected]);
  });

  function fitMarkers() {
    map.fit(markers.map(group => group.point));
  }

  get('[data-zoom-in]').addEventListener('click', () => map.zoom(1.5));
  get('[data-zoom-out]').addEventListener('click', () => map.zoom(1 / 1.5));
  fitButton.addEventListener('click', fitMarkers);

  function refreshSelection() {
    for (const {station, input} of items) input.checked = selected.has(station.id);
    for (const group of markers) {
      const count = group.stations.filter(station => selected.has(station.id)).length;
      group.element.setAttribute('aria-pressed', count === group.stations.length ? 'true' : count ? 'mixed' : 'false');
    }
  }

  function rebuild() {
    items = stations.map(station => {
      const point = location(station);
      const row = document.createElement('div');
      row.className = 'station-picker-row';
      row.innerHTML = `<label><input type="checkbox"><span class="station-row-dot" aria-hidden="true"></span><span><strong></strong><small></small></span></label>`;
      row.querySelector('strong').textContent = station.label;
      row.querySelector('.station-row-dot').style.backgroundColor = station.color || '#2b7650';
      const note = station.location_precision === 'area' ? 'Район заповедника' : point ? coordinates(point) : 'Координаты отсутствуют';
      row.querySelector('small').textContent = note;
      const input = row.querySelector('input');
      input.addEventListener('change', () => {
        if (input.checked) selected.add(station.id);
        else selected.delete(station.id);
        refreshSelection();
        onChange([...selected]);
      });
      if (point) {
        const locate = document.createElement('button');
        locate.className = 'icon-btn station-row-locate';
        locate.type = 'button';
        locate.title = 'Показать на карте';
        locate.setAttribute('aria-label', `Показать на карте: ${station.label}`);
        locate.innerHTML = icon('locate');
        locate.addEventListener('click', () => {
          map.focus(point);
          viewport.focus();
        });
        row.append(locate);
      }
      return {station, row, input};
    });

    const list = get('[data-list]');
    list.replaceChildren(...items.map(item => item.row));
    if (!items.length) list.textContent = emptyText;
    markers = stationGroups(stations).map(group => {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = `station-map-marker${group.area ? ' station-map-marker-area' : ''}`;
      const names = group.stations.map(station => station.label).join(', ');
      element.title = group.area ? `Район заповедника · ${names}` : names;
      element.setAttribute('aria-label', element.title);
      if (group.stations.length > 1 || group.area) element.textContent = group.stations.length;
      else element.innerHTML = icon('pin');
      element.addEventListener('click', () => {
        const select = group.stations.some(station => !selected.has(station.id));
        for (const station of group.stations) {
          if (select) selected.add(station.id);
          else selected.delete(station.id);
        }
        refreshSelection();
        onChange([...selected]);
      });
      return {...group, element};
    });
    get('[data-area-note]').hidden = !markers.some(group => group.area);
    fitButton.disabled = !markers.length;
    map.setMarkers(markers);
    if (!positioned && markers.length) {
      positioned = true;
      if (!markers.some(group => group.area)) fitMarkers();
    }
  }

  return {
    map,
    ids: () => [...selected],
    focusCheckbox(id) {
      items.find(item => item.station.id === id)?.input.focus();
    },
    update(catalog, ids) {
      stations = catalog;
      const stationIds = new Set(stations.map(station => station.id));
      selected = new Set(ids.filter(id => stationIds.has(id)));
      const nextKey = JSON.stringify(stations.map(({id, label, color, latitude, longitude, location_precision}) =>
        [id, label, color, latitude, longitude, location_precision]));
      if (nextKey !== catalogKey) {
        catalogKey = nextKey;
        rebuild();
      }
      refreshSelection();
    },
  };
}

export function mountStationOverview(container, {onChange = () => {}, onExpand = () => {}} = {}) {
  container.classList.add('station-overview');
  container.innerHTML = `<div class="station-overview-heading">
    <h2>Карта метеостанций</h2>
    <button class="btn line" type="button" data-expand-map aria-haspopup="dialog">${icon('fit')} Открыть карту</button>
  </div>
  <div class="station-overview-body">
    ${mapContents()}
    ${stationListContents()}
  </div>`;
  const selection = createStationSelection(container, {onChange, emptyText: 'Станции не найдены.'});
  container.querySelector('[data-expand-map]').addEventListener('click', onExpand);
  selection.update([], []);
  return {
    update: selection.update,
    destroy() {
      selection.map.destroy();
      container.replaceChildren();
      container.classList.remove('station-overview');
    },
  };
}

function dialogContents() {
  return `
    <div class="station-picker-heading">
      <div>
        <h2 id="station-picker-title">Метеостанции</h2>
        <p id="station-picker-description">Выберите станции в списке или выделите их рамкой на карте.</p>
      </div>
      <button class="icon-btn" type="button" data-close aria-label="Закрыть" autofocus>${icon('close')}</button>
    </div>
    <div class="station-picker-body">
      ${mapContents()}
      <div class="station-picker-side">
        ${stationListContents(`<button class="btn line" type="button" data-add>${icon('plus')} Добавить</button>`)}
        <form data-station-form hidden>
          <div class="station-picker-list-head"><h3>Паспорт станции</h3><button class="btn line" type="button" data-back>К списку</button></div>
          <fieldset>
            <label class="field"><span>Название станции</span><input name="label" type="text" required maxlength="100" placeholder="Например, Лесной кордон"></label>
            <label class="field"><span>Формат данных</span><select name="profile"><option value="kr1">Сокол М1 (Кр1 / Кр2)</option><option value="ame60">АМЕ-60</option></select></label>
            <div class="station-form-coordinates">
              <label class="field"><span>Широта</span><input name="latitude" type="number" inputmode="decimal" min="-90" max="90" step="any" placeholder="±90"></label>
              <label class="field"><span>Долгота</span><input name="longitude" type="number" inputmode="decimal" min="-180" max="180" step="any" placeholder="±180"></label>
            </div>
            <button class="btn line" type="button" data-pick aria-pressed="false">${icon('pin')} Указать на карте</button>
            <details>
              <summary>Дополнительные поля</summary>
              <label class="field"><span>Код станции</span><input name="id" type="text" maxlength="56" pattern="[a-z][a-z0-9_\\-]*" placeholder="Сформируется автоматически" title="Строчные латинские буквы, цифры, дефис и подчёркивание"></label>
              <label class="field"><span>Шаг сырых наблюдений</span><select name="slot_minutes"><option value="">По формату</option><option value="1">1 минута</option><option value="60">1 час</option></select></label>
              <label class="field"><span>Минимум сроков за сутки</span><input name="minimum_slots" type="number" min="1" max="1440" step="1" placeholder="По формату"></label>
              <div class="station-form-params"><div>Показатели станции</div><div data-params></div></div>
            </details>
            <p class="station-form-error" role="alert" hidden></p>
            <button class="btn primary" type="submit" data-save>Зарегистрировать станцию</button>
          </fieldset>
        </form>
      </div>
    </div>
    <div class="station-picker-footer">
      <span data-selection-count aria-live="polite"></span>
      <div><button class="btn line" type="button" data-close>Отмена</button><button class="btn primary" type="button" data-apply>Применить</button></div>
    </div>`;
}

export async function pickStations(stations, selectedIds = []) {
  const before = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'station-picker';
  dialog.setAttribute('aria-labelledby', 'station-picker-title');
  dialog.setAttribute('aria-describedby', 'station-picker-description');
  dialog.innerHTML = dialogContents();
  document.body.append(dialog);
  const get = selector => dialog.querySelector(selector);
  const form = get('[data-station-form]');
  const fields = form.elements;
  const apply = get('[data-apply]');
  const add = get('[data-add]');
  const pick = get('[data-pick]');
  const error = get('.station-form-error');
  const instruction = get('.station-map-instruction');
  let editing = false;
  let picking = false;
  let saving = false;
  let result = null;
  const selection = createStationSelection(dialog, {
    emptyText: 'Добавьте первую станцию.',
    onChange: refreshFooter,
    onPick(point) {
      fields.latitude.value = point.latitude.toFixed(5);
      fields.longitude.value = point.longitude.toFixed(5);
      setPicking(false);
      previewCoordinates();
      pick.focus();
    },
  });
  const {map} = selection;

  function refreshFooter(ids = selection.ids()) {
    get('[data-selection-count]').textContent = `Выбрано ${ids.length} из ${stations.length}`;
    apply.disabled = !ids.length || editing;
  }

  function setPicking(active) {
    picking = active;
    instruction.hidden = !active;
    pick.setAttribute('aria-pressed', active);
    map.pickCoordinates(active);
  }

  function formPoint() {
    const latitude = fields.latitude.valueAsNumber;
    const longitude = fields.longitude.valueAsNumber;
    return location({latitude, longitude});
  }

  function previewCoordinates() {
    fields.latitude.setCustomValidity('');
    fields.longitude.setCustomValidity('');
    map.setDraft(formPoint());
  }

  function applyProfile() {
    const params = get('[data-params]');
    params.replaceChildren(...[...commonParams, ...profileParams[fields.profile.value]].map(([id, label]) => {
      const option = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'params';
      input.value = id;
      input.checked = true;
      option.append(input, document.createTextNode(label));
      return option;
    }));
  }

  function showForm(active) {
    editing = active;
    form.hidden = !active;
    get('[data-picker-list]').hidden = active;
    setPicking(false);
    if (active) {
      form.reset();
      form.querySelector('details').open = false;
      applyProfile();
      error.hidden = true;
      fields.label.focus();
    } else {
      add.focus();
    }
    map.setDraft(null);
    refreshFooter();
  }

  add.addEventListener('click', () => showForm(true));
  get('[data-back]').addEventListener('click', () => showForm(false));
  pick.addEventListener('click', () => setPicking(!picking));
  fields.profile.addEventListener('change', applyProfile);
  fields.latitude.addEventListener('input', previewCoordinates);
  fields.longitude.addEventListener('input', previewCoordinates);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    error.hidden = true;
    const point = formPoint();
    if ((fields.latitude.value || fields.longitude.value) && !point) {
      const missing = fields.latitude.value ? fields.longitude : fields.latitude;
      missing.setCustomValidity('Укажите широту и долготу.');
      missing.reportValidity();
      return;
    }
    const data = new FormData(form);
    const params = data.getAll('params');
    if (!data.get('label').trim() || !params.length) {
      error.textContent = params.length ? 'Введите название станции.' : 'Укажите хотя бы один показатель.';
      error.hidden = false;
      if (params.length) fields.label.focus();
      else form.querySelector('details').open = true;
      return;
    }
    const passport = {label: data.get('label').trim(), profile: data.get('profile'), params, ...point};
    if (data.get('id')) passport.id = data.get('id');
    for (const key of ['minimum_slots', 'slot_minutes']) {
      if (data.get(key)) passport[key] = Number(data.get(key));
    }
    saving = true;
    setPicking(false);
    form.querySelector('fieldset').disabled = true;
    get('[data-back]').disabled = true;
    for (const button of dialog.querySelectorAll('[data-close]')) button.disabled = true;
    const save = get('[data-save]');
    save.textContent = 'Регистрация…';
    try {
      const response = await fetch('/api/stations', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(passport),
      });
      const body = await response.json();
      if (!response.ok || !body.station?.id) throw new Error(body.error || body.message || 'Станцию не удалось зарегистрировать.');
      stations.push(body.station);
      selection.update(stations, [...selection.ids(), body.station.id]);
      showForm(false);
      selection.focusCheckbox(body.station.id);
      const point = location(body.station);
      if (point) map.focus(point);
    } catch (failure) {
      error.textContent = failure instanceof TypeError || failure instanceof SyntaxError
        ? 'Каталог недоступен. Проверьте связь с сервером и повторите попытку.' : failure.message;
      error.hidden = false;
    } finally {
      saving = false;
      form.querySelector('fieldset').disabled = false;
      get('[data-back]').disabled = false;
      for (const button of dialog.querySelectorAll('[data-close]')) button.disabled = false;
      save.textContent = 'Зарегистрировать станцию';
    }
  });

  for (const button of dialog.querySelectorAll('[data-close]')) button.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => {
    if (map.cancelGesture() || saving) event.preventDefault();
  });
  apply.addEventListener('click', () => {
    result = selection.ids();
    dialog.close();
  });

  const finished = new Promise(resolve => dialog.addEventListener('close', () => {
    map.destroy();
    dialog.remove();
    before?.focus();
    resolve(result);
  }, {once: true}));
  dialog.showModal();
  selection.update(stations, selectedIds);
  refreshFooter();
  return finished;
}
