"use strict";

const el = (id) => document.getElementById(`temperature-${id}`);
const stationInput = el("station");
const yearInput = el("year");
const dataPanel = el("data");
const monthsPanel = el("months");
const status = el("status");
const dayLength = 86400000;
const monthFormat = new Intl.DateTimeFormat("ru-RU", {month: "long", timeZone: "UTC"});
const dateFormat = new Intl.DateTimeFormat("ru-RU", {dateStyle: "long", timeZone: "UTC"});
const shortDate = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "short", timeZone: "UTC"});
const number = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2});
const temperature = (mean) => `${number.format(Math.abs(mean) < 0.005 ? 0 : mean).replace("-", "−")} °C`;
const upperBounds = [-25, -15, -5, 5, 15, 25, Infinity];
const scaleLabels = ["< −25", "−25…<−15", "−15…<−5", "−5…<+5", "+5…<+15", "+15…<+25", "≥ +25"];
const weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const isUsable = (row) => row?.valid === true && Number.isFinite(row.mean);
const isoDay = (date) => date.toISOString().slice(0, 10);

const stations = new Map();
const years = new Map();
let days = new Map();
let buttons = new Map();
let selectedDay;
let fetchController;
let yearTimer;

function endpoint(path, params) {
  return `${path}?${new URLSearchParams(params)}`;
}

async function getJSON(path, signal) {
  const response = await fetch(path, {signal});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function classify(row) {
  if (isUsable(row)) {
    return {band: upperBounds.findIndex((upper) => row.mean < upper), label: temperature(row.mean)};
  }
  if (row && row.n + row.flagged + row.missing + row.conflicts > 0) {
    return {band: "invalid", label: "Не прошли контроль качества"};
  }
  return {band: "missing", label: "Нет наблюдений"};
}

function renderScale() {
  scaleLabels.forEach((label, index) => {
    const item = document.createElement("li");
    const color = document.createElement("span");
    color.className = "temperature-swatch";
    color.dataset.band = index;
    color.setAttribute("aria-hidden", "true");
    item.append(color, label);
    el("scale").append(item);
  });
}

function dateLabel(day) {
  return dateFormat.format(new Date(`${day}T00:00:00Z`));
}

function showDay(day, focus = false) {
  const chosen = buttons.get(day);
  if (!chosen) return;
  if (selectedDay && buttons.has(selectedDay)) {
    const previous = buttons.get(selectedDay);
    previous.tabIndex = -1;
    previous.setAttribute("aria-pressed", "false");
  }
  selectedDay = day;
  chosen.tabIndex = 0;
  chosen.setAttribute("aria-pressed", "true");

  const row = days.get(day);
  const {label} = classify(row);
  el("reading").textContent = `${dateLabel(day)} · ${label}`;
  el("reason").textContent = row?.reason ?? "";
  const source = el("source");
  source.hidden = !row;
  if (row) {
    source.href = endpoint("/api/raw", {station: stationInput.value, param: "t_air", day});
    source.setAttribute("aria-label", `Исходные сроки за ${dateLabel(day)} (JSON, новая вкладка)`);
  }

  const url = new URL(window.location.href);
  url.searchParams.set("station", stationInput.value);
  url.searchParams.set("year", yearInput.value);
  url.searchParams.set("day", day);
  window.history.replaceState(null, "", url);
  if (focus) chosen.focus();
}

function renderMonth(year, month) {
  const first = new Date(Date.UTC(year, month, 1));
  const title = document.createElement("h3");
  const name = monthFormat.format(first);
  title.id = `temperature-month-${month}`;
  title.textContent = name[0].toUpperCase() + name.slice(1);

  const section = document.createElement("section");
  section.className = "temperature-month";
  section.setAttribute("aria-labelledby", title.id);
  const grid = document.createElement("div");
  grid.className = "temperature-days";
  weekdays.forEach((name) => {
    const weekday = document.createElement("span");
    weekday.className = "temperature-weekday";
    weekday.setAttribute("aria-hidden", "true");
    weekday.textContent = name;
    grid.append(weekday);
  });
  const weekdayOffset = (first.getUTCDay() + 6) % 7;
  for (let i = 0; i < weekdayOffset; i++) {
    const spacer = document.createElement("span");
    spacer.setAttribute("aria-hidden", "true");
    grid.append(spacer);
  }
  const count = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  for (let day = 1; day <= count; day++) {
    const date = isoDay(new Date(Date.UTC(year, month, day)));
    const view = classify(days.get(date));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "temperature-day";
    button.dataset.day = date;
    button.dataset.band = view.band;
    button.tabIndex = -1;
    button.title = `${dateLabel(date)} · ${view.label}`;
    button.setAttribute("aria-label", button.title);
    button.setAttribute("aria-pressed", "false");
    button.textContent = day;
    buttons.set(date, button);
    grid.append(button);
  }

  section.append(title, grid);
  return section;
}

function renderExtreme(container, row) {
  if (!row) {
    container.textContent = "—";
    return;
  }
  const jump = document.createElement("button");
  jump.type = "button";
  jump.className = "temperature-extreme";
  jump.dataset.jump = row.start;
  jump.setAttribute("aria-label", `${temperature(row.mean)}, ${dateLabel(row.start)}. Показать день`);
  const value = document.createElement("span");
  value.textContent = temperature(row.mean);
  jump.append(value, ` · ${shortDate.format(new Date(`${row.start}T00:00:00Z`))}`);
  container.replaceChildren(jump);
}

function renderYear(station, year, rows, preferredDay) {
  days = new Map(rows.map((row) => [row.start, row]));
  buttons = new Map();
  const months = document.createDocumentFragment();
  for (let month = 0; month < 12; month++) months.append(renderMonth(year, month));
  monthsPanel.replaceChildren(months);
  const usable = rows.filter(isUsable);
  let minimum;
  let maximum;
  for (const row of usable) {
    if (!minimum || row.mean < minimum.mean) minimum = row;
    if (!maximum || row.mean > maximum.mean) maximum = row;
  }

  el("title").textContent = `${station.label} · ${year}`;
  el("coverage").textContent = `${usable.length} из ${buttons.size}`;
  renderExtreme(el("minimum"), minimum);
  renderExtreme(el("maximum"), maximum);
  dataPanel.hidden = false;
  dataPanel.setAttribute("aria-busy", "false");
  status.textContent = usable.length ? "" : "За выбранный год нет пригодных суток.";
  const preferred = preferredDay ? `${year}${preferredDay.slice(4)}` : null;
  showDay(buttons.has(preferred) ? preferred : (usable[0]?.start || `${year}-01-01`));
}

function setYearRange(station, year) {
  const first = +station.start.slice(0, 4);
  const last = +station.end.slice(0, 4);
  yearInput.min = first;
  yearInput.max = last;
  yearInput.value = Math.max(first, Math.min(last, year));
  el("first-year").textContent = first;
  el("last-year").textContent = last;
}

async function loadYear(preferredDay = selectedDay) {
  const station = stations.get(stationInput.value);
  const year = Number(yearInput.value);
  const key = `${station.id}/${year}`;
  const controller = new AbortController();
  fetchController?.abort();
  fetchController = controller;
  dataPanel.hidden = true;
  dataPanel.setAttribute("aria-busy", "true");
  delete status.dataset.error;
  status.textContent = "Загружаем календарь…";
  try {
    if (!years.has(key)) {
      const result = await getJSON(endpoint("/api/series", {
        station: station.id, param: "t_air", grain: "day", start: `${year}-01-01`, end: `${year}-12-31`,
      }), controller.signal);
      if (!Array.isArray(result.rows)) throw new Error("В ответе нет суточного ряда");
      years.set(key, result.rows);
    }
    if (!controller.signal.aborted) renderYear(station, year, years.get(key), preferredDay);
  } catch (error) {
    if (controller.signal.aborted) return;
    dataPanel.setAttribute("aria-busy", "false");
    status.dataset.error = "true";
    status.textContent = "Не удалось загрузить суточный ряд. Попробуйте выбрать год снова.";
  }
}

function requestYear(delay = 0) {
  window.clearTimeout(yearTimer);
  fetchController?.abort();
  dataPanel.hidden = true;
  dataPanel.setAttribute("aria-busy", "true");
  el("year-value").value = yearInput.value;
  if (delay) {
    status.textContent = "";
    yearTimer = window.setTimeout(loadYear, delay);
  } else {
    loadYear();
  }
}

const directions = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7};

