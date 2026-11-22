"use strict";

const $ = (id) => document.getElementById(id);
const filters = $("filters");
const detail = $("detail");
const PAGE_SIZE = 60;
const monthNames = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const monthTicks = ["янв", "фев", "март", "апр", "май", "июнь", "июль", "авг", "сент", "окт", "нояб", "дек"];
const grainNames = { day: "Дни", dekad: "Декады", month: "Месяцы" };
const periodSingular = { day: "День", dekad: "Декада", month: "Месяц" };
const linkKeys = ["station", "param", "start", "end", "grain", "base_start", "base_end", "view", "group"];
const dayShort = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", timeZone: "UTC" });
const dayLong = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
const formatNumber = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const formatTrend = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const formatSigned = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2, signDisplay: "exceptZero" });
const yearPlural = new Intl.PluralRules("ru");
const arrow = '<svg class="icon" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let stations = [];
let applied;
let series;
let comparison;
let mode = "series";
let compareMode = "months";
let page = 0;
let seriesRequest;
let detailRequest;
let comparisonRequest;
let openedPeriod;
let detailTrigger;
let activeView = "journal";

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function numeric(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function number(value) {
  return numeric(value) ? formatNumber.format(value) : "—";
}

function signed(value) {
  return numeric(value) ? formatSigned.format(value) : "—";
}

function unitSuffix(unit) {
  return unit === "°" || unit === "%" ? unit : unit ? ` ${unit}` : "";
}

function measurement(value, unit) {
  return numeric(value) ? `${number(value)}${unitSuffix(unit)}` : "—";
}

function dateLabel(value, formatter = dayLong) {
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? String(value ?? "") : formatter.format(date);
}

function rangeLabel(start, end) {
  if (start === end) return dateLabel(start);
  return `${dateLabel(start, start?.slice(0, 4) === end?.slice(0, 4) ? dayShort : dayLong)} — ${dateLabel(end)}`;
}

function periodLabel(row, grain = applied?.grain) {
  if (grain === "month" && /^\d{4}-\d{2}/.test(row.start)) {
    return `${monthNames[Number(row.start.slice(5, 7)) - 1]} ${row.start.slice(0, 4)}`;
  }
  if (grain === "day") return dateLabel(row.start);
  if (grain === "dekad") return rangeLabel(row.start, row.end);
  return String(row.label ?? row.period ?? "");
}

function tickLabel(row, grain, showYear) {
  if (!row.start) return row.label;
  if (grain === "month") {
    const month = monthTicks[Number(row.start.slice(5, 7)) - 1];
    return showYear ? `${month} ’${row.start.slice(2, 4)}` : month;
  }
  return showYear ? `${row.start.slice(8, 10)}.${row.start.slice(5, 7)}.${row.start.slice(2, 4)}` : dateLabel(row.start, dayShort);
}

function isDirection() {
  return series?.unit === "°" || /direction|(^|_)dir($|_)/i.test(applied?.param || "");
}

function stationLabel(id) {
  return stations.find((station) => station.id === id)?.label || id;
}

function setStatus(id, message, error = false) {
  const element = $(id);
  element.textContent = message;
  element.dataset.error = String(error);
}

async function api(path, query, signal) {
  const address = endpoint(path, query);
  const response = await fetch(address, { signal });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || payload.message || `Ответ сервера ${response.status}`);
  }
  return response.json();
}

function endpoint(path, query) {
  return query ? `${path}?${new URLSearchParams(query)}` : path;
}

function setOptions(element, options, preferred) {
  element.replaceChildren(...options.map(({id, label}) => new Option(label, id)));
  if (options.some((option) => option.id === preferred)) element.value = preferred;
  element.disabled = options.length === 0;
}

function updateStation(resetBase = false) {
  const station = stations.find((entry) => entry.id === $("station").value);
  if (!station) return;
  const previousParam = $("param").disabled ? "pressure_hpa" : $("param").value;
  setOptions($("param"), station.params, previousParam);

  const firstYear = Number(station.start.slice(0, 4));
  const lastYear = Number(station.end.slice(0, 4));
  for (const id of ["base-start", "base-end"]) {
    $(id).min = firstYear;
    $(id).max = lastYear;
  }
  if (resetBase || Number($("base-start").value) < firstYear || Number($("base-end").value) > lastYear) {
    $("base-start").value = String(firstYear);
    $("base-end").value = String(lastYear);
  }
  const startDate = $("start");
  const endDate = $("end");
  for (const element of [startDate, endDate]) {
    element.min = station.start;
    element.max = station.end;
  }
  if (endDate.value < station.start || startDate.value > station.end) {
    startDate.value = station.start > `${lastYear}-01-01` ? station.start : `${lastYear}-01-01`;
    endDate.value = station.end;
  } else {
    if (startDate.value < station.start) startDate.value = station.start;
    if (endDate.value > station.end) endDate.value = station.end;
  }
  updateBaselineCaption();
}

function updateBaselineCaption() {
  const first = $("base-start").value;
  const last = $("base-end").value;
  $("baseline-caption").textContent = first === last ? first : `${first}–${last}`;
}

function updateBaselineFields() {
  const monthly = $("grain").value === "month";
  $("baseline-options").hidden = !monthly;
  $("base-start").disabled = !monthly;
  $("base-end").disabled = !monthly;
}

