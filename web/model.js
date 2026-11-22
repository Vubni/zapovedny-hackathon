"use strict";

const find = (id) => document.getElementById(id);
const form = find("model-form");
const stationSelect = find("model-station");
const modeSelect = find("model-mode");
const dayInput = find("model-day");
const yesterdayInput = find("model-yesterday");
const todayInput = find("model-today");
const submit = form.querySelector("button");
const statusLine = find("model-status");
const resultPanel = find("model-result");
const testRows = find("model-test-rows");
const temperature = new Intl.NumberFormat("ru-RU", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const errorNumber = new Intl.NumberFormat("ru-RU", {minimumFractionDigits: 3, maximumFractionDigits: 3});
const formatTemperature = (value) => temperature.format(value) + " °C";
const formatDay = (value) => value.split("-").reverse().join(".");
let stations = [];
let activeRequest;

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.dataset.error = String(error);
}

function updateControls(resetDay = false) {
  const manual = modeSelect.value === "manual";
  const {archive} = stations.find((station) => station.id === stationSelect.value);
  form.dataset.manual = String(manual);
  dayInput.min = archive.first_source_day;
  dayInput.max = manual ? "" : archive.last_source_day;
  if (resetDay) dayInput.value = manual ? "" : archive.default_source_day;
  for (const input of [yesterdayInput, todayInput]) {
    input.closest(".manual-input").hidden = !manual;
    input.required = manual;
    input.disabled = !manual;
    if (resetDay) input.value = "";
  }
  find("model-hint").textContent = manual
    ? "Введите средние за два последовательных завершившихся дня по московскому времени."
    : `Выберите дату с ${formatDay(archive.first_source_day)} по ${formatDay(archive.last_source_day)}. Для расчёта нужны пригодные наблюдения за этот и предыдущий день.`;
}

function renderTestTable() {
  testRows.replaceChildren();
  for (const station of stations) {
    const {test} = station;
    const row = testRows.insertRow();
    row.dataset.station = station.id;
    const label = document.createElement("th");
    label.scope = "row";
    label.textContent = station.label;
    const period = document.createElement("span");
    period.className = "model-test-date";
    period.textContent = `${formatDay(test.first_target_day)} – ${formatDay(test.last_target_day)}`;
    label.append(period);
    row.append(label);
    for (const value of [`${test.days} из ${test.calendar_days}`,
      errorNumber.format(test.model_mae_c), errorNumber.format(test.baseline_mae_c)]) {
      const cell = row.insertCell();
      cell.className = "num";
      cell.textContent = value;
    }
  }
}

function highlightTest(stationId) {
  for (const row of testRows.rows) {
    row.classList.toggle("model-test-selected", row.dataset.station === stationId);
  }
  const station = stations.find((entry) => entry.id === stationId);
  const {training, test} = station;
  const comparison = test.model_mae_c < test.baseline_mae_c ? "Модель точнее правила «как сегодня»."
    : test.model_mae_c > test.baseline_mae_c ? "Правило «как сегодня» точнее модели."
    : "Ошибки модели и правила «как сегодня» совпали.";
  find("model-test-note").textContent =
    `${station.label}. ${comparison} Обучение на ${training.days.toLocaleString("ru-RU")} сутках с ${formatDay(training.first_target_day)} по ${formatDay(training.last_target_day)}.`;
}

function renderPrediction(data) {
  const archive = data.mode === "archive";
  find("model-result-title").textContent = `Прогноз на ${formatDay(data.target_day)}`;
  find("model-result-note").textContent = archive
    ? `Архивный расчёт по наблюдениям на ${formatDay(data.source_day)} включительно.`
    : `Расчёт по введённым наблюдениям на ${formatDay(data.source_day)} включительно.`;
  find("model-prediction").textContent = formatTemperature(data.prediction_c);
  find("model-baseline").textContent = formatTemperature(data.baseline_c);
  find("model-observed-row").hidden = !archive;
  find("model-observed").textContent = data.observed_c === null
    ? "Нет данных" : formatTemperature(data.observed_c);
  find("model-inputs").replaceChildren(...data.inputs.map((input) => {
    const row = document.createElement("li");
    if (archive) {
      const source = document.createElement("a");
      source.textContent = formatDay(input.day);
      source.href = "/api/raw?" + new URLSearchParams({station: data.station, param: "t_air", day: input.day});
      row.append(source);
    } else {
      row.textContent = formatDay(input.day);
    }
    row.append(" · " + formatTemperature(input.temperature_c));
    return row;
  }));
  highlightTest(data.station);
  find("model-data").hidden = false;
  resultPanel.hidden = false;
}

async function calculate(params = new URLSearchParams()) {
  activeRequest?.abort();
  const request = new AbortController();
  activeRequest = request;
  resultPanel.hidden = true;
  submit.disabled = true;
  setStatus("Рассчитываем прогноз…");
  try {
    const response = await fetch("/api/model?" + params, {signal: request.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось рассчитать прогноз.");
    if (!stations.length) {
      stations = data.stations;
      for (const station of stations) stationSelect.add(new Option(station.label, station.id));
      stationSelect.value = data.station;
      updateControls(true);
      for (const control of [stationSelect, modeSelect, dayInput]) control.disabled = false;
      renderTestTable();
    }
    renderPrediction(data);
    setStatus("");
  } catch (error) {
    if (request.signal.aborted) return;
    setStatus(error instanceof TypeError ? "Сервис расчёта недоступен. Попробуйте ещё раз." : error.message, true);
  } finally {
    if (!request.signal.aborted) submit.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!stations.length) {
    calculate();
    return;
  }
  const params = new URLSearchParams({station: stationSelect.value, mode: modeSelect.value, day: dayInput.value});
  if (modeSelect.value === "manual") {
    params.set("today", todayInput.value);
    params.set("yesterday", yesterdayInput.value);
  }
  calculate(params);
});

function clearPrediction() {
  activeRequest?.abort();
  submit.disabled = false;
  resultPanel.hidden = true;
  setStatus("");
}

for (const control of [stationSelect, modeSelect]) {
  control.addEventListener("change", () => {
    clearPrediction();
    updateControls(true);
    highlightTest(stationSelect.value);
    if (modeSelect.value === "archive") form.requestSubmit();
  });
}
for (const input of [dayInput, todayInput, yesterdayInput]) input.addEventListener("input", clearPrediction);

calculate();
