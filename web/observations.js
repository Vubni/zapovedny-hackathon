import { mountTimeline } from "/observation-timeline.js";
import { renderChartCard, download, periodFilename } from "/observation-chart.js";
import { pickStations, mountStationOverview } from "/station-map.js";
import { mountCoverageOverview } from "/coverage-overview.js";
import { mountObservationIndicators } from "/observation-indicators.js";

const chartGroups = [
  ["Температура воздуха", [
    ["air", "Средняя температура воздуха", "°C"],
    ["air_range", "Минимум и максимум температуры", "°C"],
    ["air_anomaly", "Отклонение от среднего за выбранный период", "°C"],
    ["air_trend", "Тренд температуры воздуха", "°C"],
    ["temp_difference", "Разница температур · Кр2 − Кр1", "°C"],
    ["temp_diurnal", "Средний суточный ход температуры", "°C"],
  ]],
  ["Температура почвы", [
    ["soil_05", "Температура почвы на глубине 5 см", "°C"],
    ["soil_10", "Температура почвы на глубине 10 см", "°C"],
    ["soil_20", "Температура почвы на глубине 20 см", "°C"],
  ]],
  ["Влажность", [
    ["rh", "Средняя влажность", "%"],
    ["rh_range", "Минимум и максимум влажности", "%"],
  ]],
  ["Скорость и порывы ветра", [
    ["wind_speed", "Средняя скорость ветра", "м/с"],
    ["wind_gust", "Максимальные порывы ветра", "м/с"],
    ["wind_combined", "Скорость и порывы ветра", "м/с"],
  ]],
  ["Направление ветра", [
    ["wind_rose", "Роза ветров", "%"],
    ["wind_freq", "Повторяемость направлений ветра", "%"],
    ["calm", "Доля штиля", "%"],
  ]],
  ["Атмосферное давление", [
    ["pressure", "Среднее атмосферное давление", "гПа"],
    ["pressure_range", "Минимум и максимум давления", "гПа"],
  ]],
];

const charts = new Map(chartGroups.flatMap(([, items]) => items.map(([id, title, unit]) => [id, { id, title, unit }])));
function orderedCharts(ids) {
  const enabled = new Set(ids);
  return [...charts.keys()].filter(id => enabled.has(id));
}
const sources = {
  modern: ["Belchertown", "https://www.belchertownweather.com/graphs/"],
  togu: ["Пособие ТОГУ", "https://togudv.ru/media/filer_public/49/4a/494aa07f-81f4-4bfd-bb8b-f8dc6519843f/meteorology-and-klimatology.pdf"],
  semenov: ["Учебный ориентир", "https://doi.org/10.12737/textbook_5c863163b4d2a8.92898948"],
};
const grains = new Set(["day", "decade", "month", "year"]);
const storageKey = "meteoarchive.observations.v1";
const defaultState = {
  selected: ["kr1"],
  start: "2025-07-01",
  end: "2025-07-31",
  mode: "modern",
  grain: "day",
  charts: ["air"],
};
const emptySelection = "Выберите хотя бы одну станцию";
const defaultIntervals = [{ start: defaultState.start, end: defaultState.end }];
const maxDateIntervals = 12;
const millisecondsPerDay = 86400000;
const numberFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const icons = {
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chart: '<path d="M3 19h18M5 15l4-5 4 3 6-8"/><circle cx="19" cy="5" r="1.3"/>',
};
const icon = (name, size = 14) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const dateLabel = value => value.split("-").reverse().join(".");
const asDay = value => Date.parse(value) / millisecondsPerDay;
const asDate = day => new Date(day * millisecondsPerDay).toISOString().slice(0, 10);

function isDate(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(asDay(value)) && asDate(asDay(value)) === value;
}

function normalizeIntervals(rows) {
  if (!Array.isArray(rows) || !rows.length || rows.some(row =>
    !isDate(row?.start) || !isDate(row?.end) || row.start > row.end)) return null;
  const ordered = rows.map(({ start, end }) => ({ start, end })).sort((a, b) => a.start.localeCompare(b.start));
  const merged = [];
  for (const interval of ordered) {
    const previous = merged.at(-1);
    if (previous && asDay(interval.start) <= asDay(previous.end) + 1) {
      if (interval.end > previous.end) previous.end = interval.end;
    } else {
      merged.push(interval);
    }
  }
  return merged.length <= maxDateIntervals ? merged : null;
}

