const monthShort = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" });
const monthFormat = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric", timeZone: "UTC" });
const statusLabels = {
  full: "Все сутки валидны",
  gaps: "Есть пропуски",
  excluded: "Месяц исключён",
  absent: "Нет данных",
  unsupported: "Станция не измеряет",
};
let nextCoverageId = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function detailDates(period) {
  return period.intervals.map(({ start, end }) => {
    const first = dateFormat.format(new Date(start));
    return start === end ? first : `${first} — ${dateFormat.format(new Date(end))}`;
  }).join(", ");
}

function describeCell(cell) {
  const status = statusLabels[cell.status];
  if (cell.status === "unsupported") return `${status} этот параметр.`;
  const count = `${cell.valid_days} из ${cell.total_days} суток.`;
  if (cell.status !== "excluded") return `${count} ${status}.`;
  return `${count} ${status}. Неполных суток ${cell.total_days - cell.valid_days}, до ${cell.longest_gap} подряд. Допустимо до 10 неполных суток и до 4 подряд.`;
}

function legend(statuses) {
  const list = element("ul", "coverage-overview-legend");
  list.setAttribute("aria-label", "Состояния покрытия");
  for (const [status, label] of Object.entries(statusLabels)) {
    if ((status === "absent" || status === "unsupported") && !statuses.has(status)) continue;
    const item = element("li", "", label);
    const marker = element("span", "coverage-overview-swatch");
    marker.dataset.level = status;
    marker.setAttribute("aria-hidden", "true");
    item.prepend(marker);
    list.append(item);
  }
  return list;
}

function yearsOf(periods) {
  const years = new Map();
  periods.forEach((period, index) => {
    const year = period.id.slice(0, 4);
    if (!years.has(year)) years.set(year, []);
    years.get(year).push({ ...period, index });
  });
  return years;
}

function moveCell(event, grid) {
  const cell = event.target.closest(".coverage-overview-cell");
  if (!cell) return;
  let row = Number(cell.dataset.row);
  let column = Number(cell.dataset.column);
  switch (event.key) {
    case "ArrowLeft": column--; break;
    case "ArrowRight": column++; break;
    case "ArrowUp": row--; break;
    case "ArrowDown": row++; break;
    case "Home": column = 0; if (event.ctrlKey) row = 0; break;
    case "End": column = grid[0].length - 1; if (event.ctrlKey) row = grid.length - 1; break;
    default: return;
  }
  event.preventDefault();
  grid[Math.max(0, Math.min(grid.length - 1, row))][Math.max(0, Math.min(grid[0].length - 1, column))].focus();
}

function makeBlock(station, year, months, selected, onPick, controls, statuses) {
  const block = element("section", "coverage-overview-block");
  const heading = element("h3", "coverage-overview-station", `${station.label} · ${year}`);
  heading.id = `coverage-block-${++nextCoverageId}`;
  block.setAttribute("aria-labelledby", heading.id);
  const scroll = element("div", "coverage-overview-scroll");
  const table = element("table", "coverage-overview-table");
  table.setAttribute("aria-labelledby", heading.id);
  const header = table.createTHead().insertRow();
  const corner = element("th", "coverage-overview-name");
  corner.scope = "col";
  corner.append(element("span", "coverage-overview-sr", "Показатель"));
  header.append(corner);
  for (const month of months) {
    const label = element("th", "coverage-overview-period", monthShort[Number(month.id.slice(5, 7)) - 1]);
    label.scope = "col";
    label.setAttribute("aria-label", monthFormat.format(new Date(month.id)));
    header.append(label);
  }

  const metrics = [{ label: "Станция", id: "all", cells: station.station_cells }];
  if (selected.id !== "all") metrics.push({ ...selected, cells: station.cells });
  const body = table.createTBody();
  let tabCell;
  function select(control) {
    if (tabCell !== control.button) {
      tabCell.tabIndex = -1;
      control.button.tabIndex = 0;
      tabCell = control.button;
    }
    onPick(control);
  }
  const grid = metrics.map((metric, row) => {
    const tr = body.insertRow();
    const name = element("th", "coverage-overview-name", metric.label);
    name.scope = "row";
    tr.append(name);
    return months.map((month, column) => {
      const cell = metric.cells[month.index];
      const count = cell.status === "unsupported" ? "—" : `${cell.valid_days}/${cell.total_days}`;
      const button = element("button", "coverage-overview-cell", count);
      const heading = `${station.label} · ${metric.label} · ${detailDates(month)}`;
      const detail = describeCell(cell);
      const control = { button, heading, detail, status: cell.status,
        key: `${station.station}:${metric.id}:${month.id}`, select() { select(control); } };
      statuses.add(cell.status);
      controls.set(control.key, control);
      button.type = "button";
      button.tabIndex = -1;
      button.dataset.row = row;
      button.dataset.column = column;
      button.dataset.level = cell.status;
      button.title = `${heading} · ${detail}`;
      button.setAttribute("aria-label", button.title);
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("focus", control.select);
      button.addEventListener("click", control.select);
      tr.insertCell().append(button);
      return button;
    });
  });
  tabCell = grid[0][0];
  tabCell.tabIndex = 0;
  table.addEventListener("keydown", event => moveCell(event, grid));
  scroll.append(table);
  block.append(heading, scroll);
  return block;
}

