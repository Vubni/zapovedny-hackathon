"use strict";

const element = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ru-RU");
const temperature = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 4});
const labels = {warming: "Из минуса в плюс", cooling: "Из плюса в минус"};

function endpoint(path, query) {
  return `${path}?${new URLSearchParams(query)}`;
}

function addCell(row, content, {header = false, numeric = false} = {}) {
  const cell = document.createElement(header ? "th" : "td");
  if (header) cell.scope = "row";
  if (numeric) cell.className = "num";
  cell.append(content);
  row.append(cell);
  return cell;
}

function rawLink(station, day) {
  const link = document.createElement("a");
  link.href = endpoint("/api/raw", {station, param: "t_air", day});
  link.className = "event-day";
  link.textContent = day;
  link.setAttribute("aria-label", `Исходные измерения ${station} за ${day}`);
  return link;
}

function addReading(row, station, day, mean) {
  const value = document.createElement("span");
  value.className = "event-value";
  const formatted = Math.abs(mean) < 0.0001 ? mean.toExponential(2).replace(".", ",") : temperature.format(mean);
  value.textContent = `${formatted} °C`;
  addCell(row, rawLink(station, day)).append(value);
}

function show(result) {
  const {station, start, end, stations, events} = result;
  element("event-station").value = station;
  element("event-start").value = start;
  element("event-end").value = end;
  element("events-period").textContent = `${start} — ${end} · Всего ${number.format(events.length)}`;
  element("events-download").href = endpoint("/api/events/export", {station, start, end});
  const stationsById = new Map(stations.map((item) => [item.station, item.label]));

  const coverage = document.createDocumentFragment();
  for (const item of stations) {
    const row = document.createElement("tr");
    addCell(row, item.label, {header: true});
    addCell(row, item.start ? `${item.start} — ${item.end}`
      : `Вне периода станции (${item.archive_start.slice(0, 4)}–${item.archive_end.slice(0, 4)})`);
    addCell(row, item.start ? `${number.format(item.valid_days)} из ${number.format(item.calendar_days)}` : "—", {numeric: true});
    addCell(row, number.format(item.warming), {numeric: true});
    addCell(row, number.format(item.cooling), {numeric: true});
    coverage.append(row);
  }
  element("events-coverage").replaceChildren(coverage);

  const rows = document.createDocumentFragment();
  for (const event of events) {
    const row = document.createElement("tr");
    const direction = document.createElement("span");
    direction.className = "event-direction";
    direction.textContent = labels[event.direction];
    addCell(row, stationsById.get(event.station), {header: true}).append(direction);
    addReading(row, event.station, event.previous_day, event.previous_mean_c);
    addReading(row, event.station, event.day, event.mean_c);
    const zeros = addCell(row, event.zero_days.length ? "" : "—");
    for (const day of event.zero_days) zeros.append(rawLink(event.station, day));
    rows.append(row);
  }
  element("events-rows").replaceChildren(rows);
  element("events-table").hidden = !events.length;
  element("events-raw-note").hidden = !events.length;
  element("events-empty").hidden = !!events.length;
  element("events-data").hidden = false;
}

async function loadEvents() {
  const query = new URLSearchParams(location.search);
  for (const field of ["station", "start", "end"]) {
    if (query.has(field)) element(`event-${field}`).value = query.get(field);
  }
  if (!element("event-station").value) element("event-station").value = "all";
  const status = element("events-status");
  status.textContent = "Читаем архив…";
  try {
    const response = await fetch(endpoint("/api/events", query));
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Не удалось получить переходы");
    show(result);
    status.textContent = "";
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Не удалось открыть выборку. ${error.message}`;
  }
}

loadEvents();