monthsPanel.addEventListener("keydown", (event) => {
  const day = event.target.dataset.day;
  if (!day || !Object.hasOwn(directions, event.key) || event.altKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  const time = Date.parse(`${day}T00:00:00Z`);
  const next = isoDay(new Date(time + directions[event.key] * dayLength));
  showDay(next, true);
});

dataPanel.addEventListener("click", (event) => {
  const day = event.target.closest("[data-day], [data-jump]");
  if (!day) return;
  showDay(day.dataset.day || day.dataset.jump, Boolean(day.dataset.jump));
});

stationInput.addEventListener("change", () => {
  setYearRange(stations.get(stationInput.value), Number(yearInput.value));
  requestYear();
});

yearInput.addEventListener("input", () => requestYear(120));

async function init() {
  renderScale();
  status.textContent = "Загружаем список станций…";
  try {
    const metadata = await getJSON("/api/meta");
    for (const station of metadata.stations) {
      if (!station.params.some((param) => param.id === "t_air")) continue;
      stations.set(station.id, station);
      stationInput.add(new Option(station.label, station.id));
    }
    const query = new URLSearchParams(window.location.search);
    const station = stations.get(query.get("station")) || stations.get("kr1") || stations.values().next().value;
    if (!station) throw new Error("В архиве нет станций с температурой");
    let year = Number(query.get("year"));
    if (!/^\d{4}$/.test(query.get("year") ?? "")) year = +station.end.slice(0, 4);
    stationInput.value = station.id;
    setYearRange(station, year);
    el("year-value").value = yearInput.value;
    stationInput.disabled = false;
    yearInput.disabled = false;
    const day = query.get("day");
    await loadYear(/^\d{4}-\d{2}-\d{2}$/.test(day ?? "") ? day : undefined);
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = "Не удалось открыть список станций. Обновите страницу, чтобы повторить запрос.";
  }
}

init();
