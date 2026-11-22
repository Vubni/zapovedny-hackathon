const byId = (id) => document.getElementById(id);
const form = byId("review-form");
const fields = byId("review-fields");
const station = byId("review-station");
const parameter = byId("review-param");
const startDate = byId("review-start");
const endDate = byId("review-end");
const fileInput = byId("review-file");
const fillExternal = byId("review-fill-external");
const fillModel = byId("review-fill-model");
const timezone = byId("review-timezone");
const subhour = byId("review-subhour");
const pressureReference = byId("review-pressure-reference");
const conflict = byId("review-conflict");
const rangeMin = byId("review-range-min");
const rangeMax = byId("review-range-max");
const filterToggles = [...form.querySelectorAll('input[name="filter"]')];
const ruleInputs = [...form.querySelectorAll("[data-rule]")];
const anomalyRules = ["hampel", "spike", "stuck"];
const conflictFilter = filterToggles.find(({ value }) => value === "conflicts");
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
const rulesByParameter = new Map();
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
const modelReasons = {
  unsupported_direction: "Модель недоступна для направления ветра.",
  unknown_cadence: "По наблюдениям не удалось определить устойчивый шаг ряда для модели.",
  no_supported_gaps: "Для модели не нашлось пропусков с подходящими соседними наблюдениями.",
  no_remaining_gaps: "После внешней оценки пропусков для модели не осталось.",
};
let stations = [];
let quality = { ranges: {}, thresholds: {} };
let source = "archive";
let stationId = "kr1";
let report = null;
let grain = "daily";
let paging = null;
let savedSource = null;

function setStatus(element, message, error = false) {
  element.textContent = message;
  element.dataset.error = String(error);
}

function showParameters(preferred = parameter.value) {
  const available = source === "file" && savedSource ? savedSource.params :
    stations.find(({ id }) => id === station.value).params;
  parameter.replaceChildren(...available.map(({ id, label, unit }) => {
    const option = new Option(label + " · " + unit, id);
    option.dataset.unit = unit;
    return option;
  }));
  if (available.some(({ id }) => id === preferred)) {
    parameter.value = preferred;
  }
  syncRules();
}

function syncRules() {
  const selected = parameter.value;
  const values = rulesByParameter.get(selected) ?? {};
  const [lower, upper] = quality.ranges[selected] ?? [];
  const defaults = { min: lower, max: upper };
  for (const id of anomalyRules) {
    defaults[id] = quality.thresholds[id]?.[selected];
  }
  for (const input of ruleInputs) {
    const id = input.dataset.rule;
    input.placeholder = defaults[id] == null ? "" : String(defaults[id]);
    if (id === "min" || id === "max") {
      input.min = quality.max_absolute_bound ? String(-quality.max_absolute_bound) : "";
    }
    input.max = String((id === "stuck" ? quality.max_stuck_hours : quality.max_absolute_bound) ?? "");
    input.value = values[id] ?? "";
  }
  const unit = parameter.selectedOptions[0]?.dataset.unit ?? "";
  byId("review-range-min-label").textContent = "Нижняя граница" + (unit ? ", " + unit : "");
  byId("review-range-max-label").textContent = "Верхняя граница" + (unit ? ", " + unit : "");
  byId("review-hampel-label").textContent = "Минимальное отклонение" + (unit ? ", " + unit : "");
  byId("review-spike-label").textContent = "Отклонение от каждого соседа" + (unit ? ", " + unit : "");
  byId("review-range-defaults").textContent = lower == null ? "" :
    "Стандартный диапазон от " + asNumber(lower) + " до " + asNumber(upper) + " " + unit + ".";
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
  const linked = isFile && Boolean(savedSource);
  const isPressure = parameter.value === "pressure_hpa";
  const hasKnownLocation = !linked || savedSource.passport.location_precision !== "unknown";
  byId("review-file-field").hidden = !isFile;
  fileInput.disabled = !isFile;
  fileInput.required = isFile && !linked;
  byId("review-saved-file").hidden = !linked;
  byId("review-saved-station").hidden = !linked;
  station.hidden = linked;
  station.disabled = linked;
  fillExternal.disabled = !hasKnownLocation;
  if (fillExternal.disabled) fillExternal.checked = false;
  byId("review-fill-location").hidden = hasKnownLocation;
  byId("review-period-hint").textContent = isFile
    ? "Укажите обе даты или оставьте их пустыми, чтобы взять весь период файла."
    : "Можно выбирать несколько лет. До 60 000 исходных строк в одной детальной книге.";
  for (const toggle of filterToggles) {
    const id = toggle.value === "conflicts" ? "conflict" : toggle.value;
    byId("review-" + id + "-field").hidden = !toggle.checked;
    byId("review-" + id).disabled = !toggle.checked;
  }
  byId("review-wind-hint").hidden = parameter.value !== "wind_dir";
  timezone.disabled = !fillExternal.checked;
  timezone.required = fillExternal.checked;
  byId("review-fill-active").hidden = !fillExternal.checked && !fillModel.checked;
  byId("review-fill-priority").hidden = !fillExternal.checked || !fillModel.checked;
  byId("review-external-options").hidden = !fillExternal.checked;
  subhour.disabled = !fillExternal.checked;
  const askPressureReference = fillExternal.checked && isPressure;
  byId("review-pressure-reference-field").hidden = !askPressureReference;
  pressureReference.disabled = !askPressureReference;
}

