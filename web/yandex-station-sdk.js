const reserve = {latitude: 56.52, longitude: 32.87};
const loads = new Map();
let namespaceIndex = 0;
let mountedCount = 0;

function loadYandex(apiKey) {
  if (loads.has(apiKey)) return loads.get(apiKey);

  const namespace = 'meteoYandexMaps' + (++namespaceIndex);
  const onerror = namespace + 'Error';
  const url = new URL('https://api-maps.yandex.ru/2.1/');
  url.search = new URLSearchParams({apikey: apiKey, lang: 'ru_RU', ns: namespace, onerror});
  const script = document.createElement('script');
  script.src = url;
  script.async = true;

  const pending = new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      script.onload = null;
      script.onerror = null;
      delete window[onerror];
    };
    const fail = () => {
      cleanup();
      script.remove();
      delete window[namespace];
      reject(new Error('Не удалось загрузить Яндекс Карты. Проверьте ключ и доступ к сервису.'));
    };
    const timer = setTimeout(fail, 8000);
    window[onerror] = script.onerror = fail;
    script.onload = () => {
      const api = window[namespace];
      if (typeof api?.ready !== 'function') return fail();
      try {
        api.ready(() => {
          if (typeof api.Map !== 'function') return fail();
          cleanup();
          resolve(api);
        }, fail);
      } catch {
        fail();
      }
    };
    document.head.append(script);
  }).catch(error => {
    loads.delete(apiKey);
    throw error;
  });

  loads.set(apiKey, pending);
  return pending;
}

function coordinates({latitude, longitude}) {
  return [latitude, longitude];
}

function isPoint(point) {
  return point && Number.isFinite(point.latitude) && Math.abs(point.latitude) <= 90 &&
    Number.isFinite(point.longitude) && Math.abs(point.longitude) <= 180;
}

function buildView(host) {
  const root = document.createElement('div');
  const hint = 'yandex-station-map-hint-' + (++mountedCount);
  root.className = 'yandex-station-map';
  root.innerHTML = `
    <style>
      .yandex-station-map { display: flex; flex-direction: column; height: 100%; min-height: 300px; }
      .yandex-station-toolbar { display: flex; flex-wrap: wrap; gap: 4px; padding: 5px; background: var(--paper, #fff); }
      .yandex-station-toolbar button { min-height: 34px; padding: 4px 8px; border: 1px solid #c5d4c8;
        border-radius: 5px; color: #20563b; background: #fff; font-size: 12px; font-weight: 650; }
      .yandex-station-toolbar button[aria-pressed="true"] { color: #fff; background: #20563b; border-color: #20563b; }
      .yandex-station-toolbar button:disabled { opacity: .55; }
      .yandex-station-hint { padding: 0 6px 5px; background: var(--paper, #fff); color: var(--muted, #526256);
        font-size: 11px; line-height: 1.35; }
      .yandex-station-view { flex: 1; position: relative; isolation: isolate; min-height: 230px; background: #e3ebe1; }
      .yandex-station-view:focus-visible { outline: 2px solid #20563b; outline-offset: -2px; }
      .yandex-station-base { position: absolute; z-index: 0; inset: 0; }
      /* Keep space for provider logos and attribution links. */
      .yandex-station-gesture, .yandex-station-markers { position: absolute; inset: 0 0 44px; overflow: hidden; }
      .yandex-station-gesture { touch-action: none; cursor: crosshair; }
      .yandex-station-markers { pointer-events: none; }
      .yandex-station-frame { position: absolute; background: #28794b30; border: 1px solid #20563b; }
      .yandex-station-marker { position: absolute; pointer-events: auto; touch-action: none;
        display: grid; place-items: center; width: 34px; height: 34px; padding: 0;
        transform: translate(-50%, -50%); border: 2px solid white; border-radius: 50%;
        box-shadow: 0 0 0 2px var(--selection-ring, transparent), 0 1px 5px #172c2380;
        background: var(--terrain-fill, #58675f); color: white; font-size: 13px; font-weight: 800; }
      .yandex-station-marker[data-area="true"] { border-style: dashed; width: 42px; height: 42px; }
      .yandex-station-marker[aria-pressed="true"] { border-color: #d7e950; --selection-ring: #20563b; }
      .yandex-station-marker[aria-pressed="mixed"] { border-color: #e5f0e5; --selection-ring: #61896f; }
      .yandex-station-marker:focus-visible { z-index: 1; outline: 2px solid #20563b; outline-offset: 2px; }
      .yandex-station-marker:disabled { pointer-events: none; }
      :is(.yandex-station-gesture, .yandex-station-frame, .yandex-station-marker)[hidden] { display: none !important; }
    </style>
    <div class="yandex-station-toolbar" role="group" aria-label="Управление Яндекс Картой">
      <button type="button" data-select aria-pressed="true">Выделение рамкой</button>
      <button type="button" data-pan aria-pressed="false">Перемещение</button>
      <button type="button" data-zoom-in aria-label="Приблизить">+</button>
      <button type="button" data-zoom-out aria-label="Отдалить">−</button>
    </div>
    <p class="yandex-station-hint" id="${hint}" aria-live="polite"></p>
    <div class="yandex-station-view" role="group" tabindex="0"
      aria-label="Станции на Яндекс Карте. Стрелки перемещают карту, плюс и минус меняют масштаб."
      aria-describedby="${hint}">
      <div class="yandex-station-base"></div>
      <div class="yandex-station-gesture" aria-hidden="true"></div>
      <div class="yandex-station-markers">
        <div class="yandex-station-frame" aria-hidden="true" hidden></div>
      </div>
    </div>`;
  host.replaceChildren(root);
  return root;
}

