"use strict";

const $ = (id) => document.getElementById(id);
const filters = $("filters");
const detail = $("detail");
const PAGE_SIZE = 60;
const monthNames = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const monthTicks = ["янв", "фев", "март", "апр", "май", "июнь", "июль", "авг", "сент", "окт", "нояб", "дек"];
const grainNames = { day: "Дни", dekad: "Декады", month: "Месяцы" };
const periodSingular = { day: "День", dekad: "Декада", month: "Месяц" };
const dayShort = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", timeZone: "UTC" });
const dayLong = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
const formatNumber = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const formatTrend = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const formatSigned = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2, signDisplay: "exceptZero" });
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
  setOptions($("param"), station.params, $("param").value || "pressure_hpa");

  const firstYear = Number(station.start.slice(0, 4));
  const lastYear = Number(station.end.slice(0, 4));
  for (const id of ["base-start", "base-end"]) {
    $(id).min = firstYear;
    $(id).max = lastYear;
  }
  if (resetBase || Number($("base-start").value) < firstYear || Number($("base-end").value) > lastYear) {
    $("base-start").value = firstYear;
    $("base-end").value = lastYear;
  }
  if ($("end").value < station.start || $("start").value > station.end) {
    $("start").value = `${lastYear}-01-01`;
    $("end").value = `${lastYear}-12-31`;
  }
  $("archive-range").textContent = `${station.label} · ${rangeLabel(station.start, station.end)}`;
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

async function init() {
  try {
    const data = await api("/api/meta");
    stations = data.stations;
    setOptions($("station"), stations, "kr1");
    if (!stations.length) throw new Error("В архиве нет станций");
    $("apply").disabled = false;
    updateStation();
    updateBaselineFields();
    await loadSeries();
  } catch (error) {
    setStatus("journal-status", `Не удалось открыть архив. ${error.message}. Обновите страницу.`, true);
  }
}

async function loadSeries() {
  const query = Object.fromEntries(new FormData(filters));
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
  seriesRequest?.abort();
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
  const baselineDates = `${applied.base_start}–${applied.base_end}`;
  $("metrics").innerHTML = [
    kpi(direction ? "Среднее направление" : "Среднее", number(stats.mean), numeric(stats.mean) ? series.unit : "", numeric(stats.used_days) ? `Суток в расчёте · ${number(stats.used_days)}` : `Принятых сроков · ${number(stats.n)}`),
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

  renderQuality();
  renderLedger();
  renderSeriesChart(baselineDates);
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
    "Валидные сутки", "Сроки", {label: "Контроль качества", numeric: false},
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
