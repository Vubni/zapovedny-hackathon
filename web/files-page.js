const main = document.querySelector(".files-main");
const picker = document.getElementById("fileInput");
const dialog = document.getElementById("files-dialog");
const numberFormats = [0, 1, 2].map((digits) => new Intl.NumberFormat("ru-RU", {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}));
const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC",
});
const glyphs = {
  filter: '<path d="M4 5h16l-6 7v5l-4 2v-7Z"/>',
  download: '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/>',
  file: '<path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10Zm0 0v7h7M8 14h8m-8 3h8"/>',
  pin: '<path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
  arrow: '<path d="M4 12h15m-5-5 5 5-5 5"/>',
  back: '<path d="M20 12H5m5-5-5 5 5 5"/>',
  upload: '<path d="M12 15V3M8 7l4-4 4 4M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
};
const parameterColors = {
  t_air: "#ab4d32", rh: "#426583", wind_speed: "#2b7650", wind_gust: "#587e1d",
  wind_dir: "#735579", pressure_hpa: "#936025", soil_05: "#8e5948",
  soil_10: "#6d6aa3", soil_20: "#377a79",
};
const state = {
  files: new Map(),
  editors: new Map(),
  stations: [],
  filters: [],
  fills: [],
  transfers: [],
  loading: true,
  uploading: false,
  exampleLoading: false,
  editor: null,
  message: "",
  error: false,
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

function fmt(value, digits = 0) {
  return Number.isFinite(value) ? numberFormats[digits].format(value).replace(/^-/, "−") : "—";
}

function countLabel(value, one = "выброс", few = "выброса", many = "выбросов") {
  const last = value % 10;
  const ending = value % 100 >= 11 && value % 100 <= 14
    ? many : last === 1 ? one : last >= 2 && last <= 4 ? few : many;
  return `${fmt(value)} ${ending}`;
}

function stamp(value) {
  const text = String(value).replace(" ", "T");
  return Date.parse(text.length === 10 || /(?:Z|[+-]\d\d:\d\d)$/.test(text) ? text : text + "Z");
}

function spanLabel(start, end) {
  if (!start || !end) return "—";
  const first = dateFormat.format(stamp(start.slice(0, 10)));
  const last = dateFormat.format(stamp(end.slice(0, 10)));
  return first === last ? first : `${first} – ${last}`;
}

function icon(name, size = 16) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${glyphs[name]}</svg>`;
}

function button(label, action, { id = "", type = "line", glyph, disabled = false } = {}) {
  return `<button type="button" class="btn ${type}" data-file-action="${action}" data-file-id="${escapeHtml(id)}" ${disabled ? "disabled" : ""}>${glyph ? icon(glyph) : ""}${escapeHtml(label)}</button>`;
}

function iconButton(label, action, id, glyph) {
  return `<button type="button" class="icon-btn" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}" data-file-action="${action}" data-file-id="${escapeHtml(id)}">${icon(glyph)}</button>`;
}

function link(label, href, { type = "line", glyph, filename } = {}) {
  return `<a class="btn ${type}" href="${escapeHtml(href)}" ${filename ? `download="${escapeHtml(filename)}"` : ""}>${glyph ? icon(glyph) : ""}${escapeHtml(label)}</a>`;
}

function tag(label, kind = "") {
  return `<span class="tag ${kind}">${escapeHtml(label)}</span>`;
}

function card(body, classes = "") {
  return `<section class="card ${classes}">${body}</section>`;
}

function heading(title, subtitle = "", action = "") {
  return `<header>
    <div class="breadcrumb">${icon("pin", 15)}<strong>Урочище Красное</strong><span>/</span><span>Центрально-Лесной заповедник</span></div>
    <div class="heading-line"><h1>${title}</h1>${action}</div>
    ${subtitle ? `<p class="sub">${subtitle}</p>` : ""}
  </header>`;
}

function cardTitle(title, subtitle = "", aside = "") {
  return `<div class="card-head"><div><h2>${title}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div>${aside}</div>`;
}

function note(message, warning = false) {
  return `<div class="note${warning ? " warn" : ""}">${icon("info")}<span>${escapeHtml(message)}</span></div>`;
}

function selectField(label, id, options, current, disabled = false) {
  return `<div class="field"><label for="${id}">${label}</label><select id="${id}" ${disabled ? "disabled" : ""}>
    ${options.map(([value, name]) => `<option value="${escapeHtml(value)}" ${current === value ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}
  </select></div>`;
}

function stationName(id) {
  return state.stations.find((station) => station.id === id)?.label ?? id ?? "—";
}

function filesUrl(endpoint = "", params = {}) {
  const query = new URLSearchParams(params);
  return `/api/files${endpoint ? `/${endpoint}` : ""}${query.size ? `?${query}` : ""}`;
}

async function jsonRequest(url, options) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Ошибка сервера (${response.status}).`);
  return result;
}

function fileStatus(file) {
  if (file.confirmed) return tag("В общих графиках");
  if (file.status === "error") return tag("Ошибка чтения", "warn");
  if (file.status === "original") return tag("Только исходник", "neutral");
  if (file.preview) return tag("Проверка результата", "neutral");
  return tag("Ожидает обработки", "neutral");
}

