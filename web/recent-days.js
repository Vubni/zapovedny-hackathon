const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const localDate = day => day.split('-').reverse().join('.');

export function mountRecentDays(panel, context) {
  const station = panel.querySelector('[data-recent-station]');
  const param = panel.querySelector('[data-recent-param]');
  const note = panel.querySelector('[data-recent-note]');
  const grid = panel.querySelector('[data-recent-table]');
  const csv = panel.querySelector('[data-recent-csv]');
  const processRange = panel.querySelector('[data-recent-process]');
  let pending;

  async function load() {
    pending?.abort();
    grid.replaceChildren();
    csv.hidden = true;
    processRange.hidden = true;
    if (!panel.open || !station.value || !param.value) return;
    const controller = new AbortController();
    pending = controller;
    note.textContent = 'Загружаем последние сутки…';
    const query = new URLSearchParams({station: station.value, param: param.value});
    try {
      const response = await fetch('/api/observations/latest-five?' + query, {signal: controller.signal});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (controller.signal.aborted) return;
      if (!data.rows.length) {
        note.textContent = 'Измерений этого показателя пока нет.';
        return;
      }
      const table = document.createElement('table');
      table.setAttribute('aria-label', 'Последние пять суток по выбранному показателю');
      const header = table.createTHead().insertRow();
      for (const name of ['Дата', 'Суточное среднее, ' + data.unit, 'Сроков с измерением', 'Сроков с оценкой', 'Полнота суток']) {
        const th = document.createElement('th');
        th.scope = 'col';
        th.textContent = name;
        header.append(th);
      }
      const body = table.createTBody();
      for (const day of data.rows) {
        const row = body.insertRow();
        for (const value of [localDate(day.day), Number.isFinite(day.mean) && day.complete ? number.format(day.mean) : '—',
          day.observed_n, day.estimated_n, day.complete ? 'Достаточно' : day.observed_n ? 'Неполные сутки' : 'Нет измерений']) {
          row.insertCell().textContent = value;
        }
      }
      grid.append(table);
      note.textContent = localDate(data.start) + ' — ' + localDate(data.end) + '. Период заканчивается последним измерением станции. В среднее входят только полные сутки.';
      csv.href = '/api/observations/latest-five.csv?' + query;
      csv.hidden = false;
      const scope = new URLSearchParams({source: 'observations', station: data.station, param: data.param, start: data.start, end: data.end});
      processRange.href = '/files?' + scope;
      processRange.hidden = false;
    } catch {
      if (!controller.signal.aborted) note.textContent = 'Не удалось получить таблицу. Раскройте её ещё раз.';
    }
  }

  function parameters() {
    const previous = param.value;
    const info = context().stations.find(item => item.id === station.value);
    param.replaceChildren(...(info?.params || []).map(item => new Option(item.label, item.id)));
    if ([...param.options].some(item => item.value === previous)) param.value = previous;
    load();
  }

  function refresh() {
    if (!panel.open) { pending?.abort(); return; }
    const {stations, selected} = context();
    const previous = station.value;
    station.replaceChildren(...stations.map(item => new Option(item.label, item.id)));
    if (stations.some(item => item.id === previous)) station.value = previous;
    else if (stations.some(item => item.id === selected[0])) station.value = selected[0];
    parameters();
  }
  panel.addEventListener('toggle', refresh);
  station.addEventListener('change', parameters);
  param.addEventListener('change', load);
  return refresh;
}
