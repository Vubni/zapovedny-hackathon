const byId = (id) => document.getElementById(id);
const form = byId("review-form");
const fields = byId("review-fields");
const station = byId("review-station");
const parameter = byId("review-param");
const startDate = byId("review-start");
const endDate = byId("review-end");
const fileInput = byId("review-file");
const fill = byId("review-fill");
const timezone = byId("review-timezone");
const subhour = byId("review-subhour");
const pressureReference = byId("review-pressure-reference");
const conflict = byId("review-conflict");
const pressure = byId("review-pressure");
const conflictFilter = form.querySelector('[value="conflicts"]');
const buddyFilter = form.querySelector('[value="buddy"]');
const status = byId("review-status");
const results = byId("review-results");
const resultTitle = byId("review-title");
const download = byId("review-download");
const originalDownload = byId("review-download-original");
const previous = byId("review-prev");
const next = byId("review-next");
const dailyButton = byId("review-daily");
const monthlyButton = byId("review-monthly");
const numberFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const monthFormat = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric", timeZone: "UTC" });
const defaultDates = { start: "2024-04-01", end: "2024-04-05" };
const datesBySource = new Map();
const uploadLimit = 12 * 1024 * 1024;
const fileTypes = {
  csv: "text/csv",
  json: "application/json",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};
const estimateReasons = {
  ambiguous_local_time: "Местное время нельзя сопоставить с единственным часом по UTC.",
  duplicate_term: "Оценка недоступна для конфликтующих записей.",
  fewer_than_two_sources: "Для срока не хватило данных из двух внешних источников.",
  incomplete_day: "Оценка недоступна до завершения суток.",
  subhourly: "Промежуточная минута. Часовые оценки сюда не переносятся.",
  unknown_pressure_reference: "Для оценки подтвердите давление на высоте станции.",
  unsupported_parameter: "Оценки этого параметра недоступны.",
};
let stations = [];
let source = "archive";
let stationId = "kr1";
let report = null;
let grain = "daily";
let paging = null;

function setStatus(element, message, error = false) {
  element.textContent = message;
  element.dataset.error = String(error);
}

function showParameters(preferred = parameter.value) {
  const available = stations.find(({ id }) => id === station.value).params;
  parameter.replaceChildren(...available.map(({ id, label, unit }) => new Option(label + " · " + unit, id)));
  if (available.some(({ id }) => id === preferred)) {
    parameter.value = preferred;
  }
}

function dateKey() {
  return source === "archive" ? stationId : "file";
}

function syncDates() {
  const isArchive = source === "archive";
  const selected = stations.find(({ id }) => id === stationId);
  for (const input of [startDate, endDate]) {
    input.min = isArchive ? selected.start : "0001-01-01";
    input.max = isArchive ? selected.end : "9999-12-31";
    input.required = isArchive;
  }
  const defaults = isArchive
    ? (selected.start <= defaultDates.start && defaultDates.end <= selected.end ? defaultDates : {
        start: selected.end.slice(0, 4) + "-04-01", end: selected.end.slice(0, 4) + "-04-05",
      })
    : { start: "", end: "" };
  const dates = datesBySource.get(dateKey()) ?? defaults;
  startDate.value = dates.start;
  endDate.value = dates.end;
}

function syncControls() {
  const isFile = source === "file";
  const isPressure = parameter.value === "pressure_hpa";
  const isBuddy = !isFile && station.value !== "ame60" && parameter.value === "t_air";
  byId("review-file-field").hidden = !isFile;
  fileInput.disabled = !isFile;
  fileInput.required = isFile;
  byId("review-period-hint").textContent = isFile
    ? "Укажите обе даты или оставьте их пустыми, чтобы взять весь период файла."
    : "Можно выбирать несколько лет. До 60 000 исходных строк в одной детальной книге.";
  conflict.disabled = !conflictFilter.checked;
  byId("review-conflict-hint").hidden = conflictFilter.checked;
  byId("review-pressure-field").hidden = !isPressure;
  pressure.disabled = !isPressure;
  buddyFilter.disabled = !isBuddy;
  byId("review-wind-hint").hidden = parameter.value !== "wind_dir";
  timezone.disabled = !fill.checked;
  timezone.required = fill.checked;
  byId("review-fill-active").hidden = !fill.checked;
  byId("review-subhour-field").hidden = !fill.checked;
  subhour.disabled = !fill.checked;
  const askPressureReference = fill.checked && isPressure;
  byId("review-pressure-reference-field").hidden = !askPressureReference;
  pressureReference.disabled = !askPressureReference;
}