function parseIntervals(value) {
  return normalizeIntervals(value.split(",").map(token => {
    const [start, end, extra] = token.split(":");
    return extra === undefined ? { start, end } : null;
  }));
}

function encodeIntervals(intervals) {
  return intervals.map(({ start, end }) => `${start}:${end}`).join(",");
}

function periodSelection(intervals) {
  return { start: intervals[0].start, end: intervals.at(-1).end, intervals };
}

export function observationDates() {
  return periodSelection(state.intervals.map(interval => ({ ...interval })));
}

export function observationDateQuery() {
  const { start, end, intervals } = state;
  return intervals.length > 1 ? { start, end, intervals: encodeIntervals(intervals) } : { start, end };
}

function notifyDates() {
  document.dispatchEvent(new CustomEvent("observations:dates", { detail: observationDates() }));
}

function restoreLocalState() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (!saved) return { ...defaultState, intervals: defaultIntervals };
    const intervals = normalizeIntervals(saved.intervals) || normalizeIntervals([{ start: saved.start, end: saved.end }]) || defaultIntervals;
    return {
      selected: Array.isArray(saved.selected) ? [...new Set(saved.selected.filter(id => typeof id === "string"))] : defaultState.selected,
      ...periodSelection(intervals),
      mode: Object.hasOwn(sources, saved.mode) ? saved.mode : defaultState.mode,
      grain: grains.has(saved.grain) ? saved.grain : defaultState.grain,
      charts: Array.isArray(saved.charts) ? [...new Set(saved.charts.filter(id => charts.has(id)))] : defaultState.charts,
    };
  } catch {
    return { ...defaultState, intervals: defaultIntervals };
  }
}

function restoreState() {
  const saved = restoreLocalState();
  const query = new URLSearchParams(location.search);
  const start = query.get("start") ?? saved.start;
  const end = query.get("end") ?? saved.end;
  const mode = query.get("mode");
  const grain = query.get("grain");
  const selected = query.get("stations");
  const chartIds = query.get("charts");
  const givenIntervals = query.get("intervals");
  const intervals = givenIntervals !== null ? parseIntervals(givenIntervals)
    : query.has("start") || query.has("end") ? normalizeIntervals([{ start, end }]) : saved.intervals;
  return {
    selected: selected === null ? saved.selected : [...new Set(selected.split(",").filter(Boolean))],
    ...periodSelection(intervals || saved.intervals),
    mode: Object.hasOwn(sources, mode) ? mode : saved.mode,
    grain: grains.has(grain) ? grain : saved.grain,
    charts: chartIds === null ? saved.charts : [...new Set(chartIds.split(",").filter(id => charts.has(id)))],
  };
}

function saveState({ updateAddress = true, deferAddress = false } = {}) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(state));
  } catch {
    // The page remains usable when browser storage is unavailable.
  }
  if (!updateAddress) return;
  clearTimeout(addressTimer);
  if (deferAddress) addressTimer = setTimeout(saveAddress, 180);
  else saveAddress();
}

function saveAddress() {
  clearTimeout(addressTimer);
  addressTimer = undefined;
  const url = new URL(location.href);
  const dates = observationDateQuery();
  const filters = {
    stations: state.selected.join(","),
    ...dates,
    grain: state.grain,
    mode: state.mode,
    charts: state.charts.join(","),
  };
  if (!dates.intervals) url.searchParams.delete("intervals");
  for (const [key, value] of Object.entries(filters)) url.searchParams.set(key, value);
  if (url.href !== location.href) history.replaceState(history.state, "", url);
}