function fileSize(bytes) {
  return Number.isFinite(bytes) ? `${fmt(bytes / 1024, 1)} КиБ` : "";
}

function sourceButton(file, iconOnly = false) {
  return iconOnly
    ? iconButton(`Открыть исходник · ${file.filename}`, "source", file.id, "file")
    : button(`Исходный ${file.filename.toLowerCase().endsWith(".xlsx") ? "XLSX" : "CSV"}`, "source", { id: file.id });
}

function queueItem(file) {
  const error = file.status === "error";
  return `<div class="queue-item">
    <span class="file-symbol">${icon("file", 19)}</span>
    <div><strong>${escapeHtml(file.filename)}</strong>
      <div class="row">${fileStatus(file)}${file.rows ? `<span class="muted small">${escapeHtml(stationName(file.station))} · ${countLabel(file.rows, "строка", "строки", "строк")}</span>` : ""}</div>
      ${error ? `<p class="file-message muted small">${escapeHtml(file.error)}</p>` : ""}
    </div>
    ${error ? (file.id ? sourceButton(file, true) : "") : button("Настроить", "edit", { id: file.id })}
  </div>`;
}

function transferItem(transfer) {
  return `<div class="queue-item">
    <span class="file-symbol">${icon("file", 19)}</span>
    <div><strong>${escapeHtml(transfer.filename)}</strong>
      <div class="row">${tag(transfer.status === "loading" ? "Загрузка…" : "В очереди на загрузку", "neutral")}</div>
    </div>
  </div>`;
}

function historyRow(file) {
  const bytes = fileSize(file.bytes);
  return `<tr>
    <td><div class="row">${icon("file")}<div class="file-name">${escapeHtml(file.filename)}${bytes ? `<p class="muted small">${bytes}</p>` : ""}</div></div></td>
    <td>${escapeHtml(stationName(file.confirmed?.settings.station ?? file.station))}</td>
    <td>${spanLabel(file.start, file.end)}</td>
    <td>${fileStatus(file)}${file.preview ? `<p class="muted small file-message">Готов предпросмотр</p>` : ""}</td>
    <td><div class="history-actions">
      ${sourceButton(file, true)}
      ${button(file.confirmed ? "Изменить фильтрацию" : "Обработать снова", "edit", { id: file.id })}
      ${file.confirmed ? iconButton(`Удалить обработанные данные · ${file.filename}`, "remove", file.id, "trash") : ""}
    </div></td>
  </tr>`;
}

function filesPage() {
  const all = Array.from(state.files.values());
  const queue = all.filter((file) => !file.version);
  const history = all.filter((file) => file.version).sort((first, last) =>
    (last.start ?? "").localeCompare(first.start ?? "") || (last.created_at ?? "").localeCompare(first.created_at ?? ""));
  const queueLength = queue.length + state.transfers.length;
  return `${heading("Файлы и импорт", "Загрузка наблюдений, проверка и учёт в общих графиках", link("Расширенный аудит", "/review"))}
    <div class="split">
      ${card(`${cardTitle("Загрузить новые файлы", "Выберите один или несколько CSV или XLSX. Обработка выполняется по одному файлу.")}
        <div class="upload" id="dropzone">
          <span class="upload-icon">${icon("upload", 24)}</span><h3>Перетащите CSV или XLSX сюда</h3>
          ${button("Выбрать файлы", "browse", { type: "accent", disabled: state.loading })}
        </div>
        <div class="file-feedback" aria-live="polite" ${state.message ? "" : "hidden"}>${state.message ? note(state.message, state.error) : ""}</div>
        ${state.error && state.loading ? button("Повторить загрузку списка", "reload") : ""}`)}
      ${card(`${cardTitle("Очередь обработки", "", tag(fmt(queueLength), "neutral"))}
        ${queueLength ? queue.map(queueItem).join("") + state.transfers.map(transferItem).join("") :
        `<div class="queue-empty">${icon("file", 25)}<p>${state.loading ? "Загрузка списка файлов…" : "Загруженные файлы появятся здесь."}</p>
          ${button("Попробовать на примере", "example", { disabled: state.loading || state.uploading || state.exampleLoading })}
        </div>`}`, "file-queue")}
    </div>
    ${card(`${cardTitle("История загрузок", "Исходники остаются в архиве при повторной обработке и исключении из графиков.")}
      <div class="table-scroll" tabindex="0" role="region" aria-label="История загрузок">
        <table class="history-table"><thead><tr><th scope="col">Файл</th><th scope="col">Станция</th><th scope="col">Период</th><th scope="col">Статус</th><th scope="col">Действия</th></tr></thead>
        <tbody>${history.map(historyRow).join("") || '<tr><td class="history-empty muted" colspan="5">История пока пуста.</td></tr>'}</tbody></table>
      </div>`)}`;
}

