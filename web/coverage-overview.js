const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" });
const monthFormat = new Intl.DateTimeFormat("ru-RU", { month: "short", timeZone: "UTC" });
const fullMonthFormat = new Intl.DateTimeFormat("ru-RU", {
  month: "long", year: "numeric", timeZone: "UTC"
});
const percentFormat = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 });
const countFormat = new Intl.NumberFormat("ru-RU");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function stationCount(valid, total) {
  return `${countFormat.format(valid)} из ${countFormat.format(total)} сут.`;
}

function measureLevel(cell) {
  if (cell.ratio === null) return "unsupported";
  if (cell.observed_days === 0) return "absent";
  if (cell.ratio === 1) return "full";
  if (cell.ratio >= 0.75) return "high";
  return "low";
}

const levels = [
  ["full", "100%"],
  ["high", "75–99,9%"],
  ["low", "Менее 75%"],
  ["absent", "Нет наблюдений"],
  ["unsupported", "Параметр недоступен"]
];

function labelPeriod(cell, grain) {
  const day = new Date(cell.id);
  return grain === "year" ? cell.id.slice(0, 4) : fullMonthFormat.format(day);
}

function detailDates(period) {
  return (period.intervals || [period]).map(({ start, end }) => {
    const first = dateFormat.format(new Date(start));
    return start === end ? first : `${first} — ${dateFormat.format(new Date(end))}`;
  }).join(", ");
}

function detailSlot(row) {
  if (row.slot_minutes === 60) return `Порог суток — ${row.minimum_slots} разных часов.`;
  return `Порог суток — ${row.minimum_slots} разных сроков.`;
}

function describeCell(row, period, cell, grain) {
  const label = period.intervals?.length > 1 ? detailDates(period) : labelPeriod(period, grain);
  if (!row.supported) return `${row.label}, ${label}. Станция не измеряет этот параметр.`;
  const measured = cell.observed_days > 0 ? stationCount(cell.valid_days, cell.total_days) : "Нет наблюдений";
  return `${row.label}, ${label}. ${measured}.`;
}

function makeLegend() {
  const list = element("ul", "coverage-overview-legend");
  list.setAttribute("aria-label", "Шкала доли пригодных суток");
  for (const [level, label] of levels) {
    const item = element("li", "", label);
    const marker = element("span", "coverage-overview-swatch");
    marker.dataset.level = level;
    marker.setAttribute("aria-hidden", "true");
    if (level === "unsupported") item.hidden = true;
    item.prepend(marker);
    list.append(item);
  }
  return list;
}