function validLinkDate(value, station) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value < station.start || value > station.end) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return numeric(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function restoreLink(params) {
  if (linkKeys.some((key) => params.getAll(key).length > 1)) {
    throw new Error("В ссылке повторяется один из фильтров");
  }
  const view = params.get("view") ?? "journal";
  const group = params.get("group") ?? "months";
  if (!["journal", "compare"].includes(view)) throw new Error("В ссылке указан неизвестный раздел");
  if (params.has("group") && (view !== "compare" || !["months", "hours"].includes(group))) {
    throw new Error("Для сравнения можно выбрать группировку по месяцам или по часам");
  }
  const station = stations.find((entry) => entry.id === (params.get("station") ?? $("station").value));
  if (!station) throw new Error("В ссылке указана неизвестная станция");
  $("station").value = station.id;
  updateStation(params.has("station"));
  const param = params.get("param") ?? $("param").value;
  if (!station.params.some((entry) => entry.id === param)) throw new Error("Станция не измеряет показатель, указанный в ссылке");
  const grain = params.get("grain") ?? $("grain").value;
  if (!Object.hasOwn(grainNames, grain)) throw new Error("В ссылке указан неизвестный шаг");

  const linkedStart = params.get("start");
  const linkedEnd = params.get("end");
  for (const value of [linkedStart, linkedEnd]) {
    if (value !== null && !validLinkDate(value, station)) throw new Error("Дата в ссылке должна быть календарной и входить в архив станции");
  }
  const defaultStart = linkedEnd ? `${linkedEnd.slice(0, 4)}-01-01` : $("start").value;
  const defaultEnd = linkedStart ? `${linkedStart.slice(0, 4)}-12-31` : $("end").value;
  const startDate = linkedStart ?? (defaultStart < station.start ? station.start : defaultStart);
  const endDate = linkedEnd ?? (defaultEnd > station.end ? station.end : defaultEnd);
  if (startDate > endDate) throw new Error("Дата начала в ссылке позже даты окончания");

  const baseStart = params.get("base_start") ?? $("base-start").value;
  const baseEnd = params.get("base_end") ?? $("base-end").value;
  const firstYear = station.start.slice(0, 4);
  const lastYear = station.end.slice(0, 4);
  if ([baseStart, baseEnd].some((value) => !/^\d{4}$/.test(value) || value < firstYear || value > lastYear) || baseStart > baseEnd) {
    throw new Error(`Годы базы в ссылке должны идти по порядку и входить в диапазон ${firstYear}–${lastYear}`);
  }
  $("param").value = param;
  $("grain").value = grain;
  $("start").value = startDate;
  $("end").value = endDate;
  $("base-start").value = baseStart;
  $("base-end").value = baseEnd;
  updateBaselineCaption();
  updateBaselineFields();
  const requestedBase = params.has("base_start") || params.has("base_end") ? {base_start: baseStart, base_end: baseEnd} : {};
  return {view, group, requestedBase};
}

function persistSelection() {
  if (activeView === "journal" && (!applied || $("journal-data").hidden)) return;
  if (activeView === "compare" && !comparison) return;
  const params = new URLSearchParams(applied);
  if (activeView === "compare") {
    params.set("view", "compare");
    params.set("group", compareMode);
  }
  const search = `?${params}`;
  if (location.search !== search) history.replaceState(history.state, "", `${location.pathname}${search}${location.hash}`);
}

async function init() {
  const initialSearch = location.search;
  try {
    const data = await api("/api/meta");
    stations = data.stations;
    setOptions($("station"), stations, "kr1");
    if (!stations.length) throw new Error("В архиве нет станций");
    $("apply").disabled = false;
    updateStation();
    updateBaselineFields();
  } catch (error) {
    setStatus("journal-status", `Не удалось открыть архив. ${error.message}. Обновите страницу.`, true);
    return;
  }
  let link;
  try {
    link = restoreLink(new URLSearchParams(initialSearch));
  } catch (error) {
    setStatus("journal-status", `${error.message}. Проверьте фильтры и нажмите «Показать».`, true);
    return;
  }
  if (link.view === "compare") {
    compareMode = link.group;
    await Promise.all([switchView("compare", false), loadSeries({syncUrl: false, requestedBase: link.requestedBase})]);
    persistSelection();
  } else {
    await loadSeries({syncUrl: Boolean(initialSearch), requestedBase: link.requestedBase});
  }
}

function hideSeriesOutput() {
  seriesRequest?.abort();
  $("journal-view").removeAttribute("aria-busy");
  $("journal-data").hidden = true;
  $("export").removeAttribute("href");
  $("export").setAttribute("aria-disabled", "true");
}

async function loadSeries({syncUrl = true, requestedBase = {}} = {}) {
  hideSeriesOutput();
  const query = {...Object.fromEntries(new FormData(filters)), ...requestedBase};
  if (query.start > query.end) {
    setStatus("journal-status", "Дата начала должна предшествовать дате окончания или совпадать с ней.", true);
    $("start").focus();
    return;
  }
  if (query.base_start && Number(query.base_start) > Number(query.base_end)) {
    setStatus("journal-status", "Первый год базы должен предшествовать последнему или совпадать с ним.", true);
    $("baseline-options").open = true;
    $("base-start").focus();
    return;
  }
  const request = new AbortController();
  seriesRequest = request;
  $("journal-view").setAttribute("aria-busy", "true");
  setStatus("journal-status", "Загружаем наблюдения…");
  try {
    const data = await api("/api/series", query, request.signal);
    if (request.signal.aborted) return;
    applied = query;
    series = data;
    page = 0;
    if (query.grain !== "month") mode = "series";
    $("export").href = endpoint("/api/export", query);
    $("export").setAttribute("download", "");
    $("export").removeAttribute("aria-disabled");
    $("journal-data").hidden = false;
    setStatus("journal-status", "");
    renderJournal();
    if (syncUrl) persistSelection();
  } catch (error) {
    if (error.name !== "AbortError") {
      setStatus("journal-status", `Не удалось загрузить выборку. ${error.message}. Проверьте фильтры и нажмите «Показать».`, true);
    }
  } finally {
    if (seriesRequest === request) $("journal-view").removeAttribute("aria-busy");
  }
}

function kpi(label, value, unit, note) {
  return `<div><dt>${escapeHTML(label)}</dt><dd><span class="metric-value">${escapeHTML(value)}${unit ? ` <small class="metric-unit">${escapeHTML(unit)}</small>` : ""}</span><span class="metric-note">${escapeHTML(note)}</span></dd></div>`;
}

function fraction(part, total) {
  return `${number(part)} из ${number(total)}`;
}

function renderJournal() {
  const stats = series.stats;
  const direction = isDirection();
  const trend = numeric(stats.trend) && !direction;
  const multiYear = applied.start.slice(0, 4) !== applied.end.slice(0, 4);
  const yearName = {one: "год", few: "года", many: "лет", other: "года"};
  const years = multiYear && numeric(stats.years) ? ` · ${number(stats.years)} ${yearName[yearPlural.select(stats.years)]}` : "";
  const meanNote = numeric(stats.used_days) ? `Суток в расчёте · ${number(stats.used_days)}${years}` : `Принятых сроков · ${number(stats.n)}${years}`;
  $("metrics").innerHTML = [
    kpi(direction ? "Направление за выбранный период" : "Среднее за выбранный период", number(stats.mean), numeric(stats.mean) ? series.unit : "", meanNote),
    direction
      ? kpi("Принятые сроки", number(stats.n), "", "Сроки валидных суток в расчёте")
      : kpi("Минимум – максимум", numeric(stats.min) && numeric(stats.max) ? `${number(stats.min)}–${number(stats.max)}` : "—", numeric(stats.min) && numeric(stats.max) ? series.unit : "", "По значениям принятых периодов"),
    kpi("Валидные сутки", fraction(stats.valid_days, stats.days), "", "По выбранному параметру"),
    direction
      ? kpi("Принятые периоды", fraction(stats.valid_periods, stats.periods), "", grainNames[applied.grain])
      : kpi("Тренд", trend ? `${stats.trend > 0 ? "+" : ""}${formatTrend.format(stats.trend)}` : "—", trend ? stats.trend_unit || `${series.unit}/год` : "", trend ? `С учётом сезонности · ${number(stats.trend_n)} мес.` : "Нужны сопоставимые месяцы ≥ 3 лет"),
  ].join("");

  $("chart-title").textContent = series.label || "Динамика наблюдений";
  $("chart-modes").hidden = applied.grain !== "month";
  const label = `${stationLabel(applied.station)} · ${rangeLabel(applied.start, applied.end)} · ${grainNames[applied.grain].toLocaleLowerCase("ru")}`;
  $("chart-subtitle").textContent = series.unit ? `${label} · ${series.unit}` : label;
  const station = stations.find((entry) => entry.id === applied.station);
  if (station) $("archive-range").textContent = `${station.label} · ${rangeLabel(station.start, station.end)}`;

  renderQuality();
  renderLedger();
  renderSeriesChart();
}

function renderQuality() {
  const { rows, stats } = series;
  const percent = stats.days > 0 ? stats.valid_days / stats.days * 100 : null;
  const rejected = rows.map((row, index) => ({row, index})).filter(({row}) => !row.valid);
  const quality = [
    `<p class="coverage-value">${measurement(percent, "%")}</p>`,
    `<p class="coverage-description">${fraction(stats.valid_days, stats.days)} суток прошли контроль</p>`,
    numeric(percent) ? `<progress class="coverage-track" value="${percent}" max="100" aria-label="Доля валидных суток">${number(percent)}%</progress>` : "",
    `<div class="quality-divider"></div>`,
    `<p class="quality-note"><strong>${fraction(stats.valid_periods, stats.periods)}</strong> периодов приняты</p>`,
  ];
  if (rejected.length) {
    const firstInvalid = rejected.slice(0, 2);
    for (const {row, index} of firstInvalid) {
      quality.push(`<div class="quality-entry">${periodButton(row, index)}
        <p class="quality-note">${escapeHTML(row.reason || "Недостаточное покрытие")}</p></div>`);
    }
    if (rejected.length > firstInvalid.length) {
      quality.push(`<p class="quality-note quality-extra">Исключено периодов · ${number(rejected.length)}. Все периоды в таблице.</p>`);
    }
  }
  if (numeric(stats.flagged) && stats.flagged > 0) {
    quality.push(`<p class="quality-note quality-extra">Сроков с флагом параметра · ${number(stats.flagged)}</p>`);
  }
  quality.push('<p class="quality-note quality-extra"><a href="/method" target="_blank" rel="noopener" aria-label="Методика расчёта, в новой вкладке">Методика расчёта</a></p>');
  $("quality").innerHTML = quality.join("");
}

function periodButton(row, index) {
  return `<button class="row-link" type="button" data-period="${index}">${escapeHTML(periodLabel(row))}${arrow}</button>`;
}

function cell(value, className = "") {
  return className ? `<td class="${className}">${value}</td>` : `<td>${value}</td>`;
}

function numbers(values) {
  return values.map((value) => cell(number(value), "num")).join("");
}

function table(columns, records, label) {
  return `<table aria-label="${escapeHTML(label)}"><thead><tr>${columns.map((column, index) => `<th scope="col"${index > 0 && column.numeric !== false ? ' class="num"' : ""}>${escapeHTML(column.label || column)}</th>`).join("")}</tr></thead><tbody>${records.join("")}</tbody></table>`;
}

function renderLedger() {
  const { rows, unit } = series;
  const monthly = applied.grain === "month";
  const direction = isDirection();
  const unitText = unit ? `, ${unit}` : "";
  const columns = [
    periodSingular[applied.grain],
    `Среднее${unitText}`,
    ...(direction ? [] : [`Мин.${unitText}`, `Макс.${unitText}`]),
    ...(monthly ? [`База${unitText}`, `Δ${unitText}`, "Лет в базе"] : []),
    "Валидные сутки", "Пригодных значений", {label: "Контроль качества", numeric: false},
  ];
  const offset = page * PAGE_SIZE;
  const records = rows.slice(offset, offset + PAGE_SIZE).map((row, index) => {
    const difference = numeric(row.anomaly) && row.anomaly !== 0 ? row.anomaly > 0 ? " delta-positive" : " delta-negative" : "";
    return `<tr${row.valid ? "" : ' class="invalid"'}><th scope="row">${periodButton(row, index + offset)}</th>${
      numbers([row.mean, ...(direction ? [] : [row.min, row.max])])
    }${monthly ? numbers([row.baseline]) + cell(signed(row.anomaly), `num${difference}`) + numbers([row.years]) : ""}${
      cell(fraction(row.valid_days, row.days), "num") + numbers([row.n])
    }${cell(escapeHTML(row.valid ? "Принят" : row.reason || "Исключён"), "cell-note")}</tr>`;
  });
  $("row-count").textContent = `${number(rows.length)} периодов`;
  const wrap = $("ledger-table");
  const scrollLeft = wrap.scrollLeft;
  const pages = Math.ceil(rows.length / PAGE_SIZE);
  const pager = pages > 1 ? `<nav class="pager" aria-label="Страницы таблицы">
    <button class="button secondary" type="button" data-page="${page - 1}" ${page === 0 ? "disabled" : ""}>Назад</button>
    <span>${page + 1} / ${pages}</span>
    <button class="button secondary" type="button" data-page="${page + 1}" ${page + 1 === pages ? "disabled" : ""}>Далее</button>
  </nav>` : "";
  wrap.innerHTML = records.length ? table(columns, records, "Агрегаты метеонаблюдений") + pager : '<p class="chart-empty">За выбранный период нет строк.</p>';
  wrap.scrollLeft = scrollLeft;
}

function limits(values, includeZero, angular) {
  if (angular) return {low: 0, high: 360, step: 90};
  const min = Math.min(...values);
  const max = Math.max(...values);
  let low = includeZero ? Math.min(0, min) : min;
  let high = includeZero ? Math.max(0, max) : max;
  if (low === high) {
    const padding = Math.max(Math.abs(low) * .005, .1);
    low -= padding;
    high += padding;
  }
  const rawStep = (high - low) / 5;
  const order = 10 ** Math.floor(Math.log10(rawStep));
  const steps = [1, 2, 2.5, 5, 10];
  const step = steps.find((choice) => choice >= rawStep / order) * order;
  return { low: Math.floor(low / step) * step, high: Math.ceil(high / step) * step, step };
}

function runShapes(rows, predicate, left, width, top, height) {
  let path = "";
  for (let first = 0; first < rows.length;) {
    if (!predicate(rows[first])) {
      first += 1;
      continue;
    }
    let last = first + 1;
    while (last < rows.length && predicate(rows[last])) last += 1;
    const x = left + first * width;
    const runWidth = (last - first) * width;
    path += `M${x.toFixed(2)} ${top}h${runWidth.toFixed(2)}v${height}h-${runWidth.toFixed(2)}Z`;
    first = last;
  }
  return path;
}

function curve(rows, field, x, y, connect, discrete) {
  let marks = "";
  let stroke = "";
  let previous = false;
  for (const [index, row] of rows.entries()) {
    const value = row[field];
    if (!numeric(value)) {
      previous = false;
      continue;
    }
    const px = x(index).toFixed(2);
    const py = y(value).toFixed(2);
    if (connect) stroke += `${previous ? "L" : "M"}${px} ${py}`;
    // Keep isolated observations visible even on a dense line.
    if (discrete || !connect || (!previous && !numeric(rows[index + 1]?.[field]))) {
      const radius = discrete ? 3 : 1.7;
      marks += `M${px} ${py}m-${radius} 0a${radius} ${radius} 0 1 0 ${radius * 2} 0a${radius} ${radius} 0 1 0 -${radius * 2} 0Z`;
    }
    previous = true;
  }
  return {marks, stroke};
}

function chart(host, rows, opts) {
  host.dataset.chartWidth = host.clientWidth;
  if (!rows.length) {
    host.innerHTML = '<p class="chart-empty">За выбранный период нет строк.</p>';
    return;
  }
  // Each field is drawn as one SVG path. The node count stays bounded for daily series.
  const width = Math.max(host.clientWidth, 230);
  const height = opts.height || 235;
  const margin = {left: width < 400 ? 43 : 53, right: 12, top: 11, bottom: 29};
  const right = width - margin.right;
  const bottom = height - margin.bottom;
  const plotHeight = bottom - margin.top;
  const stepWidth = (right - margin.left) / rows.length;
  const x = (index) => margin.left + (index + .5) * stepWidth;
  const keys = opts.reference ? ["value", "reference"] : ["value"];
  const values = rows.flatMap((row) => keys.map((key) => row[key])).filter(numeric);
  const {low, high, step} = limits(values.length ? values : [0, 1], opts.bars, opts.angular);
  const y = (value) => margin.top + (high - value) / (high - low) * plotHeight;
  const nice = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 5});
  const parts = [];
  for (let tick = 0; tick <= Math.round((high - low) / step); tick += 1) {
    const value = low + step * tick;
    const pos = y(value).toFixed(2);
    const zero = Math.abs(value / step) < .001;
    parts.push(`<path class="chart-gridline${opts.bars && zero ? " zero" : ""}" d="M${margin.left} ${pos}H${right}"/>`);
    if (values.length || opts.angular) {
      parts.push(`<text x="${margin.left - 7}" y="${pos}" dy=".33em" text-anchor="end" class="chart-axis-label">${nice.format(zero ? 0 : value)}${opts.angular ? "°" : ""}</text>`);
    }
  }
  const missing = runShapes(rows, (row) => row.rejected, margin.left, stepWidth, margin.top, plotHeight);
  if (missing) parts.unshift(`<path class="chart-gap" d="${missing}"/>`);
  if (!values.length) {
    parts.push(`<text x="${(margin.left + right) / 2}" y="${margin.top + plotHeight / 2}" text-anchor="middle" class="chart-axis-label">${escapeHTML(opts.empty || "Нет пригодных значений")}</text>`);
  }
  const capacity = Math.max(2, Math.floor((right - margin.left) / opts.labelWidth));
  const skip = Math.ceil(rows.length / capacity);
  const gapMarks = [];
  const bars = {positive: "", negative: ""};
  for (const [index, row] of rows.entries()) {
    const px = x(index);
    if (index % skip === 0) {
      const labelPosition = Math.max(margin.left, Math.min(right - (opts.labelWidth / 2 - 5), px));
      parts.push(`<text x="${labelPosition}" y="${bottom + 20}" text-anchor="middle" class="chart-axis-label">${escapeHTML(row.tick)}</text>`);
    }
    if (row.rejected && stepWidth >= 9) {
      gapMarks.push(`M${px} ${bottom - 11}l4 4-4 4-4-4Z`);
    }
    if (opts.bars && numeric(row.value) && row.value !== 0) {
      const barWidth = Math.min(39, stepWidth * .67);
      const baseline = y(0);
      const end = y(row.value);
      const barHeight = Math.max(1, Math.abs(baseline - end));
      const key = row.value >= 0 ? "positive" : "negative";
      bars[key] += `M${(px - barWidth / 2).toFixed(2)} ${Math.min(baseline, end).toFixed(2)}h${barWidth.toFixed(2)}v${barHeight.toFixed(2)}h-${barWidth.toFixed(2)}Z`;
    }
  }
  if (gapMarks.length) parts.push(`<path class="chart-gap-mark" d="${gapMarks.join("")}"/>`);
  if (opts.bars) {
    for (const [key, path] of Object.entries(bars)) {
      if (path) parts.push(`<path class="chart-${key}" d="${path}"/>`);
    }
  } else {
    for (const field of opts.reference ? ["reference", "value"] : ["value"]) {
      const isReference = field === "reference";
      const { marks, stroke } = curve(rows, field, x, y, !opts.angular, stepWidth > (isReference ? 22 : 13));
      if (stroke) parts.push(`<path class="${isReference ? "chart-baseline" : "chart-line"}" d="${stroke}"/>`);
      if (marks) parts.push(`<path class="${isReference ? "chart-reference-dot" : "chart-dot"}" d="${marks}"/>`);
    }
  }
  const baseLabel = `${opts.name}. Стрелки выбирают период${opts.activate ? "; Enter открывает подробности" : ""}.`;
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="group" tabindex="0" aria-label="${escapeHTML(baseLabel)}">
    <g aria-hidden="true">${parts.join("")}
    <path class="chart-crosshair" hidden/>
    <rect class="${opts.activate ? "chart-hit" : ""}" x="${margin.left}" y="${margin.top}" width="${right - margin.left}" height="${plotHeight}" fill="transparent"/></g>
  </svg><div class="chart-tooltip" hidden></div>`;

  const svg = host.querySelector("svg");
  const hit = svg.querySelector("rect");
  const crosshair = svg.querySelector(".chart-crosshair");
  const tooltip = host.querySelector(".chart-tooltip");
  let selected = 0;

  function show(index, byKeyboard) {
    selected = index;
    const row = rows[index];
    const px = x(index);
    const label = opts.describe(row);
    crosshair.setAttribute("d", `M${px} ${margin.top}V${bottom}`);
    crosshair.removeAttribute("hidden");
    tooltip.textContent = label;
    tooltip.hidden = false;
    tooltip.style.top = `${opts.tooltipTop || 12}px`;
    tooltip.style.left = `${Math.max(0, Math.min(width - tooltip.offsetWidth, px + 10))}px`;
    if (byKeyboard) svg.setAttribute("aria-label", `${baseLabel} ${label}`);
  }

  function hide() {
    crosshair.setAttribute("hidden", "");
    tooltip.hidden = true;
  }

  function indexAt(event) {
    const box = svg.getBoundingClientRect();
    const cursor = (event.clientX - box.left) * width / box.width;
    return Math.min(rows.length - 1, Math.max(0, Math.floor((cursor - margin.left) / stepWidth)));
  }

  hit.addEventListener("pointermove", (event) => show(indexAt(event), false));
  hit.addEventListener("pointerleave", () => { if (document.activeElement !== svg) hide(); });
  if (opts.activate) hit.addEventListener("click", (event) => opts.activate(indexAt(event), svg));
  svg.addEventListener("focus", () => show(selected, true));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") selected = Math.min(rows.length - 1, selected + 1);
    else if (event.key === "ArrowLeft") selected = Math.max(0, selected - 1);
    else if (event.key === "Home") selected = 0;
    else if (event.key === "End") selected = rows.length - 1;
    else if ((event.key === "Enter" || event.key === " ") && opts.activate) opts.activate(selected, svg);
    else return;
    event.preventDefault();
    if (document.activeElement === svg) show(selected, true);
  });
}

function renderSeriesChart() {
  const anomaly = mode === "anomaly";
  const yearSpan = applied.start.slice(0, 4) !== applied.end.slice(0, 4);
  const reference = applied.grain === "month" && !anomaly && series.rows.some((row) => numeric(row.baseline));
  const rows = series.rows.map((row) => ({
    label: periodLabel(row),
    tick: tickLabel(row, applied.grain, yearSpan),
    value: anomaly ? row.anomaly : row.mean,
    reference: row.baseline,
    rejected: !row.valid,
    reason: row.reason,
    years: row.years,
  }));

  for (const button of $("chart-modes").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
  const direction = isDirection();
  const years = applied.base_start === applied.base_end ? applied.base_start : `${applied.base_start}–${applied.base_end}`;
  const marks = anomaly
    ? [["positive", direction ? "По часовой стрелке" : "Выше базы"], ["negative", direction ? "Против часовой стрелки" : "Ниже базы"]]
    : [[direction ? "point" : "current", direction ? "Среднее направление" : "Среднее"]];
  if (reference) marks.push([direction ? "reference-point" : "reference", `База ${years}`]);
  if (rows.some((row) => row.rejected)) marks.push(["gap", "Период исключён"]);
  $("series-legend").innerHTML = marks.map(([className, label]) => `<span><i class="legend-mark ${className}"></i> ${escapeHTML(label)}</span>`).join("");

  chart($("series-chart"), rows, {
    name: anomaly ? "Отклонения от базы" : series.label,
    angular: direction && !anomaly,
    bars: anomaly,
    reference,
    labelWidth: applied.grain === "month" ? (yearSpan ? 60 : 36) : 66,
    empty: anomaly ? "Отклонения не рассчитаны" : direction ? "Среднее направление не определено" : "Нет пригодных значений",
    activate: openPeriod,
    describe(row) {
      if (row.rejected) return `${row.label} · ${row.reason || "Период исключён"}`;
      if (anomaly) return numeric(row.value)
        ? `${row.label} · Δ ${signed(row.value)}${unitSuffix(series.unit)} · лет в базе ${number(row.years)}`
        : `${row.label} · отклонение недоступно`;
      if (!numeric(row.value)) return `${row.label} · среднее не определено`;
      const value = measurement(row.value, series.unit);
      return reference && numeric(row.reference)
        ? `${row.label} · ${value} · база ${measurement(row.reference, series.unit)}`
        : `${row.label} · ${value}`;
    },
  });
}

function detailMetrics(items) {
  return `<dl class="detail-metrics">${items.map(([label, value]) => `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`).join("")}</dl>`;
}

function detailHeading(title, back) {
  $("detail-title").textContent = title;
  $("detail-context").textContent = `${stationLabel(applied.station)} · ${series.label}`;
  $("detail-back").hidden = !back;
  detail.scrollTop = 0;
  $("detail-title").focus({preventScroll: true});
}

function loadDetail(path, query, render, retry) {
  detailRequest?.abort();
  const request = new AbortController();
  detailRequest = request;
  const content = $("detail-content");
  content.innerHTML = '<p class="detail-note">Загружаем наблюдения…</p>';
  api(path, query, request.signal).then((data) => {
    if (!request.signal.aborted && detail.open) render(data);
  }).catch((error) => {
    if (error.name === "AbortError" || !detail.open) return;
    content.innerHTML = `<p class="status" data-error="true">Не удалось загрузить данные. ${escapeHTML(error.message)}</p><button class="button secondary" type="button">Повторить</button>`;
    content.querySelector("button").addEventListener("click", retry, {once: true});
  });
}

function openPeriod(index, trigger) {
  const row = series.rows[index];
  if (!row) return;
  detailTrigger = trigger;
  openedPeriod = applied.grain === "day" ? null : row;
  detail.showModal();
  if (openedPeriod) showPeriodDetail();
  else showRawDay(row.start);
}

function showPeriodDetail(focusDay) {
  const row = openedPeriod;
  if (!row) return;
  detailHeading(periodLabel(row), false);
  loadDetail("/api/series", {
    station: applied.station, param: applied.param, start: row.start, end: row.end, grain: "day",
  }, (data) => {
    const metrics = detailMetrics([
      ["Валидные сутки", fraction(row.valid_days, row.days)],
      ["Пропуск подряд, суток", number(row.longest_gap)],
      ["Сроки с флагом параметра", number(row.flagged)],
      ["Пустые ячейки", number(row.missing)],
      ["Конфликтные записи", number(row.conflicts)],
    ]);
    const records = data.rows.map((day) => `<tr${day.valid ? "" : ' class="invalid"'}>
      <th scope="row"><button class="row-link" type="button" data-day="${escapeHTML(day.start)}">${escapeHTML(dateLabel(day.start, dayShort))}${arrow}</button></th>
      ${numbers([day.mean, day.n, day.flagged])}${cell(escapeHTML(day.reason), "cell-note")}</tr>`);
    const content = $("detail-content");
    content.innerHTML = `<p class="detail-note${row.valid ? "" : " rejected"}"><strong>${row.valid ? "Период принят." : "Период исключён."}</strong> ${escapeHTML(row.reason)}</p>
      ${metrics}<p class="detail-note">Откройте дату, чтобы сверить измерения со строками исходного файла.</p>
      <div class="table-wrap">${records.length ? table(["Сутки", `Среднее, ${data.unit}`, "Пригодных значений", "Флаги", {label: "Контроль качества", numeric: false}], records, "Проверка покрытия по суткам") : '<p class="chart-empty">Суточных строк за этот период нет.</p>'}</div>`;
    if (focusDay) {
      const button = [...content.querySelectorAll("[data-day]")].find((item) => item.dataset.day === focusDay);
      button?.focus();
    }
  }, () => showPeriodDetail(focusDay));
}

const rawNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 12 });

function showRawDay(day) {
  detailHeading(dateLabel(day), !!openedPeriod);
  const query = {station: applied.station, param: applied.param, day};
  loadDetail("/api/raw", query, (data) => {
    const slotLabel = applied.station === "ame60" ? "Сроки" : "Часы";
    const metrics = detailMetrics([
      [`${slotLabel} с пригодным значением`, number(data.slots)],
      [`${slotLabel} наблюдений станции`, number(data.station_slots)],
      ["Порог полноты", number(data.threshold)],
      ["Пригодных значений", number(data.n)],
      ["Среднее за сутки", measurement(data.mean, series.unit)],
    ]);
    const records = data.rows.map((sample) => `<tr${sample.status === "Пригодно" ? "" : ' class="invalid"'}>
      <th scope="row">${escapeHTML(sample.time)}</th>
      ${cell(numeric(sample.value) ? escapeHTML(rawNumber.format(sample.value)) : "—", "num")}
      ${cell(escapeHTML(sample.flags || "—"), "cell-note")}
      ${cell(escapeHTML(sample.status))}
      ${cell(numeric(sample.line) ? String(sample.line) : "—", "num")}</tr>`);
    const signals = (data.signals || []).map((signal) => `<p class="detail-note rejected"><strong>Сигнал для проверки.</strong> ${escapeHTML(signal.description)}${numeric(signal.n) ? ` Пригодных значений · ${number(signal.n)}.` : ""}</p>`).join("");
    const source = data.source ? `<p class="detail-note">Исходный файл в архиве <code class="source-path">${escapeHTML(data.source)}</code></p>` : "";
    const download = `<a class="button secondary" href="${escapeHTML(endpoint("/api/raw", {...query, download: 1}))}" download>Скачать исходные сроки · JSON</a>`;
    $("detail-content").innerHTML = `<p class="detail-note${data.valid ? "" : " rejected"}"><strong>${data.valid ? "Сутки прошли контроль полноты." : "Сутки исключены из расчёта."}</strong>
      Пригодные интервалы составили ${number(data.slots)} при пороге ${number(data.threshold)}.</p>
      ${metrics}${signals}${source}<p class="detail-note">${download}</p>
      <div class="table-wrap">${records.length ? table(["Время в архиве", `Значение, ${series.unit}`, {label: "Исходные флаги", numeric: false}, {label: "Решение для параметра", numeric: false}, "Строка CSV"], records, "Исходные сроки за сутки") : '<p class="chart-empty">В архиве нет измерений за эти сутки.</p>'}</div>`;
  }, () => showRawDay(day));
}

async function loadComparison(syncUrl = true) {
  if (comparison || comparisonRequest) return;
  const request = new AbortController();
  comparisonRequest = request;
  setStatus("compare-status", "Загружаем парные наблюдения…");
  try {
    const data = await api("/api/compare", null, request.signal);
    if (request.signal.aborted) return;
    comparison = data;
    $("compare-data").hidden = false;
    setStatus("compare-status", "");
    renderComparison();
    if (syncUrl && activeView === "compare") persistSelection();
  } catch (error) {
    if (error.name !== "AbortError") setStatus("compare-status", `Не удалось загрузить сравнение. ${error.message}. Откройте раздел повторно.`, true);
  } finally {
    if (comparisonRequest === request) comparisonRequest = null;
  }
}

function comparisonRows() {
  const monthly = compareMode === "months";
  return comparison[compareMode].map((row) => {
    const monthIndex = monthly ? Number(String(row.month).slice(-2)) - 1 : null;
    const hour = `${String(row.hour).padStart(2, "0")}:00`;
    return {
      ...row,
      value: monthly ? row.delta : row.kr1,
      reference: monthly ? null : row.kr2,
      label: monthly ? monthNames[monthIndex] : hour,
      tick: monthly ? monthTicks[monthIndex] : hour,
    };
  });
}

function renderComparison() {
  const monthly = compareMode === "months";
  const rows = comparisonRows();
  const paired = rows.reduce((sum, row) => sum + (row.pairs || 0), 0);
  const valid = rows.filter((row) => numeric(row.delta));
  const warmer = [...valid].sort((a, b) => b.delta - a.delta)[0];
  const cooler = [...valid].sort((a, b) => a.delta - b.delta)[0];
  $("compare-metrics").innerHTML = [
    kpi(monthly ? "Общие валидные сутки" : "Общие пары «дата и час»", number(paired), "", `Кр1 и Кр2 · ${comparison.year}`),
    kpi("Наибольшая разность", signed(warmer?.delta), warmer ? "°C" : "", warmer?.label || "Нет парных наблюдений"),
    kpi("Наименьшая разность", signed(cooler?.delta), cooler ? "°C" : "", cooler?.label || "Нет парных наблюдений"),
  ].join("");
  $("compare-chart-title").textContent = monthly ? "Разница температуры" : "Средний суточный ход температуры";
  $("compare-chart-subtitle").textContent = monthly
    ? `${comparison.year} · Кр1 (поляна) минус Кр2 (поле), °C`
    : `${comparison.year} · Парные наблюдения, °C · время станции`;
  $("compare-chart").nextElementSibling.innerHTML = monthly
    ? '<span><i class="legend-mark positive"></i> Кр1 теплее</span><span><i class="legend-mark negative"></i> Кр2 теплее</span>'
    : '<span><i class="legend-mark current"></i> Кр1 · поляна</span><span><i class="legend-mark reference"></i> Кр2 · поле</span>';
  $("compare-table-title").nextElementSibling.textContent = monthly
    ? "Средние температуры и число общих валидных суток."
    : "Температуры усреднены внутри часа каждой станции и сопоставлены по дате и часу.";
  for (const button of $("compare-modes").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.compare === compareMode));
  }
  const records = rows.map((row) => `<tr>
    <th scope="row">${escapeHTML(row.label)}</th>
    ${numbers([row.kr1, row.kr2])}
    ${cell(signed(row.delta), numeric(row.delta) && row.delta < 0 ? "num delta-negative" : "num delta-positive")}
    ${numbers([row.pairs])}</tr>`);
  const columns = [monthly ? "Месяц" : "Час", "Кр1, °C", "Кр2, °C", "Разница, °C", monthly ? "Общие сутки" : "Пары «дата и час»"];
  $("compare-table").innerHTML = records.length ? table(columns, records, "Сравнение температуры станций") : '<p class="chart-empty">Нет парных наблюдений.</p>';
  renderComparisonChart(rows);
}

function renderComparisonChart(rows = comparisonRows()) {
  const monthly = compareMode === "months";
  chart($("compare-chart"), rows, {
    name: monthly ? "Разница температуры Кр1 и Кр2" : "Средний суточный ход температуры Кр1 и Кр2",
    bars: monthly,
    reference: !monthly,
    height: 254,
    labelWidth: monthly ? 36 : 53,
    empty: "Нет парных наблюдений",
    describe(row) {
      const delta = numeric(row.delta) ? `${signed(row.delta)} °C` : "—";
      if (monthly) return `${row.label} · Δ ${delta} · суток ${number(row.pairs)}`;
      return `${row.label} · Кр1 ${measurement(row.kr1, "°C")} · Кр2 ${measurement(row.kr2, "°C")} · Δ ${delta} · пар ${number(row.pairs)}`;
    },
  });
}

function switchView(view, syncUrl = true) {
  activeView = view;
  const compare = view === "compare";
  $("journal-view").hidden = compare;
  $("compare-view").hidden = !compare;
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (compare && !comparison) return loadComparison(syncUrl);
  if (syncUrl) persistSelection();
}

filters.addEventListener("submit", (event) => {
  event.preventDefault();
  loadSeries();
});
filters.addEventListener("invalid", (event) => {
  hideSeriesOutput();
  const field = event.target;
  const label = field.labels?.[0]?.textContent;
  const range = field.min && field.max ? ` Допустимый диапазон ${field.min} — ${field.max}.` : "";
  setStatus("journal-status", `Проверьте поле «${label || field.name}».${range}`, true);
  if (field.name.startsWith("base_")) $("baseline-options").open = true;
}, true);
$("station").addEventListener("change", () => updateStation(true));
$("grain").addEventListener("change", updateBaselineFields);
$("base-start").addEventListener("input", updateBaselineCaption);
$("base-end").addEventListener("input", updateBaselineCaption);

document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.view) switchView(button.dataset.view);
  else if (button.dataset.mode) {
    mode = button.dataset.mode;
    renderSeriesChart();
  } else if (button.dataset.compare) {
    compareMode = button.dataset.compare;
    renderComparison();
    persistSelection();
  } else if (button.dataset.period) {
    openPeriod(Number(button.dataset.period), button);
  } else if (button.dataset.page) {
    const direction = Number(button.dataset.page) > page;
    page = Number(button.dataset.page);
    renderLedger();
    const candidate = document.querySelector(`[data-page="${page + (direction ? 1 : -1)}"]:enabled`) || document.querySelector(".pager button:enabled");
    candidate?.focus();
  } else if (button.dataset.day) {
    $("detail-back").dataset.dayBack = button.dataset.day;
    showRawDay(button.dataset.day);
  }
});

$("detail-close").addEventListener("click", () => detail.close());
$("detail-back").addEventListener("click", () => showPeriodDetail($("detail-back").dataset.dayBack));
detail.addEventListener("close", () => {
  detailRequest?.abort();
  openedPeriod = null;
  if (detailTrigger?.isConnected) detailTrigger.focus();
  detailTrigger = null;
});

const charts = new ResizeObserver((entries) => {
  for (const {target} of entries) {
    if (!target.clientWidth || Number(target.dataset.chartWidth) === target.clientWidth) continue;
    target.dataset.chartWidth = target.clientWidth;
    if (target.id === "series-chart" && series) renderSeriesChart();
    if (target.id === "compare-chart" && comparison) renderComparisonChart();
  }
});
charts.observe($("series-chart"));
charts.observe($("compare-chart"));

init();
