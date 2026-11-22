const byId = (id) => document.getElementById(id);
const form = byId("import-form");
const fields = byId("import-fields");
const fileInput = byId("import-file");
const station = byId("import-station");
const param = byId("import-param");
const year = byId("import-year");
const start = byId("import-start");
const end = byId("import-end");
const grain = byId("import-grain");
const status = byId("import-status");
const result = byId("import-result");
const download = byId("import-download");
const exampleUrl = "/api/import/example";
const numberFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const uploadLimit = 5 * 1024 * 1024;
const fileTypes = { csv: "text/csv", json: "application/json" };
let stations = [];
let downloadUrl;

function setStatus(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
}

function clearResult() {
  result.hidden = true;
  if (downloadUrl) {
    URL.revokeObjectURL(downloadUrl);
    downloadUrl = null;
    download.removeAttribute("href");
  }
}

function showParameters(preferred = param.value) {
  const available = stations.find((entry) => entry.id === station.value).params;
  param.replaceChildren(...available.map(({ id, label, unit }) => new Option(`${label} · ${unit}`, id)));
  if (available.some(({ id }) => id === preferred)) {
    param.value = preferred;
  }
}

function setDateLimits() {
  const validYear = year.validity.valid && year.value;
  const prefix = validYear ? String(year.valueAsNumber).padStart(4, "0") : "";
  for (const input of [start, end]) {
    input.min = prefix ? `${prefix}-01-01` : "";
    input.max = prefix ? `${prefix}-12-31` : "";
  }
}

const formatted = (value) => value === null ? "—" : numberFormat.format(value);
const rangeText = (first, last) => first === last ? first : `${first} — ${last}`;

function metric(label, value) {
  const metric = document.createElement("div");
  const title = document.createElement("dt");
  const number = document.createElement("dd");
  title.textContent = label;
  number.className = "metric-value";
  number.textContent = value;
  metric.append(title, number);
  return metric;
}

function tableRow(row, data) {
  const tr = document.createElement("tr");
  tr.classList.toggle("invalid", !row.valid);
  const coverage = data.grain === "day"
    ? `${row.slots} из ${data.expected_slots} ${data.station === "ame60" ? "сроков" : "часов"}`
    : `${row.valid_days} из ${row.days} суток`;
  const cells = [row.period, coverage, formatted(row.mean), formatted(row.n), formatted(row.flagged),
    formatted(row.missing), formatted(row.conflicts), row.reason];
  for (const value of cells) {
    const cell = document.createElement("td");
    cell.textContent = value;
    tr.append(cell);
  }
  tr.lastElementChild.classList.add("cell-note");
  return tr;
}

function showResult(data, filename) {
  const stationLabel = stations.find((entry) => entry.id === data.station).label;
  byId("import-result-title").textContent = `${data.label} · ${stationLabel}`;
  byId("import-mean-heading").textContent = `Среднее, ${data.unit}`;
  byId("import-context").textContent = `Выборка ${rangeText(data.start, data.end)}. Период файла ${rangeText(data.source_start, data.source_end)}.`;
  byId("import-metrics").replaceChildren(
    metric("Строк в файле", formatted(data.raw_rows)),
    metric("Периодов принято", `${formatted(data.valid_periods)} из ${formatted(data.rows.length)}`),
    metric("Пригодных суток", formatted(data.valid_days)),
    metric("Флагов параметра в выборке", formatted(data.flagged)),
  );
  byId("import-rows").replaceChildren(...data.rows.map((row) => tableRow(row, data)));
  downloadUrl = URL.createObjectURL(new Blob([data.csv], { type: "text/csv;charset=utf-8" }));
  download.href = downloadUrl;
  download.download = data.filename;
  result.hidden = false;
  setStatus(`Рассчитан файл «${filename}».`);
}

function settings() {
  return new URLSearchParams({
    station: station.value, param: param.value, year: String(year.valueAsNumber), grain: grain.value,
    start: start.value, end: end.value,
  });
}

async function calculate(loadFile, sourceName, contentType) {
  clearResult();
  const query = settings();
  fields.disabled = true;
  setStatus("Читаем файл и проверяем покрытие…");
  try {
    const body = await loadFile();
    const response = await fetch(`/api/import?${query}`, {
      method: "POST", headers: { "Content-Type": contentType }, body,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error);
    }
    showResult(data, sourceName);
  } catch (error) {
    setStatus(error instanceof TypeError ? "Не удалось связаться с локальным сервером." : error.message, true);
  } finally {
    fields.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  const extension = file?.name.split(".").pop().toLowerCase();
  if (!extension || !Object.hasOwn(fileTypes, extension)) {
    setStatus("Выберите файл CSV или JSON.", true);
    return;
  }
  if (file.size > uploadLimit) {
    setStatus("Размер файла превышает 5 МиБ.", true);
    return;
  }
  calculate(() => file, file.name, fileTypes[extension]);
});

byId("import-example").addEventListener("click", () => {
  fileInput.value = "";
  station.value = "kr1";
  showParameters("pressure_hpa");
  year.value = "2024";
  setDateLimits();
  start.value = "";
  end.value = "";
  grain.value = "day";
  calculate(async () => {
    const response = await fetch(exampleUrl);
    if (!response.ok) {
      throw new Error("Не удалось открыть демонстрационный файл.");
    }
    return response.blob();
  }, "kr1-station-2024-04-01.json", fileTypes.json);
});

station.addEventListener("change", () => showParameters());
year.addEventListener("change", setDateLimits);
form.addEventListener("change", () => {
  clearResult();
  setStatus("");
});

async function init() {
  try {
    const response = await fetch("/api/meta");
    if (!response.ok) {
      throw new Error();
    }
    ({ stations } = await response.json());
    station.replaceChildren(...stations.map(({ id, label }) => new Option(label, id)));
    station.value = "kr1";
    showParameters("pressure_hpa");
    setDateLimits();
    fields.disabled = false;
    setStatus("");
  } catch {
    setStatus("Не удалось получить список станций. Проверьте локальный сервер и обновите страницу.", true);
  }
}

init();