function settingsQuery(settings) {
  const { station, filters, fill, timezone, subhour, pressure_reference, sources, conflict_policy, pressure_max } = settings;
  return {
    station, filters: [...filters].sort().join(","), fill, pressure_max: String(pressure_max ?? "1050"),
    conflict_policy: filters.includes("conflicts") ? conflict_policy : "all",
    ...(fill === "external" ? { timezone, subhour, pressure_reference: pressure_reference ?? "", sources: [...sources].sort().join(",") } : {}),
  };
}

function settingsFor(file) {
  const settings = file.preview?.settings ?? file.settings ?? file.confirmed?.settings;
  return {
    station: settings?.station ?? file.station,
    filters: settings?.filters ?? [],
    fill: settings?.fill ?? "none",
    timezone: settings?.timezone ?? "",
    subhour: settings?.subhour ?? "skip",
    pressure_reference: settings?.pressure_reference ?? "",
    pressure_max: String(settings?.pressure_max ?? "1050"),
    sources: settings?.sources?.length ? settings.sources : ["era5", "merra2"],
    conflict_policy: settings?.conflict_policy ?? "all",
  };
}

function checkedPreview(editor) {
  if (!editor.preview) return false;
  const calculated = settingsQuery(editor.preview.settings);
  return Object.entries(settingsQuery(editor.settings)).every(([key, value]) => calculated[key] === value);
}

function fillSettings(settings, busy) {
  const fill = state.fills.find((item) => item.id === settings.fill);
  return `${selectField("Способ заполнения", "fill-pick", state.fills.map((item) => [item.id, item.label]), settings.fill, busy)}
    ${fill?.description ? `<p class="caption">${escapeHtml(fill.description)}</p>` : ""}
    ${settings.fill === "external" ? `<div class="file-options">
      ${selectField("Часовой пояс исходного файла", "file-timezone",
        [["", "Выберите часовой пояс"], ["Europe/Moscow", "Москва (UTC+3)"], ["UTC", "UTC"]], settings.timezone, busy)}
      ${selectField("Неполные часы", "file-subhour",
        [["interpolate", "Интерполировать"], ["skip", "Оставить пропуски"]], settings.subhour, busy)}
      ${selectField("Тип давления в файле", "file-pressure",
        [["", "Неизвестен"], ["surface", "Давление у поверхности"]], settings.pressure_reference, busy)}
      <p class="caption">Источники · ERA5 и MERRA-2</p>
    </div>` : ""}`;
}

function editorPage(editor) {
  const file = state.files.get(editor.id);
  const { settings, busy } = editor;
  const hasPressure = file.params?.some(({ id }) => id === "pressure_hpa");
  const checked = checkedPreview(editor);
  const active = editor.done ? "В общих графиках" : editor.preview ? "Проверка результата" : "Настройка фильтрации";
  const statusText = busy === "restore" ? "Загрузка предпросмотра…" :
    busy === "preview" ? "Обработка файла…" :
    busy === "confirm" ? "Сохранение результата…" :
    editor.preview && !checked ? "Настройки изменились. Запустите фильтрацию повторно." : "";
  return `${heading("Обработка файла", "", button("К файлам", "list", { glyph: "back" }))}
    ${card(`<div class="between file-context"><div class="row"><span class="file-symbol">${icon("file", 19)}</span>
        <div><h2>${escapeHtml(file.filename)}</h2><p class="muted">${spanLabel(file.start, file.end)} · ${countLabel(file.rows, "строка", "строки", "строк")}</p></div>
      </div><div class="row">${tag(active, editor.done ? "" : "neutral")}${sourceButton(file)}
        ${link("Аудит исходника", `/review?${new URLSearchParams({ file: file.id })}`)}</div></div>`)}
    ${editor.done ? card(`<div class="file-success"><span class="upload-icon">${icon("check", 24)}</span>
      <div><h2>Данные добавлены в общие графики</h2><p class="muted">Исходный файл сохранён в истории загрузок.</p></div>
      ${link("К общим графикам", "/", { type: "primary", glyph: "arrow" })}${button("К очереди", "list")}</div>`) :
      `<div class="half">
      ${card(`${cardTitle("Фильтрация", "Первичная проверка выполняется при обработке автоматически.")}
        ${note(state.primary?.description || "Явно невозможные значения исключаются. Например, −100 °C или +55 °C.")}
        ${hasPressure ? `<div class="file-options">
          ${selectField("Верхняя граница давления", "file-pressure-max",
            [["1050", "1 050 гПа"], ["1550", "1 550 гПа · расширенная граница"]], settings.pressure_max, busy)}
          <p class="caption">При любой границе учитываются флаги качества из исходного файла.</p>
        </div>` : ""}
        <h3 class="edit-subhead">Дополнительные методы</h3>
        <div class="method-list">${state.filters.map((filter) => `<label class="method">
          <input type="checkbox" data-method="${escapeHtml(filter.id)}" ${settings.filters.includes(filter.id) ? "checked" : ""} ${busy ? "disabled" : ""}>
          <span><strong>${escapeHtml(filter.label)}</strong><span>${escapeHtml(filter.description)}</span></span>
        </label>`).join("")}</div>
        ${settings.filters.includes("conflicts") ? `<div class="file-options">${selectField("Повторяющиеся сроки", "file-conflicts",
          [["all", "Исключить конфликтные сроки"], ["best", "Выбрать наиболее надёжную запись"]], settings.conflict_policy, busy)}</div>` : ""}`)}
      ${card(`${cardTitle("Заполнение пропусков", "Выберите источник данных или ИИ-модель.")}
        ${fillSettings(settings, busy)}
        <hr class="rule section-rule">
        ${selectField("Метеостанция", "file-station", state.stations.map(({ id, label }) => [id, label]), settings.station, busy)}`)}
    </div>
    <div class="between processing-bar"><span class="muted small" id="processing-status" role="status">${statusText}</span>
      ${button("Отфильтровать", "filter", { type: "primary", glyph: "filter", disabled: Boolean(busy) })}</div>
    ${editor.message ? `<div role="alert">${note(editor.message, true)}</div>` : ""}
    ${editor.preview ? previewCard(editor, checked, file) : ""}`}`;
}