const state = restoreState();
state.charts = orderedCharts(state.charts);
let stations = [];
let bounds;
let waitingForStations = true;
let openingStationPicker = false;
let renderTimer;
let addressTimer;
const cards = new Map();
const get = id => document.getElementById(id);
const dateRows = get("observation-date-rows");
const addDateInterval = get("add-date-interval");
let notifiedDateKey;
const dateError = get("date-error");
const stationError = get("station-error");
const archiveRange = get("archive-range");
const openStations = get("choose-stations");
const stationChips = get("selected-stations");
const clearStations = get("clear-stations");
const modeSource = get("mode-source");
const modeButtons = document.querySelectorAll(".dash-modes [data-mode]");
const grainInput = get("observation-grain");
const catalog = get("chart-catalog");
const catalogGroups = get("observation-catalog-groups");
const observationGroups = get("observation-groups");
const summary = get("observation-summary");
const displayedGroups = chartGroups.map(([name, items]) => {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  const grid = document.createElement("div");
  section.className = "observation-group";
  section.setAttribute("aria-label", name);
  heading.className = "observation-group-title";
  heading.textContent = name;
  grid.className = "observation-group-grid";
  section.append(heading, grid);
  observationGroups.append(section);
  return { section, heading, grid, ids: items.map(([id]) => id) };
});

const exportAll = get("download-all-charts");
const exportDialog = get("chart-export-dialog");
const exportMessage = get("chart-export-message");
let exporting = false;

function openCardsInOrder() {
  return Array.from(document.querySelectorAll("[data-observation-chart]"), element =>
    cards.get(element.dataset.observationChart));
}

function updateBulkExport() {
  const opened = openCardsInOrder();
  const ready = state.selected.length && opened.length && opened.every(card => card?.status === "ready" && card.instance);
  exportAll.disabled = exporting || !ready;
  exportAll.setAttribute("aria-busy", String(exporting));
  if (exportDialog.open) {
    if (!ready) exportDialog.close();
    else get("chart-export-count").textContent = `Графиков в ZIP-архиве: ${opened.length}. Выберите формат изображений.`;
  }
}

exportAll.addEventListener("click", () => {
  exportMessage.hidden = true;
  exportDialog.showModal();
  updateBulkExport();
});

exportDialog.addEventListener("click", async event => {
  const format = event.target.closest("[data-archive-format]")?.dataset.archiveFormat;
  if (!format || exporting) return;
  const opened = openCardsInOrder();
  if (!opened.length || opened.some(card => card?.status !== "ready" || !card.instance)) {
    updateBulkExport();
    return;
  }
  exportDialog.close();
  exporting = true;
  updateBulkExport();
  exportMessage.hidden = false;
  exportMessage.textContent = "Собираем графики в ZIP…";
  const name = `Метеоархив_${periodFilename(state.intervals)}_${format}.zip`;
  try {
    // Each card captures its legend and zoom before asynchronous raster conversion starts.
    const files = await Promise.all(opened.map(card => card.instance.exportFile(format, { includeEmpty: true })));
    const { createChartArchive } = await import("/chart-archive.js");
    const archive = await createChartArchive(files);
    download(archive, name);
    exportMessage.textContent = `Скачивание началось. Файлов в архиве: ${files.length}.`;
  } catch {
    exportMessage.textContent = "Не удалось собрать архив графиков. Попробуйте скачать его ещё раз.";
  } finally {
    exporting = false;
    updateBulkExport();
  }
});

async function requestJson(url, signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    if (response.status >= 400 && response.status < 500) {
      const body = await response.json().catch(() => null);
      if (typeof body?.error === "string") error.details = body.error;
    }
    throw error;
  }
  return response.json();
}

function setStatus(element, message) {
  element.hidden = !message;
  element.textContent = message;
}

function stationLabel(id) {
  return stations.find(station => station.id === id)?.label || (id === "kr1" ? "Кр1 · поляна" : id);
}

