"use strict";

const find = id => document.getElementById(id);
const form = find("model-form");
const stationSelect = find("model-station");
const modeSelect = find("model-mode");
const dayInput = find("model-day");
const yesterdayInput = find("model-yesterday");
const todayInput = find("model-today");
const submit = find("model-submit");
const retry = find("model-retry");
const statusLine = find("model-status");
const resultPanel = find("model-result");
const testResults = find("model-test-results");
const temperature = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const errorNumber = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
const countNumber = new Intl.NumberFormat("ru-RU");
const origins = { archive: "Архив", confirmed: "Подтверждённый файл", manual: "Ручной ввод" };
const sourceDays = { archive: "", manual: "" };
let stations = new Map();
let activePrediction;
let activeCatalog;

const formatDay = value => value.split("-").reverse().join(".");
const formatPeriod = (start, end) => start === end ? formatDay(start) : `${formatDay(start)} – ${formatDay(end)}`;

function formatTemperature(value) {
  return Number.isFinite(value) ? `${temperature.format(value)} °C` : "Нет данных";
}

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.dataset.error = String(error);
}

function canCalculate() {
  const station = stations.get(stationSelect.value);
  return !!station && (modeSelect.value === "manual" || !!station.archive);
}

function clearPrediction() {
  activePrediction?.abort();
  activePrediction = undefined;
  resultPanel.hidden = true;
  submit.disabled = !canCalculate();
  setStatus("");
}

function archiveHint(station) {
  if (!station.supports_temperature) {
    return "Для этой станции не настроены измерения температуры воздуха. Можно ввести две суточные температуры вручную.";
  }
  if (!station.has_observations) {
    return "У станции пока нет подтверждённых наблюдений. Загрузите и подтвердите измерения или введите две суточные температуры вручную.";
  }
  if (!station.archive) {
    return "В подтверждённых наблюдениях нет двух последовательных суток с пригодной средней температурой. Можно ввести две суточные температуры вручную.";
  }
  const { first_source_day: start, last_source_day: end } = station.archive;
  return `Доступные даты исходных наблюдений: ${formatPeriod(start, end)}. Для расчёта нужны пригодные измерения за выбранные и предыдущие сутки.`;
}

function refreshControls() {
  const station = stations.get(stationSelect.value);
  const manual = modeSelect.value === "manual";
  stationSelect.disabled = !station;
  modeSelect.disabled = !station;
  form.dataset.manual = String(manual);
  dayInput.min = manual ? "" : station?.archive?.first_source_day || "";
  dayInput.max = manual ? "" : station?.archive?.last_source_day || "";
  dayInput.value = sourceDays[modeSelect.value];
  dayInput.disabled = !canCalculate();
  submit.disabled = !canCalculate();
  for (const input of [yesterdayInput, todayInput]) {
    input.closest(".manual-input").hidden = !manual;
    input.required = manual;
    input.disabled = !manual;
  }
  find("model-hint").textContent = !station ? "" : manual
    ? "Введите средние температуры воздуха выбранной станции за два последовательных завершившихся дня по её календарю."
    : archiveHint(station);
}

function renderTestTable(title, label, rows, selected) {
  const section = element("section", "", "model-test-group");
  section.append(element("h3", title));
  const wrap = element("div", "", "weather-table");
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", title);
  wrap.tabIndex = 0;
  const table = element("table");
  table.dataset.modelTest = label;
  const heading = table.createTHead().insertRow();
  for (const [index, text] of ["Станция и даты теста", "Суток", "Модель", "Как сегодня"].entries()) {
    const cell = element("th", text, index ? "num" : "");
    cell.scope = "col";
    heading.append(cell);
  }
  const body = table.createTBody();
  for (const test of rows) {
    const row = body.insertRow();
    row.dataset.station = test.station;
    row.classList.toggle("model-test-selected", test.station === selected);
    const station = element("th", test.label);
    station.scope = "row";
    if (test.first_target_day && test.last_target_day) {
      station.append(element("span", formatPeriod(test.first_target_day, test.last_target_day), "model-test-date"));
    }
    row.append(station);
    if (!test.days) {
      const cell = row.insertCell();
      cell.colSpan = 3;
      cell.className = "model-test-unavailable";
      cell.textContent = test.reason || "Проверка не проводилась";
      continue;
    }
    const days = test.calendar_days ? `${test.days} из ${test.calendar_days}` : String(test.days);
    for (const text of [days, errorNumber.format(test.model_mae_c), errorNumber.format(test.baseline_mae_c)]) {
      const cell = row.insertCell();
      cell.className = "num";
      cell.textContent = text;
    }
  }
  wrap.append(table);
  section.append(wrap);
  return section;
}

function renderValidation({ training, tests, transfer_tests: transfer }) {
  testResults.replaceChildren();
  const station = stationSelect.value;
  if (tests.length) testResults.append(renderTestTable("Отложенные даты", "dates", tests, station));
  if (transfer.length) testResults.append(renderTestTable("Проверка переноса на исключённую станцию", "transfer", transfer, station));
  const dates = formatPeriod(training.first_target_day, training.last_target_day);
  find("model-test-note").textContent = `Общая модель обучена на архиве заповедника: ${countNumber.format(training.days)} примеров за ${dates}. Точность в других районах пока не проверялась.`;
}