function colorFor(key) {
  return parameterColors[key] ?? "#ab4d32";
}

function outlierChip(label, key, count, current, color = "") {
  return `<button type="button" class="outlier-chip" data-file-action="outlier" data-file-id="${escapeHtml(key)}" aria-pressed="${key === current}">
    ${color ? `<i class="swatch" style="--dot:${color}"></i>` : ""}${escapeHtml(label)}<strong>${fmt(count)}</strong></button>`;
}

function legendItem(label, color) {
  return `<span class="legend-key"><i class="swatch" style="--dot:${color}"></i>${label}</span>`;
}

function table(headers, rows) {
  return `<div class="table-scroll" tabindex="0" role="region" aria-label="Отмеченные ячейки">
    <table class="outlier-table"><thead><tr>${headers.map((label) => `<th scope="col">${label}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${row.map((value) => `<td>${escapeHtml(value)}</td>`).join("")}</tr>`).join("")}</tbody></table>
  </div>`;
}

function previewCard(editor, checked, file) {
  const { preview, metric } = editor;
  const { params, points, stats } = preview;
  const selected = params.find(({ id }) => id === metric);
  const chosen = selected ? [selected] : params;
  const visibleCount = selected ? stats.parameters[metric]?.marked ?? 0 : stats.marked;
  const shown = chosen.flatMap((parameter) => (points[parameter.id] ?? [])
    .filter((point) => point.marked).slice(0, 30).map((point) => ({ ...point, parameter })))
    .sort((first, last) => first.time.localeCompare(last.time)).slice(0, 30);
  const chart = selected ? outlierTrace(points[metric] ?? [], selected) : outlierTimeline(preview, file);
  const download = filesUrl("excel", { id: file.id, revision: preview.revision });
  return card(`${cardTitle("Выбросы к удалению", "Предпросмотр после первичной проверки и выбранных дополнительных методов.",
      tag(file.confirmed ? "Текущая версия остаётся в графиках" : "В общую базу не добавлено", "neutral"))}
    <div class="outlier-top"><div><span class="eyebrow">Всего</span><div class="outlier-number">${countLabel(stats.marked)}</div></div>
      ${selected ? `<div class="metric selected-count"><span class="small">${escapeHtml(selected.label)}</span><strong>${countLabel(visibleCount)}</strong></div>` : ""}
      ${stats.filled ? `<div class="metric selected-count"><span class="small">Заполнено</span><strong>${fmt(selected ? stats.parameters[metric]?.filled ?? 0 : stats.filled)}</strong></div>` : ""}
    </div>
    <div class="outlier-tabs" role="group" aria-label="Выбросы по параметрам">
      ${outlierChip("Все", "all", stats.marked, metric)}
      ${params.map(({ id, label }) => outlierChip(label, id, stats.parameters[id]?.marked ?? 0, metric, colorFor(id))).join("")}
    </div>
    <div class="outlier-chart">${chart}</div>
    ${shown.length ? `<details class="outlier-details"><summary>Показать отмеченные ячейки</summary>
      ${table(["Дата и время", "Параметр", "Исходное значение"], shown.map(({ time, parameter, value }) =>
        [time, parameter.label, Number.isFinite(value) ? `${fmt(value, 2)} ${parameter.unit}` : "—"]))}
      ${shown.length < visibleCount ? `<p class="caption">Показано ${fmt(shown.length)} из ${fmt(visibleCount)} отмеченных ячеек.</p>` : ""}
    </details>` : `<p class="caption">${visibleCount ? "Полный список отмеченных ячеек доступен в XLSX." : "Выбросов для выбранных параметров нет."}</p>`}
    ${preview.warnings?.length ? `<div class="preview-warnings">${preview.warnings.map((message) => note(message, true)).join("")}</div>` : ""}
    <div class="between publish-bar">
      <div>${checked && !editor.busy ? link("Скачать отфильтрованный файл", download, { glyph: "download" }) :
        button("Скачать отфильтрованный файл", "excel", { glyph: "download", disabled: true })}
        <p class="muted small">XLSX с подсветкой очищенных ячеек</p></div>
      ${button(editor.busy === "confirm" ? "Сохранение…" : file.confirmed ? "Заменить в базе" : "Добавить в базу",
        "publish", { type: "accent", glyph: "arrow", disabled: !checked || Boolean(editor.busy) })}
    </div>`, "anchor preview-card-shell");
}