export function mountCoverageOverview(container) {
  const panel = element("section", "card coverage-overview");
  const heading = element("h2", "", "Покрытие");
  heading.id = "coverage-overview-title";
  panel.setAttribute("aria-labelledby", heading.id);
  const help = element("p", "", "Доля суток, прошедших порог по числу сроков наблюдений.");
  const headingBlock = element("div");
  headingBlock.append(heading, help);

  const picker = element("label", "coverage-overview-picker", "Параметр");
  const select = element("select");
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
  const scroll = element("div", "coverage-overview-scroll");
  const footer = element("footer", "coverage-overview-footer");
  const detail = element("p", "coverage-overview-detail");
  const legend = makeLegend();
  const unavailableLegend = legend.lastElementChild;
  footer.append(legend, detail);
  content.append(scroll, footer);
  content.hidden = true;
  panel.append(header, status, retry, content);
  container.replaceChildren(panel);

  let activeRequest;
  let activeCell;
  let currentKey;
  let currentFilters;
  let parameter = "all";
  let data;
  let buttons = [];
  let enabled = true;

  function activate(button, moveFocus = false) {
    const row = data.rows[Number(button.dataset.row)];
    const index = Number(button.dataset.column);
    const period = data.periods[index];
    const cell = row.cells[index];
    if (activeCell !== button) {
      if (activeCell) {
        activeCell.tabIndex = -1;
        activeCell.setAttribute("aria-pressed", "false");
      }
      activeCell = button;
      button.tabIndex = 0;
      button.setAttribute("aria-pressed", "true");
    }
    const label = element("strong", "", `${row.label} · ${detailDates(period)}`);
    let text;
    if (!row.supported) {
      text = "Станция не измеряет этот параметр.";
    } else if (!cell.observed_days) {
      text = `Нет наблюдений за ${countFormat.format(cell.total_days)} сут. ${detailSlot(row)}`;
    } else {
      text = `Пригодны ${stationCount(cell.valid_days, cell.total_days)} (${percentFormat.format(cell.ratio)}). ${detailSlot(row)}`;
    }
    detail.replaceChildren(label, document.createTextNode(`  ${text}`));
    if (moveFocus) {
      button.focus({ preventScroll: true });
      const fixedColumn = button.closest("tr").firstElementChild.getBoundingClientRect();
      const viewport = scroll.getBoundingClientRect();
      const cellBox = button.getBoundingClientRect();
      if (cellBox.left < fixedColumn.right + 5) scroll.scrollLeft += cellBox.left - fixedColumn.right - 5;
      if (cellBox.right > viewport.right - 5) scroll.scrollLeft += cellBox.right - viewport.right + 5;
    }
  }

  function navigation(event) {
    const button = event.target.closest(".coverage-overview-cell");
    if (!button) return;
    let row = Number(button.dataset.row);
    let column = Number(button.dataset.column);
    if (event.key === "ArrowLeft") column--;
    else if (event.key === "ArrowRight") column++;
    else if (event.key === "ArrowUp") row--;
    else if (event.key === "ArrowDown") row++;
    else if (event.key === "Home") {
      column = 0;
      if (event.ctrlKey) row = 0;
    } else if (event.key === "End") {
      column = data.periods.length - 1;
      if (event.ctrlKey) row = data.rows.length - 1;
    } else return;
    event.preventDefault();
    const target = buttons[Math.max(0, Math.min(data.rows.length - 1, row))][
      Math.max(0, Math.min(data.periods.length - 1, column))
    ];
    activate(target, true);
  }

  function render(payload) {
    const previous = activeCell && data ? [
      data.rows[Number(activeCell.dataset.row)].station,
      data.periods[Number(activeCell.dataset.column)].id
    ] : null;
    const restoreFocus = scroll.contains(document.activeElement);
    data = payload;
    parameter = payload.parameter;
    help.textContent = payload.intervals.length > 1
      ? "Доля пригодных суток в выбранных интервалах."
      : "Доля суток, прошедших порог по числу сроков наблюдений.";
    unavailableLegend.hidden = data.rows.every(row => row.supported);
    select.replaceChildren(...payload.parameters.map(item => {
      const option = element("option", "", item.label);
      option.value = item.id;
      return option;
    }));
    select.value = parameter;
    picker.hidden = payload.parameters.length < 2;
    activeCell = undefined;
    status.textContent = "";
    status.hidden = true;
    if (!data.rows.length || !data.periods.length) {
      status.textContent = "Нет данных о покрытии за выбранный период.";
      status.hidden = false;
      content.hidden = true;
      return;
    }

    const table = element("table", "coverage-overview-table");
    const caption = element("caption", "coverage-overview-sr",
      `Покрытие по ${data.grain === "year" ? "годам" : "месяцам"}. Стрелки перемещают по таблице. Home и End переходят к началу и концу строки.`);
    table.append(caption);
    const headerRow = table.createTHead().insertRow();
    const station = element("th", "coverage-overview-name", "Станция");
    station.scope = "col";
    headerRow.append(station);
    const multipleYears = data.start.slice(0, 4) !== data.end.slice(0, 4);
    data.periods.forEach((period, index) => {
      const label = element("th", "coverage-overview-period");
      label.scope = "col";
      if (data.grain === "year") {
        label.textContent = period.id.slice(0, 4);
      } else {
        label.textContent = monthFormat.format(new Date(period.id)).replace(/\.$/, "");
        if (multipleYears && period.id.slice(0, 4) !== data.periods[index - 1]?.id.slice(0, 4)) {
          label.append(element("span", "coverage-overview-year", period.id.slice(0, 4)));
        }
      }
      label.setAttribute("aria-label", labelPeriod(period, data.grain));
      headerRow.append(label);
    });

    const body = table.createTBody();
    buttons = data.rows.map((row, rowIndex) => {
      const tr = body.insertRow();
      const name = element("th", "coverage-overview-name");
      name.scope = "row";
      const stationLabel = element("span", "coverage-overview-station", row.label);
      if (/^#[0-9a-f]{6}$/i.test(row.color)) {
        const dot = element("i", "coverage-overview-dot");
        dot.style.backgroundColor = row.color;
        dot.setAttribute("aria-hidden", "true");
        stationLabel.prepend(dot);
      }
      name.append(stationLabel);
      const total = row.supported ? stationCount(row.valid_days, row.total_days) : "Параметр недоступен";
      name.append(element("span", "coverage-overview-total", total));
      tr.append(name);
      return row.cells.map((cell, column) => {
        const period = data.periods[column];
        const copy = row.supported && cell.observed_days ? percentFormat.format(cell.ratio) : "—";
        const button = element("button", "coverage-overview-cell", copy);
        const label = describeCell(row, period, cell, data.grain);
        button.type = "button";
        button.dataset.row = rowIndex;
        button.dataset.column = column;
        button.dataset.level = measureLevel(cell);
        button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", "false");
        button.tabIndex = -1;
        button.title = label;
        tr.insertCell().append(button);
        return button;
      });
    });
    table.addEventListener("keydown", navigation);
    table.addEventListener("focusin", event => {
      const button = event.target.closest(".coverage-overview-cell");
      if (button) activate(button);
    });
    table.addEventListener("click", event => {
      const button = event.target.closest(".coverage-overview-cell");
      if (button) activate(button);
    });
    scroll.replaceChildren(table);
    content.hidden = false;
    const previousRow = previous ? data.rows.findIndex(row => row.station === previous[0]) : -1;
    const previousColumn = previous ? data.periods.findIndex(period => period.id === previous[1]) : -1;
    const current = previousRow >= 0 && previousColumn >= 0 ? buttons[previousRow][previousColumn] : buttons[0][0];
    activate(current, restoreFocus);
  }

  async function update(filters, { force = false } = {}) {
    currentFilters = {
      ...filters,
      stations: [...filters.stations],
      intervals: (filters.intervals || []).map(({ start, end }) => ({ start, end }))
    };
    if (!enabled) return;
    const { stations, start, end, intervals } = currentFilters;
    const keyParts = [stations, start, end, intervals];
    const key = JSON.stringify([...keyParts, parameter]);
    if (key === currentKey && !force) return;
    currentKey = key;
    activeRequest?.abort();
    activeRequest = undefined;
    retry.hidden = true;
    if (!stations.length) {
      scroll.replaceChildren();
      content.hidden = true;
      picker.hidden = true;
      status.textContent = "Выберите станции, чтобы увидеть покрытие.";
      status.hidden = false;
      panel.removeAttribute("aria-busy");
      data = null;
      activeCell = null;
      return;
    }
    status.textContent = "Загружаем покрытие…";
    status.hidden = false;
    panel.setAttribute("aria-busy", "true");
    content.hidden = true;
    const request = new AbortController();
    activeRequest = request;
    const query = new URLSearchParams({ stations: stations.join(","), start, end, parameter });
    if (intervals.length > 1) {
      query.set("intervals", intervals.map(interval => `${interval.start}:${interval.end}`).join(","));
    }
    try {
      const response = await fetch(`/api/observation-coverage?${query}`, { signal: request.signal, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error);
      if (request !== activeRequest) return;
      render(payload);
      currentKey = JSON.stringify([...keyParts, parameter]);
    } catch (error) {
      if (request !== activeRequest || error.name === "AbortError") return;
      status.textContent = error.message && error.message !== "Failed to fetch" && error.name !== "SyntaxError"
        ? error.message : "Не удалось загрузить покрытие.";
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