function renderStations() {
  stationOverview.update(stations, state.selected);
  stationChips.replaceChildren(...state.selected.map(id => {
    const station = stations.find(item => item.id === id);
    const label = stationLabel(id);
    const li = document.createElement("li");
    const dot = document.createElement("i");
    const name = document.createElement("span");
    const remove = document.createElement("button");
    li.className = "station-chip";
    dot.className = "swatch";
    dot.setAttribute("aria-hidden", "true");
    dot.style.setProperty("--dot", /^#[0-9a-f]{6}$/i.test(station?.color) ? station.color : "var(--green)");
    name.textContent = label;
    remove.type = "button";
    remove.setAttribute("aria-label", `Убрать станцию ${label}`);
    remove.title = "Убрать станцию";
    remove.innerHTML = icon("close", 12);
    remove.addEventListener("click", () => {
      updateStations(state.selected.filter(current => current !== id));
      openStations.focus();
    });
    li.append(dot, name, remove);
    return li;
  }));
  clearStations.hidden = !state.selected.length;
}

function updateStations(ids, updateAddress = true) {
  state.selected = [...new Set(ids)];
  saveState({ updateAddress });
  renderStations();
  updateCharts();
}

async function loadStationCatalog() {
  const data = await requestJson("/api/stations");
  if (!Array.isArray(data.stations)) throw new Error("Некорректный список станций");
  stations = data.stations;
  if (isDate(data.start) && isDate(data.end) && data.start <= data.end) {
    bounds = { start: data.start, end: data.end };
    applyDateBounds();
  }
  get("all-dates").disabled = !bounds;
  archiveRange.hidden = !bounds;
  renderStations();
  syncHandles();
  setStatus(stationError, "");
}

async function chooseStations() {
  if (openingStationPicker) return;
  openingStationPicker = true;
  try {
    await loadStationCatalog();
    const selected = await pickStations(stations, [...state.selected]);
    if (Array.isArray(selected)) updateStations(selected);
    await loadStationCatalog();
    if (selected) updateCharts();
  } catch {
    setStatus(stationError, "Не удалось загрузить список станций. Повторите попытку.");
  } finally {
    openingStationPicker = false;
  }
}

function renderMode() {
  for (const button of modeButtons) button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  const [label, href] = sources[state.mode];
  modeSource.textContent = `${label} ↗`;
  modeSource.href = href;
  grainInput.value = state.grain;
}

function syncHandles() {
  const rows = readDateRows();
  const hasDraft = rows.length !== state.intervals.length || (rows.length > 1 &&
    rows.some((row, index) => row.start !== state.intervals[index].start || row.end !== state.intervals[index].end));
  timeline.update(bounds, state.intervals, hasDraft);
}

function applyDateBounds() {
  if (!bounds) return;
  for (const field of dateRows.querySelectorAll("input")) {
    field.min = bounds.start;
    field.max = bounds.end;
  }
}

function renderDateRows(intervals) {
  const multiple = intervals.length > 1;
  dateRows.innerHTML = intervals.map(({ start, end }, index) => {
    const ordinal = index + 1;
    const startId = index ? `dash-start-${ordinal}` : "dash-start";
    const endId = index ? `dash-end-${ordinal}` : "dash-end";
    const labelId = `date-interval-name-${ordinal}`;
    return `<fieldset class="date-interval" data-date-interval${multiple ? ` aria-labelledby="${labelId}"` : ""}>
      <legend${multiple ? "" : " hidden"}>
        <span id="${labelId}">Интервал ${ordinal}</span>
        <button type="button" class="icon-btn date-remove" data-remove-date="${index}" aria-label="Убрать интервал ${ordinal}" title="Убрать интервал ${ordinal}">${icon("close", 16)}</button>
      </legend>
      <div class="date-interval-fields">
        <div class="field"><label for="${startId}">Дата с</label><input type="date" id="${startId}" value="${escape(start)}" required aria-describedby="date-error"></div>
        <div class="field"><label for="${endId}">Дата по</label><input type="date" id="${endId}" value="${escape(end)}" required aria-describedby="date-error"></div>
      </div>
    </fieldset>`;
  }).join("");
  applyDateBounds();
  addDateInterval.disabled = intervals.length >= maxDateIntervals;
  get("date-instructions").hidden = !multiple;
  syncHandles();
}

function readDateRows() {
  return [...dateRows.children].map(row => {
    const [startInput, endInput] = row.querySelectorAll("input");
    return { start: startInput.value, end: endInput.value, startInput, endInput };
  });
}

function setDateError(message, invalid = []) {
  setStatus(dateError, message);
  for (const field of dateRows.querySelectorAll("input")) {
    if (invalid.includes(field)) field.setAttribute("aria-invalid", "true");
    else field.removeAttribute("aria-invalid");
  }
}

function commitIntervals(intervals, { immediate = true, redraw = false, retry = false } = {}) {
  if (encodeIntervals(intervals) !== encodeIntervals(state.intervals)) {
    Object.assign(state, periodSelection(intervals));
    saveState({ deferAddress: !immediate });
  }
  if (redraw) renderDateRows(state.intervals);
  else syncHandles();
  clearTimeout(renderTimer);
  if (immediate) {
    saveAddress();
    updateCharts({ retry });
  } else {
    renderTimer = setTimeout(updateCharts, 180);
  }
}

function dateChange({ immediate = false } = {}) {
  setStatus(get("date-result"), "");
  const rows = readDateRows();
  if (rows.length > 1 && !immediate) syncHandles();
  for (const [index, { start, end, startInput, endInput }] of rows.entries()) {
    const fields = [startInput, endInput];
    const prefix = rows.length > 1 ? `Интервал ${index + 1}. ` : "";
    const missing = fields.filter(field => !isDate(field.value));
    if (missing.length) {
      if (immediate || rows.length === 1) setDateError(`${prefix}Введите обе даты.`, missing);
      return;
    }
    if (start > end) {
      setDateError(`${prefix}Дата начала должна быть раньше даты окончания или совпадать с ней.`, fields);
      return;
    }
    const outside = bounds ? fields.filter(field => field.value < bounds.start || field.value > bounds.end) : [];
    if (outside.length) {
      setDateError(`${prefix}Выберите даты от ${dateLabel(bounds.start)} до ${dateLabel(bounds.end)}.`, outside);
      return;
    }
  }
  setDateError("");
  if (!immediate && rows.length > 1) return;
  const intervals = normalizeIntervals(rows);
  const redraw = encodeIntervals(rows) !== encodeIntervals(intervals);
  commitIntervals(intervals, { immediate, redraw, retry: immediate });
  if (intervals.length < rows.length) setStatus(get("date-result"), "Смежные и пересекающиеся интервалы объединены.");
}

function replaceDates(start, end) {
  setDateError("");
  setStatus(get("date-result"), "");
  commitIntervals([{ start, end }], { redraw: true });
}

function editTimeline(index, start, end, final) {
  const row = readDateRows()[index];
  if (!row) return;
  row.startInput.value = start;
  row.endInput.value = end;
  setDateError("");
  if (!final) return;
  const next = state.intervals.map((interval, i) => i === index ? {start, end} : interval);
  const normalized = normalizeIntervals(next);
  commitIntervals(normalized, {redraw: normalized.length !== next.length});
}

function drawCatalog() {
  catalogGroups.innerHTML = chartGroups.map(([group, items]) => `<section class="catalog-group">
    <h3>${group}</h3>
    ${items.map(([id, title]) => `<button type="button" class="catalog-item" data-toggle-chart="${id}" aria-pressed="false"><span>${title}</span><span class="catalog-toggle">${icon("plus", 16)}</span></button>`).join("")}
  </section>`).join("");
}

function updateCatalog() {
  for (const button of catalogGroups.querySelectorAll("[data-toggle-chart]")) {
    const chosen = state.charts.includes(button.dataset.toggleChart);
    if (button.getAttribute("aria-pressed") !== String(chosen)) {
      button.setAttribute("aria-pressed", String(chosen));
      button.lastElementChild.innerHTML = icon(chosen ? "check" : "plus", 16);
    }
  }
  get("catalog-count").textContent = `На экране: ${state.charts.length + indicators.count + Number(coverageToggle.checked)}`;
}

function toggleChart(id, keepFocus = false) {
  const index = state.charts.indexOf(id);
  state.charts = index < 0 ? orderedCharts([...state.charts, id]) : state.charts.filter(current => current !== id);
  saveState();
  updateCatalog();
  updateCharts();
  if (keepFocus) get("open-chart-catalog").focus();
}

function showCardMessage(card, message, detail = "", status = "status") {
  const chart = charts.get(card.id);
  card.placeholder.innerHTML = `<div class="card-head"><h2>${chart.title}</h2>
    <div class="row chart-tools"><span class="tag">${chart.unit}</span>
    <button class="icon-btn" type="button" aria-label="Скрыть · ${chart.title}" title="Скрыть · ${chart.title}">${icon("close", 16)}</button></div></div>
    <div class="empty-chart obs-chart__empty" role="${status}">${icon("chart", 29)}
      <h3>${escape(message)}</h3>${detail ? `<p>${escape(detail)}</p>` : ""}
    </div>`;
  card.placeholder.querySelector("button").addEventListener("click", () => toggleChart(card.id, true));
}

function renderCard(card) {
  card.mount.hidden = card.status !== "ready";
  card.placeholder.hidden = !card.mount.hidden;
  if (card.status === "waiting" || card.status === "loading") {
    showCardMessage(card, "Загружаем наблюдения…");
  } else if (card.status === "no-stations") {
    showCardMessage(card, emptySelection);
  } else if (card.status === "error") {
    showCardMessage(card, "Не удалось загрузить наблюдения", card.errorDetails || "Нажмите «Показать», чтобы повторить запрос.", "alert");
  } else {
    const { start, end, grain, mode } = state;
    card.instance = renderChartCard(card.mount, card.data, {
      mode, grain, from: start, to: end, intervals: card.data.intervals ?? state.intervals, primary: card.primary,
      onHide: () => toggleChart(card.id, true),
    });
  }
  updateBulkExport();
}

function createCard(id) {
  const element = document.createElement("article");
  const mount = document.createElement("div");
  const placeholder = document.createElement("div");
  element.dataset.observationChart = id;
  mount.className = "observation-card-mount";
  placeholder.className = "card dashboard-chart observation-card-placeholder";
  element.append(mount, placeholder);
  const card = { id, element, mount, placeholder };
  cards.set(id, card);
  return card;
}

function updateCard(card, retry) {
  const query = new URLSearchParams({
    stations: state.selected.join(","),
    ...observationDateQuery(),
    grain: state.grain,
    mode: state.mode,
    chart: card.id,
  });
  const queryKey = waitingForStations ? "waiting" : state.selected.length ? query.toString() : "no-stations";
  const primaryCard = card.id === state.charts[0];
  card.element.classList.toggle("is-primary", primaryCard);
  if (card.key === queryKey && !(retry && card.status === "error")) {
    if (card.primary !== primaryCard) {
      card.primary = primaryCard;
      renderCard(card);
    }
    return;
  }

  card.controller?.abort();
  card.key = queryKey;
  card.primary = primaryCard;
  card.status = waitingForStations ? "waiting" : state.selected.length ? "loading" : "no-stations";
  card.data = undefined;
  renderCard(card);
  if (card.status !== "loading") return;

  const controller = new AbortController();
  card.controller = controller;
  requestJson(`/api/observations?${query}`, controller.signal).then(data => {
    if (controller.signal.aborted) return;
    card.status = "ready";
    card.data = { id: card.id, ...data };
    renderCard(card);
    if (state.charts[0] === card.id) renderSummary(card);
  }).catch(error => {
    if (controller.signal.aborted) return;
    console.error(error);
    card.status = "error";
    card.errorDetails = error.details;
    renderCard(card);
    if (state.charts[0] === card.id) renderSummary(card);
  });
}

function formatValue(value, unit) {
  return `${numberFormat.format(value)} ${unit}`;
}

function summaryOf(series) {
  let min = Infinity;
  let max = -Infinity;
  let weightedSum = 0;
  let weight = 0;
  for (const point of series.points) {
    if (!Number.isFinite(point.y)) continue;
    min = Math.min(min, point.y);
    max = Math.max(max, point.y);
    const usedDays = point.used_days || 1;
    weightedSum += point.y * usedDays;
    weight += usedDays;
  }
  return weight ? { min, max, average: weightedSum / weight } : null;
}

function renderSummary(card) {
  const intervals = state.intervals.map(({ start, end }) => `${dateLabel(start)} — ${dateLabel(end)}`);
  const heading = intervals.length === 1
    ? `<span class="eyebrow">За выбранный период</span><h2>${intervals[0]}</h2>`
    : `<span class="eyebrow">За выбранные интервалы</span><ul class="summary-intervals">${intervals.map(interval => `<li>${interval}</li>`).join("")}</ul>`;
  const data = card.data;
  const stats = data?.series?.map(series => ({ series, stats: summaryOf(series) })).filter(result => result.stats) ?? [];
  if (!stats.length) {
    const labels = {
      waiting: "Загружаем наблюдения…",
      loading: "Загружаем наблюдения…",
      "no-stations": emptySelection,
      error: "Не удалось загрузить наблюдения",
    };
    summary.innerHTML = `${heading}<p class="muted">${labels[card.status] || "Нет наблюдений"}</p>`;
    return;
  }

  let details;
  if (data.axis === "direction") {
    details = stats.map(({ series, stats }) => {
      const point = series.points.find(point => point.y === stats.max);
      return `<div class="between"><span>${escape(series.label)} · ${escape(point.x)}</span><strong>${escape(formatValue(stats.max, series.unit || data.unit))}</strong></div>`;
    }).join("");
  } else if (stats.length === 1 && card.id === "air") {
    const [{ series, stats: values }] = stats;
    const unit = series.unit || data.unit;
    details = `<span class="big">${escape(formatValue(values.average, unit))}</span><p class="muted">${escape(series.label)}</p>
      <div class="between small"><span class="muted">Диапазон</span><strong>${escape(numberFormat.format(values.min))} … ${escape(formatValue(values.max, unit))}</strong></div>`;
  } else {
    details = `<div class="observation-summary-values">${stats.map(({ series, stats }) => {
      const unit = series.unit || data.unit;
      const interval = stats.min === stats.max ? formatValue(stats.max, unit) : `${numberFormat.format(stats.min)} … ${formatValue(stats.max, unit)}`;
      return `<div class="between small"><span>${escape(series.label)}</span><strong>${escape(interval)}</strong></div>`;
    }).join("")}</div>`;
  }
  summary.innerHTML = `${heading}${details}`;
}

function updateCharts({ retry = false } = {}) {
  updateExtraPanels({retry});
  clearTimeout(renderTimer);
  const dateKey = encodeIntervals(state.intervals);
  if (dateKey !== notifiedDateKey) {
    notifiedDateKey = dateKey;
    notifyDates();
  }
  const selected = new Set(state.charts);
  const [primaryId] = state.charts;
  for (const [id, card] of cards) {
    if (!selected.has(id)) {
      card.controller?.abort();
      card.instance?.destroy();
      card.element.remove();
      cards.delete(id);
    }
  }
  observationGroups.hidden = !selected.size;
  summary.hidden = !selected.size;
  get("hidden-observations").hidden = !!selected.size;

  for (const { section, heading, grid, ids } of displayedGroups) {
    const visible = ids.filter(id => selected.has(id));
    section.hidden = !visible.length;
    heading.hidden = selected.size < 2;
    grid.classList.toggle("has-primary", visible.includes(primaryId));
    grid.classList.toggle("has-one-card", visible.length === 1);

    // Insert new cards against their existing neighbors to keep mounted chart controls and dialogs in place.
    let next = null;
    for (const id of visible.reverse()) {
      const card = cards.get(id) || createCard(id);
      if (!card.element.parentElement) grid.insertBefore(card.element, next);
      next = card.element;
    }
  }

  const primaryCard = cards.get(primaryId);
  if (primaryCard && primaryCard.element.nextElementSibling !== summary) primaryCard.element.after(summary);
  for (const id of state.charts) updateCard(cards.get(id), retry);
  if (primaryCard) renderSummary(primaryCard);
  updateBulkExport();
}

get("dashboard-dates").addEventListener("submit", event => {
  event.preventDefault();
  dateChange({ immediate: true });
});
dateRows.addEventListener("input", () => dateChange());
addDateInterval.addEventListener("click", () => {
  const rows = readDateRows();
  if (rows.length >= maxDateIntervals) return;
  setDateError("");
  setStatus(get("date-result"), "");
  renderDateRows([...rows, { start: "", end: "" }]);
  dateRows.lastElementChild.querySelector("input").focus();
});
dateRows.addEventListener("click", event => {
  const remove = event.target.closest("[data-remove-date]");
  if (!remove || dateRows.children.length < 2) return;
  const index = Number(remove.dataset.removeDate);
  setDateError("");
  renderDateRows(readDateRows().filter((_, position) => position !== index));
  dateChange({ immediate: true });
  dateRows.children[Math.min(index, dateRows.children.length - 1)].querySelector("input").focus();
});

get("all-dates").addEventListener("click", () => replaceDates(bounds.start, bounds.end));
get("reset-dates").addEventListener("click", () => replaceDates(defaultState.start, defaultState.end));
openStations.addEventListener("click", chooseStations);
clearStations.addEventListener("click", () => {
  updateStations([]);
  openStations.focus();
});
for (const button of modeButtons) {
  button.addEventListener("click", () => {
    state.mode = button.dataset.mode;
    saveState();
    renderMode();
    updateCharts();
  });
}
grainInput.addEventListener("change", () => {
  state.grain = grainInput.value;
  saveState();
  updateCharts();
});
get("open-chart-catalog").addEventListener("click", () => catalog.showModal());
catalog.addEventListener("click", event => {
  if (event.target === catalog || event.target.closest("[data-close-catalog]")) catalog.close();
  const toggle = event.target.closest("[data-toggle-chart]");
  if (toggle) toggleChart(toggle.dataset.toggleChart);
});

async function start() {
  renderDateRows(state.intervals);
  drawCatalog();
  updateCatalog();
  renderMode();
  updateCharts();
  try {
    await loadStationCatalog();
    const available = new Set(stations.map(station => station.id));
    updateStations(state.selected.filter(id => available.has(id)), false);
  } catch {
    renderStations();
    setStatus(stationError, "Не удалось загрузить список станций. Нажмите «Выбрать станции», чтобы повторить запрос.");
  } finally {
    waitingForStations = false;
    openStations.disabled = false;
    updateCharts();
  }
}

window.addEventListener("pagehide", () => {
  if (addressTimer !== undefined) saveAddress();
});
async function refreshPublishedData() {
  try {
    await loadStationCatalog();
    coverageOverview.refresh();
    updateExtraPanels({refresh: true});
    for (const card of cards.values()) card.key = undefined;
    updateCharts();
  } catch {
    setStatus(stationError, "Не удалось обновить список станций.");
  }
}
window.addEventListener("pageshow", event => {
  if (event.persisted) refreshPublishedData();
});
window.addEventListener("storage", event => {
  if (event.key === "meteoarchive.dataRevision") refreshPublishedData();
});

const timeline = mountTimeline(archiveRange, editTimeline);
const stationOverview = mountStationOverview(get("station-overview"), {
  onChange: updateStations,
  onExpand: chooseStations,
});
const coverageToggle = get("show-coverage");
const coverageOverview = mountCoverageOverview(get("coverage-overview"));
const indicators = mountObservationIndicators({
  catalog: get("indicator-catalog"), container: get("observation-indicators"), onChange: updateCatalog,
});
const coverageKey = "meteoarchive.observations.coverage";
try {
  coverageToggle.checked = localStorage.getItem(coverageKey) === "1";
} catch {}
function showCoverage() {
  get("coverage-overview").hidden = !coverageToggle.checked;
  coverageOverview.setEnabled(coverageToggle.checked);
}
coverageToggle.addEventListener("change", () => {
  try { localStorage.setItem(coverageKey, coverageToggle.checked ? "1" : "0"); } catch {}
  showCoverage();
  updateExtraPanels();
  updateCatalog();
});
function updateExtraPanels(options = {}) {
  const dates = {start: state.start, end: state.end, intervals: state.intervals};
  coverageOverview.update({...dates, stations: state.selected, chart: state.charts[0]});
  indicators.update({...options, ...dates, stations: stations.filter(station => state.selected.includes(station.id))});
}
showCoverage();
start();