const DAY = 86_400_000;
const chartBox = { w: 960, h: 218, left: 52, right: 15, top: 16, bottom: 185 };
const chartWidth = chartBox.w - chartBox.left - chartBox.right;

function gridLine(value, y) {
  const { left, w, right } = chartBox;
  const label = fmt(value, Number.isInteger(value) ? 0 : 2);
  return `<path d="M${left} ${y}H${w - right}" class="grid"/><text x="${left - 8}" y="${y + 4}" text-anchor="end">${label}</text>`;
}

function axisLabel(value, x, align = "middle") {
  return `<text x="${x}" y="206" text-anchor="${align}">${escapeHtml(value)}</text>`;
}

function chartSvg(label, grid, drawing) {
  return `<svg class="graph" viewBox="0 0 ${chartBox.w} ${chartBox.h}" role="img" aria-label="${escapeHtml(label)}">${grid}${drawing}</svg>`;
}

function dayLabel(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
}

function outlierTimeline(preview, file) {
  const start = stamp(file.start.slice(0, 10));
  const end = stamp(file.end.slice(0, 10));
  const days = Math.round((end - start) / DAY) + 1;
  const months = days > 95;
  const monthNumber = (time) => Number(time.slice(0, 4)) * 12 + Number(time.slice(5, 7)) - 1;
  const startMonth = monthNumber(file.start);
  const bucketCount = months ? monthNumber(file.end) - startMonth + 1 : days;
  const countsByBucket = new Map();
  for (const row of preview.timeline) {
    const bucket = months ? monthNumber(row.time) - startMonth :
      Math.round((stamp(row.time.slice(0, 10)) - start) / DAY);
    const counts = countsByBucket.get(bucket) ?? new Map();
    for (const [key, count] of Object.entries(row.parameters)) {
      if (count > 0) counts.set(key, (counts.get(key) ?? 0) + count);
    }
    if (counts.size) countsByBucket.set(bucket, counts);
  }
  let maximum = 0;
  for (const counts of countsByBucket.values()) {
    const sum = [...counts.values()].reduce((total, count) => total + count, 0);
    maximum = Math.max(maximum, sum);
  }
  const step = Math.max(1, Math.ceil(maximum / 4));
  const { left, top, bottom } = chartBox;
  const y = (value) => bottom - value / (4 * step) * (bottom - top);
  const grid = Array.from({ length: 5 }, (_, index) => gridLine(index * step, y(index * step))).join("");
  const width = chartWidth / bucketCount;
  const barWidth = Math.min(26, width * 0.71);
  const labels = new Map(preview.params.map(({ id, label }) => [id, label]));
  const dateAt = (bucket) => {
    if (!months) return new Date(start + bucket * DAY).toISOString().slice(0, 10);
    const month = bucket + startMonth;
    return `${Math.floor(month / 12)}-${String(month % 12 + 1).padStart(2, "0")}`;
  };
  const bars = [];
  for (const [bucket, counts] of countsByBucket) {
    let sum = 0;
    const x = left + (bucket + 0.5) * width;
    for (const [key, count] of counts) {
      const label = `${dateAt(bucket)} · ${labels.get(key) ?? key} · ${countLabel(count)}`;
      bars.push(`<rect x="${x - barWidth / 2}" width="${barWidth}" y="${y(sum + count)}" height="${y(sum) - y(sum + count)}" fill="${colorFor(key)}"><title>${escapeHtml(label)}</title></rect>`);
      sum += count;
    }
  }
  const stride = Math.max(1, Math.ceil(bucketCount / 11));
  const axis = [];
  for (let index = 0; index < bucketCount; index += stride) {
    const value = dateAt(index);
    const label = months ? `${value.slice(5)}/${value.slice(2, 4)}` : dayLabel(value);
    axis.push(axisLabel(label, left + (index + 0.5) * width));
  }
  return chartSvg("Количество выбросов от начала до конца периода", grid, bars.join("") + axis.join(""));
}