function wireMap(root, map, {onToggleGroup, onSelectIds, onPick}) {
  const query = selector => root.querySelector(selector);
  const viewport = query('.yandex-station-view');
  const overlay = query('.yandex-station-markers');
  const surface = query('.yandex-station-gesture');
  const frame = query('.yandex-station-frame');
  const hint = query('.yandex-station-hint');
  const select = query('[data-select]');
  const pan = query('[data-pan]');
  const abort = new AbortController();
  const markers = new Map();
  let selecting = true;
  let picking = false;
  let pointer = null;
  let suppressClick = false;
  let animation;
  let destroyed = false;

  function listen(element, type, listener, capture = false) {
    element.addEventListener(type, listener, {capture, signal: abort.signal});
  }

  function locate() {
    const bounds = viewport.getBoundingClientRect();
    const pageLeft = bounds.left + window.scrollX;
    const pageTop = bounds.top + window.scrollY;
    const projection = map.options.get('projection');
    const zoom = map.getZoom();
    return {
      pointToLocal(point) {
        const globalPixels = projection.toGlobalPixels(coordinates(point), zoom);
        const [pageX, pageY] = map.converter.globalToPage(globalPixels);
        return [pageX - pageLeft, pageY - pageTop];
      },
      localToPoint(x, y) {
        const globalPixels = map.converter.pageToGlobal([x + pageLeft, y + pageTop]);
        const [latitude, longitude] = projection.fromGlobalPixels(globalPixels, zoom);
        return {latitude, longitude};
      },
    };
  }

  function redraw() {
    animation = undefined;
    if (!overlay.clientWidth) return;
    const {pointToLocal} = locate();
    for (const {button, group} of markers.values()) {
      const [x, y] = pointToLocal(group.point);
      button.hidden = x < 0 || x > overlay.clientWidth || y < 0 || y > overlay.clientHeight;
      button.style.left = x + 'px';
      button.style.top = y + 'px';
    }
  }

  function scheduleRedraw() {
    animation ??= requestAnimationFrame(redraw);
  }

  function cancelGesture() {
    if (!pointer) return false;
    const {id} = pointer;
    pointer = null;
    frame.hidden = true;
    if (viewport.hasPointerCapture(id)) viewport.releasePointerCapture(id);
    return true;
  }

  function refreshMode() {
    cancelGesture();
    select.setAttribute('aria-pressed', String(selecting && !picking));
    pan.setAttribute('aria-pressed', String(!selecting && !picking));
    select.disabled = pan.disabled = picking;
    surface.hidden = !selecting && !picking;
    for (const {button} of markers.values()) button.disabled = picking;
    hint.textContent = picking ? 'Укажите точку на карте или нажмите Enter, чтобы выбрать центр.'
      : selecting ? 'Проведите рамку вокруг станций. Ctrl или ⌘ добавляет их к выбору.'
      : 'Перетаскивайте карту. Удерживайте Shift, чтобы выделить станции рамкой.';
  }

  function changeZoom(delta) {
    if (pointer) return;
    const zoom = Math.max(2, Math.min(19, Math.round(map.getZoom()) + delta));
    map.setZoom(zoom).then(null, () => {});
  }

  function position(event) {
    const bounds = viewport.getBoundingClientRect();
    return [event.clientX - bounds.left, event.clientY - bounds.top];
  }

  function rectangle(gesture, x, y) {
    const endX = Math.max(0, Math.min(x, overlay.clientWidth));
    const endY = Math.max(0, Math.min(y, overlay.clientHeight));
    return {
      left: Math.min(gesture.x, endX), top: Math.min(gesture.y, endY),
      right: Math.max(gesture.x, endX), bottom: Math.max(gesture.y, endY),
    };
  }

  function isAdditive(event) {
    return event.ctrlKey || event.metaKey || (selecting && event.shiftKey);
  }

  listen(viewport, 'pointerdown', event => {
    if (event.button !== 0 || !event.isPrimary || pointer) return;
    suppressClick = false;
    if (!selecting && !picking && !event.shiftKey) return;
    const [x, y] = position(event);
    if (x < 0 || y < 0 || x > overlay.clientWidth || y > overlay.clientHeight) return;
    const target = event.target.closest('.yandex-station-marker');
    pointer = {id: event.pointerId, x, y, target, moved: false, additive: isAdditive(event)};
    event.preventDefault();
    event.stopPropagation();
    (target ?? viewport).focus({preventScroll: true});
    viewport.setPointerCapture(pointer.id);
  }, true);

  listen(viewport, 'pointermove', event => {
    if (pointer?.id !== event.pointerId) return;
    const [x, y] = position(event);
    if (Math.hypot(x - pointer.x, y - pointer.y) >= 5) pointer.moved = true;
    if (!pointer.moved || picking) return;
    const {left, top, right, bottom} = rectangle(pointer, x, y);
    Object.assign(frame.style, {left: left + 'px', top: top + 'px', width: (right - left) + 'px',
      height: (bottom - top) + 'px'});
    frame.hidden = false;
  });

  listen(viewport, 'pointerup', event => {
    if (pointer?.id !== event.pointerId) return;
    const gesture = pointer;
    const [x, y] = position(event);
    const moved = gesture.moved || Math.hypot(x - gesture.x, y - gesture.y) >= 5;
    suppressClick = true;
    cancelGesture();
    if (picking) {
      if (!moved) onPick(locate().localToPoint(x, y));
      return;
    }
    if (!moved) {
      gesture.target?.click();
      return;
    }
    const {left, top, right, bottom} = rectangle(gesture, x, y);
    const {pointToLocal} = locate();
    const ids = new Set();
    for (const {group} of markers.values()) {
      const [px, py] = pointToLocal(group.point);
      if (px >= left && px <= right && py >= top && py <= bottom) {
        for (const station of group.stations) ids.add(station.id);
      }
    }
    onSelectIds([...ids], Boolean(gesture.additive || isAdditive(event)));
  });

  const cancelPointer = event => {
    if (pointer?.id === event.pointerId) cancelGesture();
  };
  listen(viewport, 'pointercancel', cancelPointer);
  listen(viewport, 'lostpointercapture', cancelPointer);
  listen(viewport, 'click', event => {
    if (!suppressClick || event.detail === 0) return;
    suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  listen(viewport, 'contextmenu', event => {
    if (pointer || (event.ctrlKey && !surface.hidden && position(event)[1] <= overlay.clientHeight)) {
      event.preventDefault();
    }
  });

  const directions = {ArrowLeft: [-48, 0], ArrowRight: [48, 0], ArrowUp: [0, -48], ArrowDown: [0, 48]};
  listen(viewport, 'keydown', event => {
    if (event.key === 'Escape' && pointer) {
      suppressClick = true;
      cancelGesture();
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (event.target !== viewport || pointer) return;
    if (directions[event.key]) {
      const [dx, dy] = directions[event.key];
      const point = locate().localToPoint(viewport.clientWidth / 2 + dx, viewport.clientHeight / 2 + dy);
      map.setCenter(coordinates(point)).then(null, () => {});
    } else if (event.key === '+' || event.key === '=') changeZoom(1);
    else if (event.key === '-' || event.key === '−') changeZoom(-1);
    else if (event.key === 'Enter' && picking) {
      const [latitude, longitude] = map.getCenter();
      onPick({latitude, longitude});
    } else return;
    event.preventDefault();
  });

  function setSelectionMode(active) {
    if (destroyed) return;
    selecting = Boolean(active);
    refreshMode();
  }

  listen(select, 'click', () => setSelectionMode(true));
  listen(pan, 'click', () => setSelectionMode(false));
  listen(query('[data-zoom-in]'), 'click', () => changeZoom(1));
  listen(query('[data-zoom-out]'), 'click', () => changeZoom(-1));

  const mapEvents = ['boundschange', 'actiontickcomplete', 'sizechange'];
  map.events.add(mapEvents, scheduleRedraw);
  refreshMode();

  return {
    update(groups, selectedIds) {
      if (destroyed) return;
      const ids = selectedIds == null ? null : new Set(selectedIds);
      const retained = new Set();
      for (const group of groups) {
        if (!isPoint(group.point) || !group.stations?.length) continue;
        const key = JSON.stringify(group.stations.map(station => station.id));
        retained.add(key);
        let marker = markers.get(key);
        if (!marker) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'yandex-station-marker';
          button.hidden = true;
          marker = {button, group};
          listen(button, 'click', () => onToggleGroup(marker.group));
          markers.set(key, marker);
          overlay.append(button);
        }
        marker.group = group;
        const {button} = marker;
        const names = group.stations.map(station => station.label || station.id).join(', ');
        const count = group.stations.filter(station => ids ? ids.has(station.id) : station.selected).length;
        button.title = group.area ? 'Район заповедника · ' + names : names;
        button.setAttribute('aria-label', button.title);
        button.setAttribute('aria-pressed', count === 0 ? 'false'
          : count === group.stations.length ? 'true' : 'mixed');
        button.dataset.area = String(Boolean(group.area));
        button.textContent = group.area || group.stations.length > 1 ? group.stations.length : '•';
        button.disabled = picking;
        button.style.setProperty('--terrain-fill', group.element?.style.getPropertyValue('--terrain-fill') || '');
      }
      for (const [key, {button}] of markers) {
        if (!retained.has(key)) {
          button.remove();
          markers.delete(key);
        }
      }
      scheduleRedraw();
    },
    focus(point) {
      if (destroyed || !isPoint(point)) return;
      return map.setCenter(coordinates(point)).then(null, () => {});
    },
    setPicking(active) {
      if (destroyed) return;
      picking = Boolean(active);
      refreshMode();
      if (picking) viewport.focus({preventScroll: true});
    },
    setSelectionMode,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      cancelGesture();
      abort.abort();
      map.events.remove(mapEvents, scheduleRedraw);
      cancelAnimationFrame(animation);
      try {
        map.destroy();
      } finally {
        root.remove();
      }
    },
  };
}

export async function openYandexStationMap(element, {
  apiKey, groups = [], onToggleGroup = () => {}, onSelectIds = () => {}, onPick = () => {}, center = reserve,
}) {
  if (typeof apiKey !== 'string' || !apiKey.trim()) throw new Error('Требуется ключ JavaScript API Яндекс Карт.');
  const api = await loadYandex(apiKey.trim());
  if (!element.isConnected) throw new Error('Контейнер Яндекс Карты закрыт.');
  const root = buildView(element);
  let map;
  let controller;
  try {
    map = new api.Map(root.querySelector('.yandex-station-base'), {
      center: coordinates(isPoint(center) ? center : reserve),
      zoom: 10,
      controls: [],
      behaviors: ['drag', 'dblClickZoom', 'multiTouch'],
    }, {autoFitToViewport: 'always', yandexMapDisablePoiInteractivity: true});
    controller = wireMap(root, map, {onToggleGroup, onSelectIds, onPick});
    controller.update(groups);
    return controller;
  } catch (error) {
    if (controller) controller.destroy();
    else {
      map?.destroy();
      root.remove();
    }
    throw error;
  }
}