export function mountCoverageOverview(container, { compact = false } = {}) {
  const panel = element("section", compact ? "coverage-overview coverage-overview-embedded" : "card coverage-overview");
  const heading = element("h2", compact ? "coverage-overview-sr" : "", "Покрытие по месяцам");
  heading.id = `coverage-title-${++nextCoverageId}`;
  panel.setAttribute("aria-labelledby", heading.id);
  const help = element("p", "", "Валидные дни / выбранные календарные дни");
  const headingBlock = element("div");
  headingBlock.append(heading, help);
  const picker = element("label", "coverage-overview-picker", "Параметр");
  const select = element("select");
  select.setAttribute("aria-label", "Параметр покрытия");
  picker.append(select);
  picker.hidden = true;
  const header = element("header", "coverage-overview-heading");
  header.append(headingBlock, picker);

  const status = element("p", "coverage-overview-status", "Выберите станции, чтобы увидеть покрытие.");
  status.setAttribute("role", "status");
  const retry = element("button", "btn line coverage-overview-retry", "Повторить загрузку");
  retry.type = "button";
  retry.hidden = true;
  const content = element("div", "coverage-overview-content");
  const blocks = element("div", "coverage-overview-blocks");
  const footer = element("div", "coverage-overview-footer");
  const legendHost = element("div");
  const detail = element("p", "coverage-overview-detail");
  detail.setAttribute("aria-live", "polite");
  footer.append(legendHost, detail);
  content.append(blocks, footer);
  content.hidden = true;
  panel.append(header, status, retry, content);
  container.replaceChildren(panel);

  let activeRequest;
  let active;
  let currentKey;
  let currentFilters;
  let chart;
  let parameter;
  let enabled = true;

  function pick(control) {
    if (active === control) return;
    active?.button.setAttribute("aria-pressed", "false");
    active = control;
    active.button.setAttribute("aria-pressed", "true");
    detail.replaceChildren(element("strong", "", control.heading), document.createTextNode(` ${control.detail}`));
  }

  function render(data) {
    const previous = active?.key;
    const selected = data.parameters.find(item => item.id === data.parameter);
    const options = data.parameters.filter(item => item.id !== "all"
      && (!compact || data.chart_parameters.includes(item.id)));
    select.replaceChildren(...options.map(item => {
      const option = element("option", "", item.label);
      option.value = item.id;
      return option;
    }));
    select.value = selected.id;
    picker.hidden = options.length < 2;
    active = undefined;
    status.textContent = "";
    status.hidden = true;
    if (!data.rows.length || !data.periods.length) {
      status.textContent = "Нет данных о покрытии за выбранный период.";
      status.hidden = false;
      content.hidden = true;
      return;
    }

    const years = yearsOf(data.periods);
    const controls = new Map();
    const statuses = new Set();
    const fragment = document.createDocumentFragment();
    for (const station of data.rows) {
      for (const [year, months] of years) {
        fragment.append(makeBlock(station, year, months, selected, pick, controls, statuses));
      }
    }
    blocks.replaceChildren(fragment);
    legendHost.replaceChildren(legend(statuses));
    content.hidden = false;
    const initial = controls.get(previous) || controls.values().next().value;
    initial.select();
  }

  async function update(filters, { force = false } = {}) {
    currentFilters = {
      ...filters,
      stations: [...filters.stations],
      intervals: (filters.intervals || []).map(({ start, end }) => ({ start, end }))
    };
    if (chart !== (filters.chart || "air")) {
      chart = filters.chart || "air";
      parameter = undefined;
    }
    if (!enabled) return;
    const { stations, start, end, intervals } = currentFilters;
    const key = JSON.stringify([stations, start, end, intervals, chart, parameter]);
    if (key === currentKey && !force) return;
    currentKey = key;
    activeRequest?.abort();
    activeRequest = undefined;
    retry.hidden = true;
    if (!stations.length) {
      blocks.replaceChildren();
      content.hidden = true;
      picker.hidden = true;
      status.textContent = "Выберите станции, чтобы увидеть покрытие.";
      status.hidden = false;
      panel.removeAttribute("aria-busy");
      active = undefined;
      return;
    }
    status.textContent = "Загружаем покрытие…";
    status.hidden = false;
    panel.setAttribute("aria-busy", "true");
    content.hidden = true;
    const request = new AbortController();
    activeRequest = request;
    const query = new URLSearchParams({ stations: stations.join(","), start, end, chart });
    if (parameter) query.set("parameter", parameter);
    if (intervals.length > 1) {
      query.set("intervals", intervals.map(interval => `${interval.start}:${interval.end}`).join(","));
    }
    try {
      const response = await fetch(`/api/observation-coverage?${query}`, { signal: request.signal, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error);
      if (request === activeRequest) render(payload);
    } catch (error) {
      if (request !== activeRequest || error.name === "AbortError") return;
      status.textContent = "Не удалось загрузить покрытие.";
      retry.hidden = false;
      currentKey = undefined;
    } finally {
      if (request === activeRequest) {
        activeRequest = undefined;
        panel.removeAttribute("aria-busy");
      }
    }
  }

  select.addEventListener("change", () => {
    parameter = select.value;
    update(currentFilters);
  });
  function refresh() {
    if (currentFilters) return update(currentFilters, { force: true });
  }
  function setEnabled(value) {
    const next = Boolean(value);
    if (enabled === next) return;
    enabled = next;
    panel.hidden = !enabled;
    if (enabled) return refresh();
    activeRequest?.abort();
    activeRequest = undefined;
    currentKey = undefined;
    panel.removeAttribute("aria-busy");
  }
  retry.addEventListener("click", refresh);
  return { update, setEnabled, refresh };
}