function rememberDates() {
  datesBySource.set(dateKey(), { start: startDate.value, end: endDate.value });
}

function resetExternalConfirmation() {
  timezone.value = "";
  pressureReference.checked = false;
  if (fillExternal.checked) {
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

function modelNotes(model) {
  if (!model) return [];
  if (model.estimated_count > 0) {
    const step = model.step_minutes ? " Шаг ряда " + asNumber(model.step_minutes) + " мин." : "";
    return ["Оценок по модели · " + asNumber(model.estimated_count) + "." + step];
  }
  return [modelReasons[model.reason] ?? "Модель не добавила оценок для выбранного ряда."];
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
  if (!observation.keep && Number.isFinite(observation.estimate)) {
    cellNote(working, observation.estimate_method === "model" ? "Модель" : "Внешние данные");
  }
  const reason = cell(row, observation.reasons?.join(" ") || "—", "review-reason");
  if (observation.original?.qc_flag) {
    cellNote(reason, "Исходные флаги · " + observation.original.qc_flag);
  }
  const official = rowDecision(row, observation.official_keep, observation.synthetic);
  if (!observation.synthetic && observation.official_reasons?.length) {
    cellNote(official, observation.official_reasons.join(" "));
  }
  if (report.external || report.model) {
    const estimate = cell(row, asNumber(observation.estimate), "review-estimate-cell");
    cellNote(estimate, observation.estimate_sources || estimateReasons[observation.estimate_reason]);
    const origin = observation.provenance;
    if (origin?.sources?.length || origin?.anchors?.length) {
      const details = document.createElement("details");
      const title = document.createElement("summary");
      title.textContent = origin.anchors ? "Опорные измерения" : "Значения источников";
      details.append(title);
      const stamp = origin.time_utc || origin.start_utc;
      if (stamp) cellNote(details, stamp.slice(0, 16).replace("T", " ") + " UTC");
      for (const source of origin.sources ?? []) {
        cellNote(details, source.model + " · " + asNumber(source.value) + " " + report.unit);
      }
      for (const anchor of origin.anchors ?? []) {
        cellNote(details, asDate(anchor.time) + " " + anchor.time.slice(11) + " · " + asNumber(anchor.value) + " " + report.unit);
      }
      if (origin.validation?.kind === "uncalibrated_interpolation") {
        cellNote(details, "Точность этой оценки не проверена на отдельных наблюдениях.");
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
  const withEstimates = Boolean(report.external || report.model);
  byId("review-estimate-heading").hidden = !withEstimates;
  byId("review-rows").replaceChildren(...(rows.length ? rows.map(rowElement) : [
    emptyRow(withEstimates ? 8 : 7, "В выбранном периоде нет строк. Укажите другой период или источник."),
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
  const stationLabel = data.station_label ?? stations.find(({ id }) => id === data.station)?.label ?? data.station;
  const selectedDates = dateRange(data.start, data.end);
  const sourceLabel = filename ? "Файл «" + filename + "»" : "Архив";
  const sourceDates = data.source_start && (data.source_start !== data.start || data.source_end !== data.end)
    ? " · в файле " + dateRange(data.source_start, data.source_end) : "";
  resultTitle.textContent = data.label + " · " + stationLabel;
  byId("review-context").textContent = sourceLabel + " · " + selectedDates + sourceDates;
  byId("review-original-heading").textContent = "Исходное, " + data.unit;
  byId("review-working-heading").textContent = "Рабочее, " + data.unit;
  byId("review-estimate-heading").textContent = "Оценка, " + data.unit;
  const { raw_n, kept, rejected, official_kept } = data.counts;
  byId("review-metrics").replaceChildren(
    metric("Исходные строки", raw_n),
    metric("Принято измерений", kept),
    metric("Исключено измерений", rejected),
    metric("Принято официально", official_kept),
  );
  const warnings = [...data.warnings, ...externalNotes(data.external), ...modelNotes(data.model)];
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
    if (fillExternal.checked && days > 31) {
      validationError("Для заполнения внешними оценками выберите период до 31 суток включительно.", endDate);
      return null;
    }
  }
  const file = source === "file" ? fileInput.files[0] : null;
  const linked = source === "file" && savedSource && !file;
  const extension = file?.name.split(".").pop().toLowerCase();
  if (source === "file" && !linked && (!file || !Object.hasOwn(fileTypes, extension))) {
    validationError("Выберите файл с расширением .csv, .json или .xlsx.", fileInput);
    return null;
  }
  if (file && file.size > uploadLimit) {
    validationError("Размер файла превышает 12 МиБ.", fileInput);
    return null;
  }
  const selected = parameter.value;
  const selectedFilters = filterToggles.filter(({ checked }) => checked).map(({ value }) => value);
  const limits = [rangeMin, rangeMax].map((input) => input.value === "" ? null : input.valueAsNumber);
  const defaults = quality.ranges[selected] ?? [];
  if ((limits[0] ?? defaults[0]) > (limits[1] ?? defaults[1])) {
    validationError("Нижняя граница должна быть не выше верхней.", rangeMin);
    return null;
  }
  const thresholds = {};
  for (const id of anomalyRules) {
    const input = byId("review-" + id);
    if (input.disabled || input.value === "") continue;
    if (input.valueAsNumber <= 0) {
      validationError("Порог дополнительной проверки должен быть больше нуля.", input);
      return null;
    }
    thresholds[id] = { [selected]: input.valueAsNumber };
  }
  const query = new URLSearchParams({
    station: station.value,
    param: selected,
    start: startDate.value,
    end: endDate.value,
    filters: selectedFilters.join(","),
    conflict_policy: conflictFilter.checked ? conflict.value : "all",
    fill_external: fillExternal.checked ? "1" : "0",
    fill_model: fillModel.checked ? "1" : "0",
  });
  if (limits.some((limit) => limit !== null)) {
    query.set("ranges", JSON.stringify({ [selected]: limits }));
  }
  if (Object.keys(thresholds).length) {
    query.set("thresholds", JSON.stringify(thresholds));
  }
  if (fillExternal.checked) {
    query.set("timezone", timezone.value);
    query.set("subhour", subhour.checked ? "interpolate" : "skip");
    if (!pressureReference.disabled && pressureReference.checked) {
      query.set("pressure_reference", "surface");
    }
  }
  if (file) {
    query.set("filename", file.name);
  }
  if (linked) query.set("file_id", savedSource.item.id);
  return {
    query,
    filename: file?.name ?? (linked ? savedSource.item.filename : undefined),
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
  setStatus(status, fillExternal.checked || fillModel.checked ? "Фильтруем измерения и подбираем оценки…" : "Проверяем измерения…");
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

form.addEventListener("input", (event) => {
  const id = event.target.dataset.rule;
  if (id) {
    const selected = parameter.value;
    const values = rulesByParameter.get(selected) ?? {};
    values[id] = event.target.value;
    rulesByParameter.set(selected, values);
    byId("review-stale").hidden = !report;
  }
});

form.addEventListener("change", (event) => {
  const target = event.target;
  if (target.name === "source" && target.checked) {
    rememberDates();
    source = target.value;
    if (source === "file" && savedSource) {
      stationId = savedSource.passport.profile;
      station.value = stationId;
    }
    showParameters();
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
    if (fileInput.files.length) savedSource = null;
    showParameters();
    resetExternalConfirmation();
  }
  if (target === parameter) {
    syncRules();
    pressureReference.checked = false;
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
  fillExternal.checked = true;
  fillModel.checked = false;
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
    const data = await responseData(response, "Не удалось получить список станций.");
    stations = data.stations;
    quality = data.quality ?? quality;
    station.replaceChildren(...stations.map(({ id, label }) => new Option(label, id)));
    station.value = stationId;
    showParameters("pressure_hpa");
    syncDates();
    syncControls();
    fields.disabled = false;
    setStatus(status, "");
    const id = new URLSearchParams(location.search).get("file");
    if (id) await openSavedSource(id);
  } catch (error) {
    setStatus(status, error instanceof TypeError ? "Не удалось связаться с локальным сервером." : error.message, true);
  }
}

async function openSavedSource(id) {
  fields.disabled = true;
  try {
    const [item, catalog] = await Promise.all([
      fetch("/api/files/item?" + new URLSearchParams({id})).then(r => responseData(r, "Исходный файл недоступен.")),
      fetch("/api/stations").then(r => responseData(r, "Не удалось получить паспорта станций.")),
    ]);
    if (item.error) throw new Error(item.error);
    const passport = catalog.stations.find(s => s.id === item.station);
    const available = passport?.params.filter(p => item.params.some(column => column.id === p.id));
    if (!available?.length) throw new Error("В файле нет показателей из паспорта станции.");
    savedSource = {item, passport, params: available};
    source = "file";
    form.querySelector('[name="source"][value="file"]').checked = true;
    stationId = passport.profile;
    station.value = stationId;
    byId("review-saved-file").textContent = "Открыт сохранённый оригинал «" + item.filename + "».";
    byId("review-saved-station").textContent = passport.label;
    const settings = item.settings ?? {};
    for (const checkbox of filterToggles) {
      checkbox.checked = settings.filters?.includes(checkbox.value) ?? false;
    }
    for (const { id: param } of available) {
      const [lower, upper] = settings.ranges?.[param] ?? [];
      const values = { min: lower, max: upper };
      if (param === "pressure_hpa" && upper == null && settings.pressure_max != null) {
        values.max = settings.pressure_max;
      }
      for (const id of anomalyRules) {
        values[id] = settings.thresholds?.[id]?.[param];
      }
      rulesByParameter.set(param, values);
    }
    conflict.value = settings.conflict_policy ?? "all";
    showParameters();
    syncDates();
    syncControls();
  } finally {
    fields.disabled = false;
  }
}

init();
