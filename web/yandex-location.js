// The public widget owns its camera. The offline selection canvas has its own projection.
let configRequest;

function configuration() {
  if (!configRequest) {
    configRequest = fetch('/api/map-config').then(response => response.ok ? response.json() : {})
      .catch(() => { configRequest = undefined; return {}; });
  }
  return configRequest;
}

function mapUrl(point, exact, widget) {
  const url = new URL(widget ? 'https://yandex.ru/map-widget/v1/' : 'https://yandex.ru/maps/');
  const coordinates = [point.longitude, point.latitude].join(',');
  url.searchParams.set('ll', coordinates);
  url.searchParams.set('z', exact ? '14' : '10');
  if (exact) url.searchParams.set('pt', coordinates + ',pm2gnm');
  return url.href;
}

export function mountYandexLocation(viewport, callbacks) {
  const column = viewport.parentElement;
  const controls = document.createElement('div');
  controls.className = 'station-provider';
  controls.setAttribute('role', 'group');
  controls.setAttribute('aria-label', 'Источник карты');
  controls.innerHTML = '<button class="btn line" type="button" data-provider="yandex">Яндекс Карты</button>' +
    '<button class="btn line" type="button" data-provider="local">Схема станций</button>';
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
  viewport.before(controls, surface, caption);
  const localNotes = [...column.querySelectorAll('.station-map-source, .station-map-shortcut')];
  let point = callbacks.center;
  let exact = false;
  let groups = [];
  let selected = [];
  let mode;
  let frame;
  let controller;
  let generation = 0;
  let visible = !window.IntersectionObserver;
  let timer;
  let lastSelected;
  let observer;
  let loading = false;
  let picking = false;
  const buttons = [...controls.querySelectorAll('button')];

  function widgetNote() {
    return (exact ? 'Координаты станции.' : 'Район заповедника.') + ' Для выделения рамкой откройте схему станций.';
  }

  function locate() {
    link.href = mapUrl(point, exact, false);
    if (controller) {
      controller.focus(point);
    } else if (frame) {
      const target = mapUrl(point, exact, true);
      if (frame.src !== target) frame.src = target;
      note.textContent = widgetNote();
    }
  }

  function loadWidget() {
    column.dataset.mapProvider = 'widget';
    frame = document.createElement('iframe');
    frame.title = 'Расположение на Яндекс Картах';
    frame.loading = 'lazy';
    frame.referrerPolicy = 'strict-origin-when-cross-origin';
    frame.allowFullscreen = true;
    locate();
    frame.addEventListener('load', () => { clearTimeout(timer); });
    surface.replaceChildren(frame);
    timer = setTimeout(() => {
      if (mode === 'yandex' && frame) note.textContent = 'Карта загружается. Для работы без сети доступна схема станций.';
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
        const live = await openYandexStationMap(container, {...callbacks, apiKey: browserKey, groups, center: point});
        if (current !== generation) { live.destroy(); return; }
        controller = live;
        column.dataset.mapProvider = 'sdk';
        controller.update(groups, selected);
        controller.setPicking(picking);
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
    column.dataset.mapProvider = online ? 'widget' : 'local';
    ++generation;
    clearTimeout(timer);
    controller?.destroy();
    controller = null;
    frame = null;
    loading = false;
    surface.replaceChildren();
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.provider === next));
    surface.hidden = caption.hidden = !online;
    viewport.hidden = online;
    for (const node of localNotes) node.hidden = online;
    if (online) {
      link.href = mapUrl(point, exact, false);
      note.textContent = 'Загрузка Яндекс Карт…';
      loadMap();
    }
  }
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
  switchMode(callbacks.inline && navigator.onLine ? 'yandex' : 'local');
  return {
    update(nextGroups, ids) {
      groups = nextGroups;
      selected = ids;
      controller?.update(groups, selected);
      const unique = ids.length === 1 ? ids[0] : null;
      if (unique && unique !== lastSelected) {
        const group = groups.find(group => group.stations.some(station => station.id === unique));
        if (group) { point = group.point; exact = !group.area; locate(); }
      }
      lastSelected = unique;
    },
    focus(next, precise) { point = next; exact = precise; locate(); },
    setPicking(active) {
      picking = active;
      if (controller) controller.setPicking(active);
      else if (active) switchMode('local');
      buttons[0].disabled = active && !controller;
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