function rememberDates() {
  datesBySource.set(dateKey(), { start: startDate.value, end: endDate.value });
}

function resetExternalConfirmation() {
  timezone.value = "";
  pressureReference.checked = false;
  if (fill.checked) {
    byId("review-estimate-options").open = true;
  }
}

function asNumber(value) {
  return Number.isFinite(value) ? numberFormat.format(value) : "—";
}

function asDate(value) {
  return value ? value.slice(0, 10).split("-").reverse().join(".") : "—";
}

function dateRange(first, last) {
  return first === last ? asDate(first) : asDate(first) + " — " + asDate(last);
}

function metric(label, value, note) {
  const item = document.createElement("div");
  const title = document.createElement("dt");
  const number = document.createElement("dd");
  title.textContent = label;
  number.className = "metric-value";
  number.textContent = asNumber(value);
  item.append(title, number);
  if (note) {
    const detail = document.createElement("dd");
    detail.className = "metric-note";
    detail.textContent = note;
    item.append(detail);
  }
  return item;
}

function cell(row, text, className = "", type = "td") {
  const element = document.createElement(type);
  element.className = className;
  element.textContent = text;
  if (type === "th") {
    element.scope = "row";
  }
  row.append(element);
  return element;
}

function cellNote(element, text) {
  if (!text) {
    return;
  }
  const note = document.createElement("span");
  note.className = "review-cell-note";
  note.textContent = text;
  element.append(note);
}

function emptyRow(columns, text) {
  const row = document.createElement("tr");
  cell(row, text, "muted").colSpan = columns;
  return row;
}

function rowDecision(row, keep, synthetic) {
  const decision = synthetic ? "Пропуск" : keep ? "Принято" : "Исключено";
  const color = synthetic ? "muted" : keep ? "review-accepted-text" : "review-rejected-text";
  return cell(row, decision, "review-decision " + color);
}

function externalNotes(external) {
  if (!external) {
    return [];
  }
  const notes = [];
  if (external.estimated_count > 0) {
    notes.push("Добавлено внешних оценок · " + asNumber(external.estimated_count) + ". Они вынесены в отдельный столбец.");
  }
  if (external.subhour === "interpolate") {
    notes.push("Включена интерполяция к минуте. В журнале она отмечена отдельно от средних за час.");
  }
  if (external.unavailable_count > 0) {
    notes.push("Сроков без внешней оценки · " + asNumber(external.unavailable_count) + ".");
  }
  if (external.timezone) {
    const localTime = external.timezone === "Europe/Moscow" ? "московское время (UTC+3)" : "UTC";
    notes.push("Часовой пояс исходных сроков · " + localTime + ".");
  }
  if (estimateReasons[external.reason]) {
    notes.push(estimateReasons[external.reason]);
  }
  return notes;
}

function rowElement(observation) {
  const row = document.createElement("tr");
  row.classList.toggle("review-excluded", !observation.keep && !observation.synthetic);
  const time = observation.time ?? "";
  cell(row, [asDate(time), time.slice(11)].filter(Boolean).join(" "), "", "th");
  const original = observation.original?.[report.param];
  cell(row, original === undefined || original === null || original === "" ? "—" : String(original), "num review-original");
  rowDecision(row, observation.keep, observation.synthetic);
  const working = cell(row, asNumber(observation.keep ? observation.value : observation.estimate), "num");
  if (!observation.keep && Number.isFinite(observation.estimate)) cellNote(working, "Оценка");
  const reason = cell(row, observation.reasons?.join(" ") || "—", "review-reason");
  if (observation.original?.qc_flag) {
    cellNote(reason, "Исходные флаги · " + observation.original.qc_flag);
  }
  const official = rowDecision(row, observation.official_keep, observation.synthetic);
  if (!observation.synthetic && observation.official_reasons?.length) {
    cellNote(official, observation.official_reasons.join(" "));
  }
  if (report.external) {
    const estimate = cell(row, asNumber(observation.estimate), "review-estimate-cell");
    cellNote(estimate, observation.estimate_sources || estimateReasons[observation.estimate_reason]);
    const origin = observation.provenance;
    if (origin?.sources?.length) {
      const details = document.createElement("details");
      const title = document.createElement("summary");
      title.textContent = "Значения источников";
      details.append(title);
      const stamp = origin.time_utc || origin.start_utc;
      if (stamp) cellNote(details, stamp.slice(0, 16).replace("T", " ") + " UTC");
      for (const source of origin.sources) {
        cellNote(details, source.model + " · " + asNumber(source.value) + " " + report.unit);
      }
      estimate.append(details);
    }
  }
  const file = observation.source?.split("/").pop();
  const fileCell = cell(row, file || "—", "review-source-cell");
  if (file && observation.source !== file) {
    fileCell.title = observation.source;
  }
  if (observation.line != null) {
    cellNote(fileCell, "Строка " + observation.line);
  }
  return row;
}

