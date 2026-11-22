// The public widget renders geographic points itself. Selection lives in the local map or SDK.
let configRequest;
const stationPlural = new Intl.PluralRules('ru');

function configuration() {
  if (!configRequest) {
    configRequest = fetch('/api/map-config').then(response => response.ok ? response.json() : {})
      .catch(() => { configRequest = undefined; return {}; });
  }
  return configRequest;
}

const coordinatePair = ({longitude, latitude}) => `${longitude},${latitude}`;
const radians = Math.PI / 180;
export const wrapLongitude = longitude => ((longitude + 180) % 360 + 360) % 360 - 180;

function mercatorY(latitude) {
  const bounded = Math.max(-85, Math.min(85, latitude));
  return (1 - Math.asinh(Math.tan(bounded * radians)) / Math.PI) / 2;
}

export function longitudeFrame(points) {
  const longitudes = points.map(point => wrapLongitude(point.longitude)).sort((a, b) => a - b);
  let start = longitudes[0];
  let largestGap = 0;
  for (const [index, longitude] of longitudes.entries()) {
    const next = longitudes[index + 1] ?? longitudes[0] + 360;
    if (next - longitude > largestGap) {
      largestGap = next - longitude;
      start = next;
    }
  }
  const span = 360 - largestGap;
  return {longitude: wrapLongitude(start + span / 2), span};
}

function fittingCamera(groups, fallback, {width, height}) {
  if (!groups.length) return {point: fallback, zoom: 10};
  if (groups.length === 1) return {point: groups[0].point, zoom: groups[0].area ? 10 : 14};

  const {longitude, span} = longitudeFrame(groups.map(group => group.point));
  const latitudes = groups.map(group => mercatorY(group.point.latitude));
  const yMin = Math.min(...latitudes);
  const yMax = Math.max(...latitudes);
  const latitude = Math.atan(Math.sinh(Math.PI * (1 - yMin - yMax))) / radians;
  const scale = Math.min(Math.max(100, width - 108) / (span / 360 || 1e-9),
    Math.max(80, height - 92) / (yMax - yMin || 1e-9));
  const zoom = Math.min(14, Math.max(1, Math.floor(Math.log2(scale / 256))));
  return {point: {longitude, latitude}, zoom};
}

function mapUrl({point, zoom}, groups, widget) {
  const url = new URL(widget ? 'https://yandex.ru/map-widget/v1/' : 'https://yandex.ru/maps/');
  url.searchParams.set('ll', coordinatePair(point));
  url.searchParams.set('z', String(zoom));
  url.searchParams.set('l', 'map');
  url.searchParams.set('lang', 'ru_RU');
  const points = [...new Set(groups.map(group => coordinatePair(group.point)))];
  if (points.length) url.searchParams.set('pt', points.join('~'));
  return url.href;
}

function groupLabel(group) {
  if (!group.area) return group.stations.map(station => station.label).join(', ');
  const count = group.stations.length;
  const ending = {one: 'станция', few: 'станции', many: 'станций'}[stationPlural.select(count)];
  return `Район заповедника · ${count} ${ending}`;
}