function outlierTrace(points, parameter) {
  const sample = points.map((point) => ({ ...point, stamp: stamp(point.time) }))
    .filter((point) => Number.isFinite(point.stamp)
      && (Number.isFinite(point.value) || Number.isFinite(point.estimate?.value)))
    .sort((first, last) => first.stamp - last.stamp);
  if (!sample.length) return '<div class="empty-chart"><h3>Нет наблюдений по этому параметру</h3></div>';

  const { left, top, bottom } = chartBox;
  const color = colorFor(parameter.id);
  const observationColor = "#6b8f79";
  const restoredColor = "#227790";
  const start = sample[0].stamp;
  const span = sample.at(-1).stamp - start;
  const x = (time) => left + (span ? (time - start) / span : 0.5) * chartWidth;
  let low = Infinity;
  let high = -Infinity;
  for (const point of sample) {
    for (const value of [point.value, point.estimate?.value]) {
      if (Number.isFinite(value)) {
        low = Math.min(low, value);
        high = Math.max(high, value);
      }
    }
  }
  const scale = Math.max(Math.abs(low), Math.abs(high), 1);
  const delta = high / scale - low / scale;
  const y = (value) => bottom - (delta ? (value / scale - low / scale) / delta : 0.5) * (bottom - top);
  const grid = delta ? Array.from({ length: 5 }, (_, index) => {
    const value = low * (1 - index / 4) + high * (index / 4);
    return gridLine(value, y(value));
  }).join("") : gridLine(low, y(low));
  const axis = [];
  const seen = new Set();
  for (let index = 0; index <= 6; index++) {
    const time = start + span * index / 6;
    const iso = new Date(time).toISOString();
    const label = span <= DAY ? iso.slice(11, 16) : dayLabel(iso);
    if (seen.has(label)) continue;
    axis.push(axisLabel(label, x(time), span && index === 0 ? "start" : span && index === 6 ? "end" : "middle"));
    seen.add(label);
  }
  const dots = [];
  const highlighted = [];
  for (const point of sample) {
    const position = x(point.stamp);
    if (Number.isFinite(point.value)) {
      if (point.marked) {
        const label = `${point.time} · ${fmt(point.value, 2)} ${parameter.unit} · к удалению`;
        highlighted.push(`<circle cx="${position}" cy="${y(point.value)}" r="4.1" fill="${color}" stroke="#fff" stroke-width="1.2"><title>${escapeHtml(label)}</title></circle>`);
      } else {
        dots.push(`<circle cx="${position}" cy="${y(point.value)}" r="1.8" fill="${observationColor}" opacity=".64"/>`);
      }
    }
    if (Number.isFinite(point.estimate?.value)) {
      const label = `${point.time} · ${fmt(point.estimate.value, 2)} ${parameter.unit} · ${point.estimate.source}`;
      highlighted.push(`<circle cx="${position}" cy="${y(point.estimate.value)}" r="4.1" fill="${restoredColor}" stroke="#fff" stroke-width="1.2"><title>${escapeHtml(label)}</title></circle>`);
    }
  }
  const estimates = sample.some((point) => Number.isFinite(point.estimate?.value));
  return `<span class="small muted">${escapeHtml(parameter.unit)}</span>
    ${chartSvg(`${parameter.label} с выделенными выбросами`, grid, axis.join("") + dots.join("") + highlighted.join(""))}
    <div class="legend">${legendItem("Наблюдения", observationColor)}${legendItem("К удалению", color)}
      ${estimates ? legendItem("Заполненные значения", restoredColor) : ""}</div>`;
}

function render({ top = false } = {}) {
  main.innerHTML = `<section>${state.editor ? editorPage(state.editor) : filesPage()}</section>`;
  if (top) {
    const heading = main.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  }
}

function messageFor(error) {
  return error instanceof TypeError ? "Не удалось связаться с сервером." : error.message;
}

function saveRecord(result) {
  const file = result.file ?? result;
  state.files.set(file.id, file);
  return file;
}

async function loadList() {
  state.message = "";
  state.error = false;
  render();
  try {
    const [listing, catalog] = await Promise.all([
      jsonRequest(filesUrl()),
      jsonRequest("/api/stations"),
    ]);
    state.files = new Map(listing.files.map((file) => [file.id, file]));
    state.stations = catalog.stations;
    state.filters = listing.filters;
    state.fills = listing.fills;
    state.primary = listing.primary;
    state.loading = false;
  } catch (error) {
    state.message = messageFor(error);
    state.error = true;
  }
  render();
}

function appendUploads(files) {
  for (const file of files) state.transfers.push({ file, filename: file.name, status: "queued" });
  if (!state.editor) render();
  if (!state.uploading) void uploadNext();
}

async function uploadNext() {
  state.uploading = true;
  while (state.transfers.length) {
    const transfer = state.transfers[0];
    const { file, filename } = transfer;
    transfer.status = "loading";
    if (!state.editor) render();
    try {
      if (!/\.(csv|xlsx)$/i.test(filename)) throw new Error("Поддерживаются форматы CSV и XLSX.");
      const contentType = filename.toLowerCase().endsWith(".xlsx")
        ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv";
      const result = await jsonRequest(filesUrl("upload", { filename }), {
        method: "POST", headers: { "Content-Type": contentType }, body: file,
      });
      saveRecord(result);
    } catch (error) {
      state.files.set(Symbol(filename), { filename, status: "error", error: messageFor(error) });
    }
    state.transfers.shift();
  }
  state.uploading = false;
  if (!state.editor) render();
}

function csvCell(value) {
  return '\"' + String(value ?? "").replaceAll('\"', '\"\"') + '\"';
}