function paintPage() {
  const { rows, offset, limit, token, total_rows: total } = report;
  byId("review-estimate-heading").hidden = !report.external;
  byId("review-rows").replaceChildren(...(rows.length ? rows.map(rowElement) : [
    emptyRow(report.external ? 8 : 7, "В выбранном периоде нет строк. Укажите другой период или источник."),
  ]));
  byId("review-page").textContent = rows.length
    ? asNumber(offset + 1) + "–" + asNumber(offset + rows.length) + " из " + asNumber(total)
    : "Нет строк";
  previous.disabled = !token || offset <= 0 || Boolean(paging);
  next.disabled = !token || offset + limit >= total || Boolean(paging);
}

function periodCell(row, interval, data) {
  const element = cell(row, "");
  const headline = document.createElement("strong");
  headline.textContent = interval.valid ? "Покрытие принято" : "Покрытие неполное";
  headline.className = interval.valid ? "review-accepted-text" : "review-rejected-text";
  if (grain === "monthly" && data.partial) {
    headline.textContent = "Часть месяца";
    headline.className = "muted";
  }
  element.append(headline);
  const coverage = grain === "daily"
    ? asNumber(interval.slots) + (report.station === "ame60" ? " разных сроков" : " из 24 часов")
    : asNumber(interval.valid_days) + " из " + asNumber(data.days) + " пригодных суток";
  cellNote(element, coverage + " · " + asNumber(interval.n) + " изм.");
  if (Number.isFinite(interval.mean)) {
    const statistics = ["Среднее " + asNumber(interval.mean)];
    if (Number.isFinite(interval.min) && Number.isFinite(interval.max)) {
      statistics.push("минимум " + asNumber(interval.min), "максимум " + asNumber(interval.max));
    }
    cellNote(element, statistics.join(" · "));
  }
  cellNote(element, interval.reason);
}

function periodRow(data) {
  const row = document.createElement("tr");
  const period = grain === "daily" ? asDate(data.day) : monthFormat.format(new Date(data.start + "T00:00:00Z"));
  const name = cell(row, period, "", "th");
  if (grain === "monthly" && data.partial) {
    cellNote(name, dateRange(data.start, data.end));
  }
  cell(row, asNumber(data.raw_n), "num");
  periodCell(row, data.working, data);
  periodCell(row, data.official, data);
  return row;
}

function paintSummary() {
  const data = report.summaries[grain];
  dailyButton.setAttribute("aria-pressed", String(grain === "daily"));
  monthlyButton.setAttribute("aria-pressed", String(grain === "monthly"));
  byId("review-period-heading").textContent = grain === "daily" ? "Дата" : "Месяц";
  byId("review-summaries").replaceChildren(...(data.length ? data.map(periodRow) : [
    emptyRow(4, "За выбранный период сводка недоступна."),
  ]));
}