export function mountYandexLocation(viewport, callbacks) {
  const column = viewport.parentElement;
  const controls = document.createElement('div');
  controls.className = 'station-provider';
  controls.setAttribute('role', 'group');
  controls.setAttribute('aria-label', 'Источник карты');
  controls.innerHTML = '<button class="btn line" type="button" data-provider="local">Схема станций</button>' +
    '<button class="btn line" type="button" data-provider="yandex">Яндекс Карты</button>';
  const surface = document.createElement('div');
  surface.className = 'station-yandex-viewport';
  const caption = document.createElement('div');
  caption.className = 'station-provider-caption';
  const note = document.createElement('span');
  note.setAttribute('role', 'status');
  const link = document.createElement('a');
  link.textContent = 'Открыть в Яндекс Картах ↗';
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  caption.append(note, link);
  const pointList = document.createElement('div');
  pointList.className = 'station-widget-points';
  pointList.setAttribute('role', 'group');
  pointList.setAttribute('aria-label', 'Метки на Яндекс Картах');
  viewport.before(controls, surface, caption, pointList);
  const localNotes = [...column.querySelectorAll('.station-map-source, .station-map-shortcut')];
  const buttons = [...controls.querySelectorAll('button')];
  const onlineButton = controls.querySelector('[data-provider="yandex"]');
  const allPoints = document.createElement('button');
  allPoints.className = 'btn line';
  allPoints.type = 'button';
  allPoints.textContent = 'Все метки';
  let focused = null;
  let groups = [];
  let selected = [];
  let mode;
  let frame;
  let controller;
  let generation = 0;
  let visible = !window.IntersectionObserver;
  let timer;
  let observer;
  let loading = false;
  let picking = false;

  function camera() {
    if (focused) return focused;
    return fittingCamera(groups, callbacks.center, {
      width: surface.clientWidth || viewport.clientWidth || column.clientWidth,
      height: surface.clientHeight || viewport.clientHeight || 300,
    });
  }

  function syncCamera(moveSdk = false) {
    const view = camera();
    link.href = mapUrl(view, groups, false);
    if (moveSdk && controller) controller.focus(view.point, view.zoom);
    if (frame) {
      const target = mapUrl(view, groups, true);
      if (frame.src !== target) frame.src = target;
    }
  }

  function listPoints() {
    const items = groups.map(group => {
      const button = document.createElement('button');
      button.className = 'btn line';
      button.type = 'button';
      button.textContent = groupLabel(group);
      if (group.area) {
        button.title = group.stations.map(station => station.label).join(', ');
        button.setAttribute('aria-label', `${button.textContent}: ${button.title}`);
      }
      button.addEventListener('click', () => {
        focused = {point: group.point, zoom: group.area ? 11 : 14};
        syncCamera(true);
      });
      return button;
    });
    pointList.replaceChildren(allPoints, ...items);
    pointList.hidden = mode !== 'yandex' || (!frame && !controller) || !groups.length;
    allPoints.hidden = groups.length < 2;
  }

  function showRemoteSurface() {
    surface.hidden = false;
    viewport.hidden = true;
    for (const node of localNotes) node.hidden = true;
  }

  function loadWidget() {
    column.dataset.mapProvider = 'widget';
    frame = document.createElement('iframe');
    frame.title = 'Расположение на Яндекс Картах';
    frame.loading = 'lazy';
    frame.referrerPolicy = 'strict-origin-when-cross-origin';
    frame.allowFullscreen = true;
    frame.addEventListener('load', () => { clearTimeout(timer); });
    note.textContent = 'Выберите подпись, чтобы приблизить метку. Выделение рамкой доступно в схеме станций.';
    showRemoteSurface();
    syncCamera();
    surface.replaceChildren(frame);
    listPoints();
    timer = setTimeout(() => {
      if (mode === 'yandex' && frame) note.textContent = 'Если карта загружается долго, выберите схему станций.';
    }, 8000);
  }

  async function loadMap() {
    if (!visible || mode !== 'yandex' || loading || frame || controller) return;
    loading = true;
    const current = generation;
    const {browserKey} = await configuration();
    if (current !== generation) return;
    if (browserKey) {
      const container = document.createElement('div');
      container.className = 'station-sdk-surface';
      surface.replaceChildren(container);
      try {
        const {openYandexStationMap} = await import('./yandex-station-sdk.js');
        if (current !== generation) return;
        const view = camera();
        surface.hidden = false;
        const live = await openYandexStationMap(container, {...callbacks, apiKey: browserKey, groups,
          center: view.point, zoom: view.zoom});
        if (current !== generation) { live.destroy(); return; }
        controller = live;
        column.dataset.mapProvider = 'sdk';
        controller.update(groups, selected);
        controller.setPicking(picking);
        showRemoteSurface();
        listPoints();
        syncCamera(true);
        note.textContent = 'Выберите станцию или выделите несколько станций рамкой.';
      } catch {
        if (current !== generation) return;
        loadWidget();
      }
    } else {
      loadWidget();
    }
    loading = false;
  }

  function switchMode(next) {
    if (next === mode) return;
    mode = next;
    const online = next === 'yandex';
    column.dataset.mapProvider = 'local';
    ++generation;
    clearTimeout(timer);
    controller?.destroy();
    controller = null;
    frame = null;
    focused = null;
    loading = false;
    surface.replaceChildren();
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.provider === next));
    surface.hidden = pointList.hidden = true;
    caption.hidden = !online;
    viewport.hidden = false;
    for (const node of localNotes) node.hidden = false;
    if (online) {
      syncCamera();
      note.textContent = 'Загрузка Яндекс Карт…';
      loadMap();
    }
  }

  allPoints.addEventListener('click', () => {
    focused = null;
    syncCamera(true);
  });
  controls.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button && !button.disabled) switchMode(button.dataset.provider);
  });
  if (window.IntersectionObserver) {
    observer = new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting);
      if (visible) loadMap();
    }, {rootMargin: '150px'});
    observer.observe(column);
  }
  function onOffline() { switchMode('local'); }
  window.addEventListener('offline', onOffline);
  switchMode('local');
  return {
    update(nextGroups, ids) {
      const catalogChanged = nextGroups !== groups;
      groups = nextGroups;
      selected = ids;
      controller?.update(groups, selected);
      if (catalogChanged && mode === 'yandex') {
        listPoints();
        syncCamera(!focused);
      }
    },
    focus(point, exact) {
      if (mode !== 'yandex') return;
      focused = {point, zoom: exact ? 14 : 11};
      syncCamera(true);
    },
    setPicking(active) {
      picking = active;
      if (controller) controller.setPicking(active);
      else if (active) switchMode('local');
      onlineButton.disabled = active && !controller;
    },
    destroy() {
      ++generation;
      observer?.disconnect();
      clearTimeout(timer);
      window.removeEventListener('offline', onOffline);
      controller?.destroy();
      surface.replaceChildren();
    },
  };
}