async function uploadExample() {
  state.exampleLoading = true;
  state.message = "Загружаем пример Кр1…";
  state.error = false;
  render();
  try {
    const rows = await jsonRequest("/api/import/example");
    const fields = Object.keys(rows[0]);
    const csv = [fields.join(","), ...rows.map((row) => fields.map((field) => csvCell(row[field])).join(","))].join("\n");
    state.message = "";
    appendUploads([new File([csv], "kr1-station-2024-04-01.csv", { type: "text/csv" })]);
  } catch (error) {
    state.message = messageFor(error);
    state.error = true;
  } finally {
    state.exampleLoading = false;
    if (!state.editor) render();
  }
}

async function openEditor(id) {
  const file = state.files.get(id);
  if (!file || file.status === "error") return;
  const editor = state.editors.get(id) ?? {
    id, settings: settingsFor(file), preview: null, metric: "all", message: "",
  };
  state.editors.set(id, editor);
  state.editor = editor;
  if (!editor.preview && !editor.busy && file.preview) {
    editor.busy = "restore";
    render({ top: true });
    try {
      editor.preview = await jsonRequest(filesUrl("preview", { id, revision: file.preview.revision }));
      if (editor.preview.file) saveRecord(editor.preview);
    } catch (error) {
      editor.message = `Предпросмотр недоступен. ${messageFor(error)}`;
    } finally {
      editor.busy = null;
      if (state.editor === editor) render();
    }
  } else {
    render({ top: true });
  }
}

async function runPreview(editor) {
  if (editor.busy) return;
  if (editor.settings.fill === "external" && !editor.settings.timezone) {
    editor.message = "Укажите часовой пояс исходного файла.";
    render();
    document.getElementById("file-timezone").focus();
    return;
  }
  editor.busy = "preview";
  editor.message = "";
  render();
  try {
    const result = await jsonRequest(filesUrl("preview", { id: editor.id, ...settingsQuery(editor.settings) }), { method: "POST" });
    const file = saveRecord(result);
    editor.preview = result;
    editor.settings = settingsFor(file);
    editor.metric = "all";
  } catch (error) {
    editor.message = messageFor(error);
    editor.preview = null;
  } finally {
    editor.busy = null;
    if (state.editor === editor) {
      render();
      const refreshed = main.querySelector(editor.preview ? '[data-file-action="outlier"]' : '[data-file-action="filter"]');
      refreshed?.focus({ preventScroll: true });
      main.querySelector(".preview-card-shell")?.scrollIntoView({ block: "nearest" });
    } else if (!state.editor) {
      render();
    }
  }
}

async function confirmFile(editor) {
  if (editor.busy || !checkedPreview(editor)) return;
  editor.busy = "confirm";
  editor.message = "";
  render();
  try {
    saveRecord(await jsonRequest(filesUrl("confirm", {
      id: editor.id, revision: editor.preview.revision, ...settingsQuery(editor.settings),
    }), { method: "POST" }));
    notifyDataChange();
    editor.done = true;
    state.editors.delete(editor.id);
  } catch (error) {
    editor.message = messageFor(error);
  } finally {
    editor.busy = null;
    if (state.editor === editor) render({ top: editor.done });
    else if (!state.editor) render();
  }
}

function notifyDataChange() {
  try {
    localStorage.setItem("meteoarchive.dataRevision", crypto.randomUUID());
  } catch {
    // Обновление архива уже сохранено на сервере.
  }
}

function openDialog(title, body) {
  dialog.innerHTML = `<div class="between dialog-header"><h2 id="files-dialog-title">${escapeHtml(title)}</h2>
    ${iconButton("Закрыть", "close", "", "close")}</div>${body}`;
  dialog.showModal();
}

async function readCsvSample(url, signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error("Не удалось прочитать исходный CSV.");
  const reader = response.body.getReader();
  const bytes = new Uint8Array(8192);
  let offset = 0;
  let lineCount = 0;
  try {
    while (offset < bytes.length && lineCount < 9) {
      const { done, value } = await reader.read();
      if (done) break;
      const piece = value.subarray(0, bytes.length - offset);
      bytes.set(piece, offset);
      offset += piece.length;
      for (const byte of piece) if (byte === 10) lineCount++;
    }
  } finally {
    await reader.cancel();
  }
  const sample = bytes.subarray(0, offset);
  let content = new TextDecoder().decode(sample);
  if (content.includes("\uFFFD")) content = new TextDecoder("windows-1251").decode(sample);
  return content.split(/\r?\n/).slice(0, 9).join("\n");
}

async function showOriginal(file) {
  const url = filesUrl("original", { id: file.id });
  const excel = file.filename.toLowerCase().endsWith(".xlsx");
  openDialog(file.filename, `<div class="raw-dialog">
    <p class="muted raw-status" role="status">${excel ? "Сохранённая исходная книга XLSX." : "Читаем начальные строки исходного файла…"}</p>
    <pre class="raw-sample" hidden></pre>
    ${link(excel ? "Скачать исходный XLSX" : "Скачать исходный CSV", url, { glyph: "download", filename: file.filename })}
  </div>`);
  if (excel) return;
  const status = dialog.querySelector(".raw-status");
  const sample = dialog.querySelector(".raw-sample");
  const request = new AbortController();
  dialog.addEventListener("close", () => request.abort(), { once: true });
  try {
    sample.textContent = await readCsvSample(url, request.signal);
    sample.hidden = !sample.textContent;
    status.textContent = sample.textContent ? "Начальные строки исходного файла." : "Исходный файл пуст.";
  } catch (error) {
    if (!request.signal.aborted) status.textContent = messageFor(error);
  }
}