function showReport(data, filename) {
  report = data;
  grain = "daily";
  const stationLabel = stations.find(({ id }) => id === data.station)?.label ?? data.station;
  const selectedDates = dateRange(data.start, data.end);
  const sourceLabel = filename ? "Файл «" + filename + "»" : "Архив";
  const sourceDates = data.source_start && (data.source_start !== data.start || data.source_end !== data.end)
    ? " · в файле " + dateRange(data.source_start, data.source_end) : "";
  resultTitle.textContent = data.label + " · " + stationLabel;
  byId("review-context").textContent = sourceLabel + " · " + selectedDates + sourceDates;
  byId("review-original-heading").textContent = "Исходное, " + data.unit;
  byId("review-working-heading").textContent = "Рабочее, " + data.unit;
  byId("review-estimate-heading").textContent = "Внешняя оценка, " + data.unit;
  const { raw_n, kept, rejected, official_kept } = data.counts;
  byId("review-metrics").replaceChildren(
    metric("Исходные строки", raw_n),
    metric("Принято измерений", kept),
    metric("Исключено измерений", rejected),
    metric("Принято официально", official_kept),
  );
  const warnings = [...data.warnings, ...externalNotes(data.external)];
  const warningList = byId("review-warnings");
  warningList.replaceChildren(...warnings.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  warningList.hidden = !warnings.length;
  const hasOriginal = Boolean(data.token && data.original_workbook);
  download.hidden = !data.token;
  originalDownload.hidden = !hasOriginal;
  byId("review-excel-original").hidden = !hasOriginal;
  if (data.token) {
    const token = new URLSearchParams({ token: data.token });
    const filename = [data.station, data.param, data.start, data.end].join("_") + ".xlsx";
    download.href = "/api/review.xlsx?" + token;
    download.download = "review_" + filename;
    if (hasOriginal) {
      originalDownload.href = "/api/review-original.xlsx?" + token;
      originalDownload.download = "original_" + filename;
    }
  }
  setStatus(byId("review-download-status"), "");
  paintPage();
  paintSummary();
  setStatus(byId("review-page-status"), "");
  byId("review-stale").hidden = true;
  results.hidden = false;
  resultTitle.focus();
}

async function responseData(response, fallback) {
  const contentType = response.headers.get("Content-Type") ?? "";
  const data = contentType.includes("json") ? await response.json() : null;
  if (!response.ok || !data) {
    throw new Error(data?.error || fallback);
  }
  return data;
}

function validationError(message, input) {
  setStatus(status, message, true);
  input.focus();
}

function selection() {
  if (Boolean(startDate.value) !== Boolean(endDate.value)) {
    validationError("Укажите обе даты или оставьте обе пустыми.", startDate.value ? endDate : startDate);
    return null;
  }
  if (startDate.value) {
    const days = (endDate.valueAsNumber - startDate.valueAsNumber) / 86_400_000 + 1;
    if (days <= 0) {
      validationError("Дата окончания должна быть не раньше даты начала.", endDate);
      return null;
    }
    if (fill.checked && days > 31) {
      validationError("Для заполнения внешними оценками выберите период до 31 суток включительно.", endDate);
      return null;
    }
  }
  const file = source === "file" ? fileInput.files[0] : null;
  const extension = file?.name.split(".").pop().toLowerCase();
  if (source === "file" && (!file || !Object.hasOwn(fileTypes, extension))) {
    validationError("Выберите файл с расширением .csv, .json или .xlsx.", fileInput);
    return null;
  }
  if (file && file.size > uploadLimit) {
    validationError("Размер файла превышает 12 МиБ.", fileInput);
    return null;
  }
  const selectedFilters = [...form.querySelectorAll('input[name="filter"]:checked:enabled')].map(({ value }) => value);
  const query = new URLSearchParams({
    station: station.value,
    param: parameter.value,
    start: startDate.value,
    end: endDate.value,
    filters: selectedFilters.join(","),
    conflict_policy: conflictFilter.checked ? conflict.value : "all",
    fill: fill.checked ? "1" : "0",
  });
  if (!pressure.disabled) {
    query.set("pressure_max", pressure.value);
  }
  if (fill.checked) {
    query.set("timezone", timezone.value);
    query.set("subhour", subhour.checked ? "interpolate" : "skip");
    if (!pressureReference.disabled && pressureReference.checked) {
      query.set("pressure_reference", "surface");
    }
  }
  if (file) {
    query.set("filename", file.name);
  }
  return {
    query,
    filename: file?.name,
    options: file ? { method: "POST", headers: { "Content-Type": fileTypes[extension] }, body: file } : { method: "POST" },
  };
}

async function calculate() {
  const request = selection();
  if (!request) {
    return;
  }
  if (paging) {
    paging.abort();
    paging = null;
    paintPage();
  }
  fields.disabled = true;
  const submit = byId("review-submit");
  submit.textContent = "Фильтруем…";
  setStatus(status, fill.checked ? "Фильтруем измерения и подбираем внешние оценки…" : "Проверяем измерения…");
  try {
    const response = await fetch("/api/review?" + request.query, request.options);
    const data = await responseData(response, "Не удалось отфильтровать измерения.");
    setStatus(status, "");
    showReport(data, request.filename);
  } catch (error) {
    const message = error instanceof TypeError ? "Не удалось связаться с локальным сервером." : error.message;
    setStatus(status, message, true);
  } finally {
    fields.disabled = false;
    submit.textContent = "Отфильтровать";
  }
}

async function changePage(direction) {
  if (!report || paging || fields.disabled) {
    return;
  }
  const controller = new AbortController();
  const { token, limit, offset } = report;
  const query = new URLSearchParams({ token, offset: String(Math.max(0, offset + direction * limit)) });
  const pageStatus = byId("review-page-status");
  paging = controller;
  previous.disabled = true;
  next.disabled = true;
  setStatus(pageStatus, "Загружаем строки…");
  try {
    const response = await fetch("/api/review?" + query, { signal: controller.signal });
    const page = await responseData(response, "Не удалось открыть страницу журнала.");
    if (paging === controller) {
      report = { ...report, ...page };
      setStatus(pageStatus, "");
    }
  } catch (error) {
    if (paging === controller) {
      setStatus(pageStatus, error.message, true);
    }
  } finally {
    if (paging === controller) {
      paging = null;
      paintPage();
      byId("review-rows").closest(".table-wrap").scrollTop = 0;
    }
  }
}

async function downloadExcel(event) {
  event.preventDefault();
  const sourceLink = event.currentTarget;
  if (sourceLink.getAttribute("aria-disabled") === "true" || !report?.token) {
    return;
  }
  const url = sourceLink.href;
  const filename = sourceLink.download;
  const feedback = byId("review-download-status");
  sourceLink.setAttribute("aria-disabled", "true");
  setStatus(feedback, "Готовим книгу Excel…");
  try {
    const response = await fetch(url);
    if (!response.ok) {
      await responseData(response, "Не удалось подготовить книгу Excel.");
    }
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(await response.blob());
    link.href = objectUrl;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    setStatus(feedback, "");
  } catch (error) {
    const message = error instanceof TypeError ? "Не удалось связаться с локальным сервером." : error.message;
    setStatus(feedback, message, true);
  } finally {
    sourceLink.removeAttribute("aria-disabled");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  calculate();
});

form.addEventListener("invalid", (event) => {
  if (event.target === timezone) {
    byId("review-estimate-options").open = true;
  }
}, true);

form.addEventListener("change", (event) => {
  const target = event.target;
  if (target.name === "source" && target.checked) {
    rememberDates();
    source = target.value;
    resetExternalConfirmation();
    syncDates();
  }
  if (target === station) {
    rememberDates();
    stationId = station.value;
    resetExternalConfirmation();
    showParameters();
    syncDates();
  }
  if (target === fileInput) {
    resetExternalConfirmation();
  }
  syncControls();
  setStatus(status, "");
  byId("review-stale").hidden = !report;
});

previous.addEventListener("click", () => changePage(-1));
next.addEventListener("click", () => changePage(1));
download.addEventListener("click", downloadExcel);
originalDownload.addEventListener("click", downloadExcel);
byId("review-fill-example").addEventListener("click", () => {
  rememberDates();
  source = "archive";
  form.querySelector('[name="source"][value="archive"]').checked = true;
  stationId = "kr1";
  station.value = stationId;
  datesBySource.set(stationId, { start: "2024-04-21", end: "2024-04-21" });
  syncDates();
  showParameters("t_air");
  fill.checked = true;
  timezone.value = "Europe/Moscow";
  subhour.checked = false;
  pressureReference.checked = false;
  syncControls();
  byId("review-stale").hidden = !report;
  calculate();
});
document.querySelector('a[href="#review-excel"]').addEventListener("click", () => {
  byId("review-excel").open = true;
});
for (const [button, mode] of [[dailyButton, "daily"], [monthlyButton, "monthly"]]) {
  button.addEventListener("click", () => {
    grain = mode;
    paintSummary();
  });
}

async function init() {
  try {
    const response = await fetch("/api/meta");
    ({ stations } = await responseData(response, "Не удалось получить список станций."));
    station.replaceChildren(...stations.map(({ id, label }) => new Option(label, id)));
    station.value = stationId;
    showParameters("pressure_hpa");
    syncDates();
    syncControls();
    fields.disabled = false;
    setStatus(status, "");
  } catch {
    setStatus(status, "Не удалось получить список станций. Проверьте локальный сервер и обновите страницу.", true);
  }
}

init();