function highlightTests() {
  for (const row of testResults.querySelectorAll("tbody tr")) {
    row.classList.toggle("model-test-selected", row.dataset.station === stationSelect.value);
  }
}

function renderPrediction(data) {
  const archive = data.mode === "archive";
  const baselineUsed = data.fit.kind === "baseline";
  const station = stations.get(data.station);
  const source = archive ? "Наблюдения станции" : "Ручной ввод";
  const fit = baselineUsed ? "Для этой даты доступно правило «как сегодня»."
    : `Версия модели обучена на данных по ${formatDay(data.fit.training_through)}.`;
  find("model-result-title").textContent = `Прогноз на ${formatDay(data.target_day)}`;
  find("model-result-note").textContent = `${station.label} · ${source}. ${fit}`;
  find("model-prediction-label").textContent = baselineUsed ? "Правило «как сегодня»" : "Общая модель";
  find("model-prediction").textContent = formatTemperature(data.prediction_c);
  find("model-baseline-row").hidden = baselineUsed;
  find("model-baseline").textContent = formatTemperature(data.baseline_c);
  find("model-observed-row").hidden = !archive;
  find("model-observed").textContent = formatTemperature(data.observed_c);
  find("model-inputs").replaceChildren(...data.inputs.map(input => {
    const row = element("li");
    const day = formatDay(input.day);
    if (input.origin === "archive") {
      const link = element("a", day);
      link.href = "/api/raw?" + new URLSearchParams({ station: data.station, param: "t_air", day: input.day });
      row.append(link);
    } else row.append(day);
    row.append(` · ${formatTemperature(input.temperature_c)}`);
    if (origins[input.origin]) row.append(element("span", ` · ${origins[input.origin]}`, "model-input-origin"));
    return row;
  }));
  resultPanel.hidden = false;
}

async function getJSON(url, signal) {
  const response = await fetch(url, { signal, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось получить данные модели.");
  return data;
}

async function calculate(params) {
  clearPrediction();
  const request = new AbortController();
  activePrediction = request;
  submit.disabled = true;
  setStatus("Рассчитываем прогноз…");
  try {
    const data = await getJSON(`/api/model?${params}`, request.signal);
    if (activePrediction !== request) return;
    renderPrediction(data);
    setStatus("");
  } catch (error) {
    if (activePrediction === request) {
      setStatus(error instanceof TypeError ? "Сервис расчёта недоступен. Попробуйте ещё раз." : error.message, true);
    }
  } finally {
    if (activePrediction === request) submit.disabled = !canCalculate();
  }
}

function resetArchiveDay(retain = false) {
  const archive = stations.get(stationSelect.value)?.archive;
  const day = sourceDays.archive;
  if (!retain || !archive || day < archive.first_source_day || day > archive.last_source_day) {
    sourceDays.archive = archive?.default_source_day || "";
  }
}

async function loadCatalog() {
  activeCatalog?.abort();
  clearPrediction();
  const request = new AbortController();
  activeCatalog = request;
  retry.hidden = true;
  for (const control of [stationSelect, modeSelect, dayInput, yesterdayInput, todayInput, submit]) control.disabled = true;
  setStatus("Загружаем станции и историческую проверку…");
  try {
    const data = await getJSON("/api/model/catalog", request.signal);
    if (activeCatalog !== request) return;
    const previous = stationSelect.value;
    stations = new Map(data.stations.map(station => [station.id, station]));
    stationSelect.replaceChildren(...data.stations.map(station => new Option(station.label, station.id)));
    stationSelect.value = stations.has(previous) ? previous : data.default_station;
    resetArchiveDay(stationSelect.value === previous);
    refreshControls();
    renderValidation(data);
    find("model-data").hidden = false;
    setStatus("");
    if (modeSelect.value === "archive" && canCalculate()) form.requestSubmit();
  } catch (error) {
    if (activeCatalog !== request) return;
    setStatus(error instanceof TypeError ? "Не удалось загрузить каталог станций. Повторите загрузку." : error.message, true);
    retry.hidden = false;
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  if (!canCalculate()) return;
  const params = new URLSearchParams({ station: stationSelect.value, mode: modeSelect.value, day: dayInput.value });
  if (modeSelect.value === "manual") {
    params.set("today", todayInput.value);
    params.set("yesterday", yesterdayInput.value);
  }
  calculate(params);
});

stationSelect.addEventListener("change", () => {
  clearPrediction();
  resetArchiveDay();
  sourceDays.manual = "";
  yesterdayInput.value = todayInput.value = "";
  refreshControls();
  highlightTests();
  if (modeSelect.value === "archive" && canCalculate()) form.requestSubmit();
});

modeSelect.addEventListener("change", () => {
  clearPrediction();
  refreshControls();
  if (modeSelect.value === "archive" && canCalculate()) form.requestSubmit();
});

dayInput.addEventListener("input", () => {
  sourceDays[modeSelect.value] = dayInput.value;
  clearPrediction();
});
for (const input of [todayInput, yesterdayInput]) input.addEventListener("input", clearPrediction);
retry.addEventListener("click", loadCatalog);
window.addEventListener("storage", event => {
  if (event.key === "meteoarchive.dataRevision") loadCatalog();
});
window.addEventListener("pageshow", event => {
  if (event.persisted) loadCatalog();
});

loadCatalog();