function showDiscard(file) {
  openDialog("Удалить обработанные данные?", `<div class="raw-dialog">
    <p>Очищенные данные будут исключены из общих графиков. Исходный файл сохранится.</p>
    <p class="muted">${escapeHtml(file.filename)}</p>
    <div class="discard-error" role="alert" hidden></div>
    <div class="dialog-actions">${button("Отмена", "close")}${button("Удалить данные", "discard", { type: "primary", id: file.id })}</div>
  </div>`);
}

async function discardFile(id) {
  const file = state.files.get(id);
  if (!file?.confirmed || dialog.getAttribute("aria-busy") === "true") return;
  dialog.setAttribute("aria-busy", "true");
  for (const control of dialog.querySelectorAll("button")) control.disabled = true;
  const errorBox = dialog.querySelector(".discard-error");
  errorBox.hidden = true;
  try {
    saveRecord(await jsonRequest(filesUrl("discard", { id, version: file.version }), { method: "POST" }));
    notifyDataChange();
    state.editors.delete(id);
    state.message = "Очищенные данные исключены из графиков. Исходник сохранён.";
    state.error = false;
    dialog.close();
    render();
    main.querySelector(".file-feedback")?.scrollIntoView({ block: "nearest" });
  } catch (error) {
    errorBox.hidden = false;
    errorBox.innerHTML = note(messageFor(error), true);
  } finally {
    dialog.removeAttribute("aria-busy");
    for (const control of dialog.querySelectorAll("button")) control.disabled = false;
  }
}

const settingFields = {
  "file-station": "station", "fill-pick": "fill", "file-timezone": "timezone",
  "file-subhour": "subhour", "file-pressure": "pressure_reference", "file-pressure-max": "pressure_max",
  "file-conflicts": "conflict_policy",
};

main.addEventListener("change", (event) => {
  const editor = state.editor;
  if (!editor || editor.busy || editor.done) return;
  const target = event.target;
  const { method } = target.dataset;
  const key = settingFields[target.id];
  if (!method && !key) return;
  if (method) {
    const ids = editor.settings.filters;
    editor.settings.filters = target.checked ? [...ids, method] : ids.filter((id) => id !== method);
  } else {
    editor.settings[key] = target.value;
  }
  editor.message = "";
  render();
  const controls = method ? [...main.querySelectorAll("[data-method]")].find((input) => input.dataset.method === method) :
    document.getElementById(target.id);
  controls?.focus({ preventScroll: true });
});

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-file-action]");
  if (!control) return;
  const action = control.dataset.fileAction;
  const id = control.dataset.fileId;
  const file = state.files.get(id);
  const editor = state.editor;
  switch (action) {
    case "browse": picker.click(); break;
    case "example": void uploadExample(); break;
    case "reload": void loadList(); break;
    case "edit": void openEditor(id); break;
    case "list": state.editor = null; render({ top: true }); break;
    case "filter": if (editor) void runPreview(editor); break;
    case "publish": if (editor) void confirmFile(editor); break;
    case "source": if (file) void showOriginal(file); break;
    case "remove": if (file) showDiscard(file); break;
    case "discard": void discardFile(id); break;
    case "close": dialog.close(); break;
    case "outlier":
      if (editor?.preview) {
        editor.metric = id;
        const preview = main.querySelector(".preview-card-shell");
        preview.outerHTML = previewCard(editor, checkedPreview(editor), state.files.get(editor.id));
        main.querySelector(".outlier-chip[aria-pressed=true]")?.focus({ preventScroll: true });
      }
      break;
  }
});

dialog.addEventListener("cancel", (event) => {
  if (dialog.getAttribute("aria-busy") === "true") event.preventDefault();
});

picker.addEventListener("change", () => {
  appendUploads(picker.files);
  picker.value = "";
});

function isFileDrag(event) {
  return event.dataTransfer?.types.includes("Files");
}

main.addEventListener("dragover", (event) => {
  const dropzone = event.target.closest("#dropzone");
  if (dropzone && !state.loading && isFileDrag(event)) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    dropzone.classList.add("drag");
  }
});

main.addEventListener("dragleave", (event) => {
  const dropzone = event.target.closest("#dropzone");
  if (dropzone && !dropzone.contains(event.relatedTarget)) dropzone.classList.remove("drag");
});

window.addEventListener("drop", (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  const dropzone = event.target.closest("#dropzone");
  if (!dropzone) return;
  dropzone.classList.remove("drag");
  if (!state.loading && event.dataTransfer.files.length) appendUploads(event.dataTransfer.files);
});

void loadList();
