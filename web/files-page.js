const main = document.querySelector(".files-main");
const picker = document.getElementById("fileInput");
const correctedPicker = document.getElementById("correctedCsvInput");
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
const filterText = {
  conflicts: ["Записи с одинаковым временем", "Если на одно время приходятся разные показания, можно убрать всю группу или оставить запись, которая лучше согласуется с соседними."],
  hampel: ["Значение выбивается из ряда", "Сравнивает показание с соседними. Отмечает его, когда разница превышает заданный порог и обычный разброс значений."],
  spike: ["Скачок с возвратом", "Значение резко меняется на один замер, а в следующем возвращается к прежнему уровню."],
  stuck: ["Датчик повторяет одно значение", "Отмечает одинаковые показания, которые держатся заданное число часов или дольше."],
};
const state = {
  files: new Map(),
  editors: new Map(),
  stations: [],
  uploadStation: "kr1",
  newStation: { label: "", latitude: "", longitude: "", profile: "kr1" },
  filters: [],
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

function inputField(label, id, value, attributes) {
  return `<div class="field"><label for="${id}">${label}</label>
    <input id="${id}" value="${escapeHtml(value)}" ${attributes}></div>`;
}

function stationName(id) {
  return state.stations.find((station) => station.id === id)?.label ?? id ?? "—";
}

function stationOptions() {
  return state.stations.map(({ id, label }) => [id, label]);
}

function saveStations(catalog) {
  state.stations = catalog.stations.filter(({ label }) => !/^поляна и поле$/i.test(label.trim()));
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
    <td>${fileStatus(file)}
      ${file.preview ? `<p class="muted small file-message">Готов предпросмотр</p>` : ""}
      ${file.confirmed?.manual ? `<p class="muted small file-message">С ручными правками</p>` : ""}</td>
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
  return `${heading("Файлы и импорт", "Загрузка наблюдений, проверка и учёт в общих графиках",
    `<div class="row heading-actions">${link("Расширенный аудит", "/review")}${link("Доступ оператора", "/operator")}</div>`)}
    <div class="split">
      ${card(`${cardTitle("Загрузить новые файлы", "Выберите один или несколько CSV или XLSX. Обработка выполняется по одному файлу.")}
        ${uploadStationFields()}
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

function uploadStationFields() {
  const { label, latitude, longitude, profile } = state.newStation;
  return `<div id="upload-station-fields" class="file-options">
    ${selectField("Станция для загрузки", "upload-station", [...stationOptions(), ["new", "Новая метеостанция…"]],
      state.uploadStation, state.loading)}
    ${state.uploadStation === "new" ? `<div class="new-station-fields">
      ${inputField("Название новой станции", "upload-station-label", label, 'required maxlength="100" data-new-station="label"')}
      ${selectField("Профиль прибора", "upload-station-profile", [["kr1", "Сокол М1"], ["ame60", "АМЕ-60"]], profile)}
      ${inputField("Точная широта", "upload-station-latitude", latitude, 'required inputmode="decimal" placeholder="56,5234" data-new-station="latitude"')}
      ${inputField("Точная долгота", "upload-station-longitude", longitude, 'required inputmode="decimal" placeholder="32,8765" data-new-station="longitude"')}
    </div>` : ""}
  </div>`;
}

function effectiveRanges(settings = {}) {
  const ranges = { ...state.primary?.ranges };
  if (!settings.ranges?.pressure_hpa && String(settings.pressure_max) === "1550" && ranges.pressure_hpa) {
    ranges.pressure_hpa = [ranges.pressure_hpa[0], 1550];
  }
  return { ...ranges, ...settings.ranges };
}

function effectiveThresholds(settings = {}) {
  return Object.fromEntries(Object.entries(state.primary?.thresholds ?? {}).map(([method, defaults]) =>
    [method, { ...defaults, ...settings.thresholds?.[method] }]));
}

function fillSelection(settings) {
  return {
    fill_external: settings.fill_external ?? ["external", "both"].includes(settings.fill),
    fill_model: settings.fill_model ?? ["model", "both"].includes(settings.fill),
  };
}

function settingsPayload(settings) {
  const { station, filters, timezone, subhour, pressure_reference, sources, conflict_policy } = settings;
  const { fill_external, fill_model } = fillSelection(settings);
  const ranges = Object.fromEntries(Object.entries(effectiveRanges(settings)).sort(([a], [b]) => a.localeCompare(b))
    .map(([id, values]) => [id, values.map((value) => value === "" ? null : Number(value))]));
  const thresholds = Object.fromEntries(Object.entries(effectiveThresholds(settings)).sort(([a], [b]) => a.localeCompare(b))
    .map(([method, params]) => [method, Object.fromEntries(Object.entries(params).sort(([a], [b]) => a.localeCompare(b))
      .map(([id, value]) => {
        const maximum = method === "stuck" ? state.primary.max_stuck_hours : state.primary.max_absolute_bound;
        if (!filters.includes(method) && !(Number(value) > 0 && Number(value) <= maximum)) {
          return [id, state.primary.thresholds[method][id]];
        }
        return [id, value === "" ? null : Number(value)];
      }))]));
  return {
    station, filters: [...filters].sort().join(","), ranges, thresholds,
    fill_external: fill_external ? "1" : "0", fill_model: fill_model ? "1" : "0",
    pressure_max: ranges.pressure_hpa?.[1] === 1550 ? "1550" : "1050",
    conflict_policy: filters.includes("conflicts") ? conflict_policy : "all",
    ...(fill_external ? { timezone, subhour, pressure_reference: pressure_reference ?? "", sources: [...sources].sort().join(",") } : {}),
  };
}

function jsonPost(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function settingsFor(file) {
  const settings = file.preview?.settings ?? file.settings ?? file.confirmed?.settings ?? {};
  return {
    station: settings.station ?? file.station,
    filters: settings.filters ?? [],
    ...fillSelection(settings),
    ranges: effectiveRanges(settings),
    thresholds: effectiveThresholds(settings),
    timezone: settings.timezone ?? "",
    subhour: settings.subhour ?? "skip",
    pressure_reference: settings.pressure_reference ?? "",
    sources: settings.sources?.length ? settings.sources : ["era5", "merra2"],
    conflict_policy: settings.conflict_policy ?? "all",
  };
}

function checkedPreview(editor) {
  if (!editor.preview) return false;
  return JSON.stringify(settingsPayload(editor.settings)) === JSON.stringify(settingsPayload(editor.preview.settings));
}

function processingStatus(editor) {
  if (editor.busy === "restore") return "Загрузка предпросмотра…";
  if (editor.busy === "preview") return "Обработка файла…";
  if (editor.busy === "corrected") return "Проверка исправленного CSV…";
  if (editor.busy === "confirm") return "Сохранение результата…";
  if (editor.preview && !checkedPreview(editor)) return "Настройки изменились. Запустите фильтрацию повторно.";
  if (editor.preview?.manual) return "Повторная фильтрация обработает исходник и заменит ручные правки в черновике.";
  return "";
}

function fillSettings(settings, busy) {
  return `<div class="method-list">
    <label class="method">
      <input type="checkbox" id="file-fill-external" data-fill="fill_external" ${settings.fill_external ? "checked" : ""} ${busy ? "disabled" : ""}>
      <span><strong>Внешние метеосервисы</strong><span>Оценки из ERA5 и MERRA-2.</span></span>
    </label>
    <label class="method">
      <input type="checkbox" id="file-fill-model" data-fill="fill_model" ${settings.fill_model ? "checked" : ""} ${busy ? "disabled" : ""}>
      <span><strong>Локальная модель заполнения</strong><span>Использует соседние пригодные измерения из файла.</span></span>
    </label>
  </div>
  <p class="caption">${settings.fill_external && settings.fill_model
    ? "Сначала метеосервисы, затем локальная модель для оставшихся пропусков."
    : !settings.fill_external && !settings.fill_model ? "Пропуски останутся в данных." : ""}</p>
  ${settings.fill_external ? `<div class="file-options">
    ${selectField("Часовой пояс исходного файла", "file-timezone",
      [["", "Выберите часовой пояс"], ["Europe/Moscow", "Москва (UTC+3)"], ["UTC", "UTC"]], settings.timezone, busy)}
    ${selectField("Неполные часы", "file-subhour",
      [["interpolate", "Интерполировать"], ["skip", "Оставить пропуски"]], settings.subhour, busy)}
    ${selectField("Тип давления в файле", "file-pressure",
      [["", "Неизвестен"], ["surface", "Давление у поверхности"]], settings.pressure_reference, busy)}
  </div>` : ""}`;
}

function filteringBounds(file, editor) {
  const rows = file.params?.filter(({ id }) => editor.settings.ranges[id]) ?? [];
  if (!rows.length) return "";
  return `<details class="file-bounds" ${editor.boundsOpen ? "open" : ""}>
    <summary>Ручные границы значений</summary>
    <div class="file-options">
      <p class="caption">Значения за границами исключаются. Флаги качества из исходного файла учитываются.</p>
      ${rows.map(({ id, label, unit }) => `<fieldset class="range-fields"><legend>${escapeHtml(label)}${unit ? `, ${escapeHtml(unit)}` : ""}</legend>
        ${editor.settings.ranges[id].map((value, index) => inputField(index ? "Максимум" : "Минимум", `bound-${id}-${index}`, value,
          `type="number" step="any" required aria-label="${index ? "Максимум" : "Минимум"}: ${escapeHtml(label)}" data-bound="${id}" data-bound-index="${index}" ${editor.busy ? "disabled" : ""}`)).join("")}
      </fieldset>`).join("")}
      ${methodThresholds(rows, editor)}
      <div>${button("Сбросить границы", "reset-bounds", { disabled: Boolean(editor.busy) })}</div>
    </div>
  </details>`;
}

function methodThresholds(params, editor) {
  const methods = Object.entries(editor.settings.thresholds).filter(([method]) => editor.settings.filters.includes(method));
  if (!methods.length) return "";
  return `<h3>Пороги дополнительных методов</h3>
    ${methods.map(([method, values]) => `<fieldset class="range-fields">
      <legend>${filterText[method][0]}${method === "stuck" ? " · минимум часов" : " · минимальное отклонение"}</legend>
      ${params.filter(({ id }) => Object.hasOwn(values, id)).map(({ id, label, unit }) => inputField(
        `${escapeHtml(label)}${unit && method !== "stuck" ? `, ${escapeHtml(unit)}` : ""}`, `threshold-${method}-${id}`, values[id],
        `type="number" step="any" required aria-label="${escapeHtml(filterText[method][0] + ": " + label)}"
        data-threshold="${method}" data-threshold-param="${id}" ${editor.busy ? "disabled" : ""}`)).join("")}
    </fieldset>`).join("")}`;
}

function editorPage(editor) {
  const file = state.files.get(editor.id);
  const { settings, busy } = editor;
  const hasPressure = file.params?.some(({ id }) => id === "pressure_hpa");
  const pressureUpper = String(settings.ranges.pressure_hpa?.[1] ?? "1050");
  const pressurePreset = ["1050", "1550"].includes(pressureUpper) ? pressureUpper : "manual";
  const checked = checkedPreview(editor);
  const active = editor.done ? "В общих графиках" : editor.preview ? "Проверка результата" : "Настройка фильтрации";
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
            [["1050", "1 050 гПа"], ["1550", "1 550 гПа · расширенная граница"], ["manual", "Задана вручную"]], pressurePreset, busy)}
        </div>` : ""}
        ${filteringBounds(file, editor)}
        <h3 class="edit-subhead">Дополнительные методы</h3>
        <div class="method-list">${state.filters.map((filter) => {
          const [label, description] = filterText[filter.id] ?? [filter.label, filter.description];
          return `<label class="method">
          <input type="checkbox" data-method="${escapeHtml(filter.id)}" ${settings.filters.includes(filter.id) ? "checked" : ""} ${busy ? "disabled" : ""}>
          <span><strong>${escapeHtml(label)}</strong><span>${escapeHtml(description)}</span></span>
        </label>`;
        }).join("")}</div>
        ${settings.filters.includes("conflicts") ? `<div class="file-options">${selectField("Записи с одинаковым временем", "file-conflicts",
          [["all", "Исключить конфликтные сроки"], ["best", "Выбрать наиболее надёжную запись"]], settings.conflict_policy, busy)}</div>` : ""}`)}
      ${card(`${cardTitle("Заполнение пропусков", "Выберите источник данных или ИИ-модель.")}
        ${fillSettings(settings, busy)}
        <hr class="rule section-rule">
        ${selectField("Метеостанция", "file-station", stationOptions(), settings.station, busy)}`)}
    </div>
    <div class="between processing-bar"><span class="muted small" id="processing-status" role="status">${processingStatus(editor)}</span>
      ${button("Отфильтровать", "filter", { type: "primary", glyph: "filter", disabled: Boolean(busy) })}</div>
    ${editor.message ? `<div class="file-editor-feedback" role="alert">${note(editor.message, true)}</div>` : ""}
    ${editor.preview ? previewCard(editor, checked, file) : ""}`}`;
}

function colorFor(key) {
  return parameterColors[key] ?? "#ab4d32";
}

function outlierChip(label, key, count, active, color = "") {
  return `<button type="button" class="outlier-chip" data-file-action="outlier" data-file-id="${escapeHtml(key)}" aria-pressed="${active}">
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

const timelineLayers = {
  marked: { label: "Исходные выбросы", color: "#ab4d32" },
  filled: { label: "Замены и заполнения", color: "#227790" },
  manual: { label: "Ручные правки", color: "#735579" },
};

function previewEvents(parameters, points) {
  const events = [];
  for (const parameter of parameters) {
    const filledTimes = new Set();
    const manualTimes = new Set();
    for (const point of points[parameter.id] ?? []) {
      if (point.marked) events.push({ ...point, parameter, kind: "marked" });
      if (point.estimate && !filledTimes.has(point.time)) {
        filledTimes.add(point.time);
        events.push({ ...point, parameter, kind: "filled" });
      }
      if (point.manual && !manualTimes.has(point.time)) {
        manualTimes.add(point.time);
        events.push({ ...point, parameter, kind: "manual" });
      }
    }
  }
  return events;
}

function eventTable(events, total) {
  if (!events.length) {
    return `<p class="caption">${total ? "Подробности отмеченных сроков доступны в XLSX." : "В выбранном диапазоне нет отмеченных событий."}</p>`;
  }
  const rows = events.slice().sort((first, last) => first.time.localeCompare(last.time)).slice(0, 30);
  return `${table(["Дата и время", "Параметр", "До изменения", "Результат", "Причина или способ"],
    rows.map(({ time, parameter, value, estimate, reasons, kind, previous_value, manual_value }) => {
      const reading = (number) => Number.isFinite(number) ? `${fmt(number, 2)} ${parameter.unit}` : "—";
      if (kind === "manual") {
        return [time, parameter.label, reading(previous_value),
          manual_value == null ? "Очищено" : reading(manual_value), "Исправлено в загруженном CSV"];
      }
      const method = { model: "Локальная модель", external: "Внешние погодные источники", manual: "Ручная правка" }[estimate?.method];
      return [
        time, parameter.label, reading(value), kind === "marked" ? "К удалению" : reading(estimate.value),
        kind === "marked" ? (reasons?.join(" ") || "Отмечено проверкой") :
          [method, estimate.source, reasons?.join(" ")].filter(Boolean).join(" · "),
      ];
    }))}
    ${rows.length < total ? `<p class="caption">Показано ${fmt(rows.length)} из ${fmt(total)} событий. Полные результаты доступны в XLSX.</p>` : ""}`;
}

function manualCsvControls(file, preview, editor, ready) {
  const csvUrl = filesUrl("csv", { id: file.id, revision: preview.revision });
  return `<div class="manual-csv">
    <div><h3>Ручная правка CSV</h3>
      <p class="muted small">Исправьте значения в скачанном CSV, сохранив столбцы и время наблюдений.</p></div>
    ${preview.manual ? `<p class="row manual-csv-file">${tag("Исправленный CSV")}<strong>${escapeHtml(preview.manual.filename)}</strong></p>` : ""}
    <div class="row">
      ${ready ? link("Скачать CSV для правки", csvUrl, { glyph: "download" }) :
        button("Скачать CSV для правки", "download-csv", { glyph: "download", disabled: true })}
      ${button(editor.busy === "corrected" ? "Проверка CSV…" : "Загрузить исправленный CSV", "corrected-browse",
        { glyph: "upload", disabled: !ready })}
    </div>
  </div>`;
}

function previewCard(editor, checked, file) {
  const { preview, parameters, hiddenLayers, focusEvent, eventDate } = editor;
  const { params, points, stats } = preview;
  const chosen = parameters.size ? params.filter(({ id }) => parameters.has(id)) : params;
  const selected = parameters.size === 1 ? chosen[0] : null;
  const layers = Object.keys(timelineLayers).filter((kind) => kind !== "manual" || preview.manual?.changed);
  const enabled = layers.filter((kind) => !hiddenLayers.has(kind));
  const events = previewEvents(chosen, points);
  const totals = { marked: 0, filled: 0, manual: 0 };
  for (const { id } of chosen) {
    totals.marked += stats.parameters[id]?.marked ?? 0;
    totals.filled += stats.parameters[id]?.filled ?? 0;
    totals.manual += preview.manual?.parameters?.[id] ?? 0;
  }
  const timeline = timelineData(preview, file, chosen, events, totals);
  const focus = focusEvent ? timeline.groups.get(focusEvent.bucket)?.[focusEvent.kind] : null;
  const visible = events.filter(({ kind, time }) => focus
    ? kind === focusEvent.kind && timeline.indexFor(time) === focusEvent.bucket
    : enabled.includes(kind) && (!eventDate || time.startsWith(eventDate)));
  const eventCount = focus ? sumCounts(focus) : eventDate
    ? eventsOnDate(preview, chosen, enabled, eventDate, visible.length)
    : enabled.reduce((sum, kind) => sum + totals[kind], 0);
  const chartFocus = eventDate
    ? { bucket: timeline.indexFor(eventDate), endBucket: timeline.indexFor(`${eventDate} 23:59`) }
    : focusEvent;
  const download = filesUrl("excel", { id: file.id, revision: preview.revision });
  return card(`${cardTitle("Выбросы и заполнения", "Общая шкала изменений после выбранных методов.",
      tag(file.confirmed ? "Текущая версия остаётся в графиках" : "В общую базу не добавлено", "neutral"))}
    <div class="outlier-top"><div><span class="eyebrow">${parameters.size ? "Выбранные параметры" : "Всего"}</span>
      <div class="outlier-number">${countLabel(totals.marked)}</div></div>
      <div class="metric selected-count"><span class="small">Заменено или заполнено</span><strong>${fmt(totals.filled)}</strong></div>
      ${preview.manual?.changed ? `<div class="metric selected-count"><span class="small">Ручные правки</span><strong>${fmt(totals.manual)}</strong></div>` : ""}
    </div>
    <div class="outlier-tabs" role="group" aria-label="Параметры шкалы">
      ${outlierChip("Все", "all", stats.marked, !parameters.size)}
      ${params.map(({ id, label }) => outlierChip(label, id, stats.parameters[id]?.marked ?? 0, parameters.has(id), colorFor(id))).join("")}
    </div>
    <div class="timeline-tools"><div class="legend timeline-legend" role="group" aria-label="Слои шкалы">
      ${layers.map((kind) =>
        `<button type="button" class="legend-key" data-file-action="timeline-layer" data-file-id="${kind}" aria-pressed="${enabled.includes(kind)}">
          <i class="swatch block" style="--dot:${timelineLayers[kind].color}"></i>${timelineLayers[kind].label}</button>`).join("")}
    </div>
    ${file.start !== file.end ? `<form class="timeline-date">
      <label for="timeline-date" class="muted small">Дата на шкале</label>
      <input id="timeline-date" type="date" required min="${escapeHtml(file.start)}" max="${escapeHtml(file.end)}" value="${escapeHtml(eventDate)}">
      <button type="submit" class="btn line">Перейти</button>
    </form>` : ""}</div>
    <div class="outlier-chart">${outlierTimeline(timeline, chosen, enabled, chartFocus)}</div>
    ${timeline.incomplete ? note("Чтобы увидеть заполнения по выбранным параметрам, пересчитайте сохранённый предпросмотр.") : ""}
    ${selected ? `<div class="outlier-chart parameter-trace">${outlierTrace(points[selected.id] ?? [], selected, enabled)}</div>` : ""}
    ${eventCount || eventDate ? `<details class="outlier-details" ${focus || eventDate ? "open" : ""}>
      <summary>${focus ? `${timeline.periodLabel(focusEvent.bucket)} · ${timelineLayers[focusEvent.kind].label} · ${fmt(eventCount)}` :
        eventDate ? `${spanLabel(eventDate, eventDate)} · Отмеченные сроки` : "Показать отмеченные ячейки"}</summary>
      <div id="timeline-events">${eventDate && !eventCount
        ? '<p class="caption">В предпросмотре нет отмеченных сроков на выбранную дату.</p>'
        : eventTable(visible, eventCount)}</div>
      ${focus || eventDate ? button("Все отмеченные сроки", "timeline-reset") : ""}
    </details>` : `<p class="caption">${enabled.length ? "Для выбранных слоёв нет изменений." : "Выберите слой на шкале."}</p>`}
    ${preview.warnings?.length ? `<div class="preview-warnings">${preview.warnings.map((message) => note(message, true)).join("")}</div>` : ""}
    ${manualCsvControls(file, preview, editor, checked && !editor.busy)}
    <div class="between publish-bar">
      <div>${checked && !editor.busy ? link("Скачать отфильтрованный файл", download, { glyph: "download" }) :
        button("Скачать отфильтрованный файл", "excel", { glyph: "download", disabled: true })}
        <p class="muted small">XLSX с подсветкой очищенных ячеек</p></div>
      ${button(editor.busy === "confirm" ? "Сохранение…" : file.confirmed ? "Заменить в базе" : "Добавить в базу",
        "publish", { type: "accent", glyph: "arrow", disabled: !checked || Boolean(editor.busy) })}
    </div>`, "anchor preview-card-shell");
}

function eventsOnDate(preview, parameters, enabled, date, visibleCount) {
  if (preview.timeline_days > 1) return visibleCount;
  const day = preview.timeline.find(({ time }) => time === date);
  if (!day || (enabled.includes("filled") && day.filled && !day.filled_parameters)) return visibleCount;
  const fields = { marked: day.parameters, filled: day.filled_parameters, manual: day.manual_parameters };
  return enabled.reduce((total, kind) =>
    total + parameters.reduce((count, { id }) => count + (fields[kind]?.[id] ?? 0), 0), 0);
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

const HOUR = 3_600_000;

function sumCounts(counts) {
  return Object.values(counts).reduce((total, count) => total + count, 0);
}

function timelineData(preview, file, parameters, events, totals) {
  const start = stamp(file.start.slice(0, 10));
  const end = stamp(file.end.slice(0, 10));
  const days = Math.round((end - start) / DAY) + 1;
  const filledEvents = events.filter(({ kind }) => kind === "filled");
  const completeFills = filledEvents.length === totals.filled;
  const hourly = days <= 2 && completeFills && events.length === totals.filled + totals.marked + totals.manual;
  const periodDays = preview.timeline_days ?? 1;
  const months = days > 95 && periodDays === 1;
  const monthNumber = (time) => Number(time.slice(0, 4)) * 12 + Number(time.slice(5, 7)) - 1;
  const startMonth = monthNumber(file.start);
  const size = hourly ? days * 24 :
    months ? monthNumber(file.end) - startMonth + 1 : Math.ceil(days / periodDays);
  const indexFor = (time) => {
    if (hourly) return Math.floor((stamp(time) - start) / HOUR);
    if (months) return monthNumber(time) - startMonth;
    return Math.floor((stamp(time.slice(0, 10)) - start) / (DAY * periodDays));
  };
  const isoAt = (index) => {
    if (months) {
      const month = startMonth + index;
      return `${Math.floor(month / 12)}-${String(month % 12 + 1).padStart(2, "0")}`;
    }
    return new Date(start + index * (hourly ? HOUR : DAY * periodDays)).toISOString();
  };
  const axisLabel = (index) => {
    const iso = isoAt(index);
    if (hourly) return `${days > 1 ? dayLabel(iso) + " " : ""}${iso.slice(11, 16)}`;
    return months ? `${iso.slice(5)}/${iso.slice(2, 4)}` : dayLabel(iso);
  };
  const periodLabel = (index) => {
    const iso = isoAt(index);
    if (hourly) return `${dateFormat.format(stamp(iso))} · ${iso.slice(11, 16)}–${iso.slice(11, 13)}:59`;
    if (months) return `${iso.slice(5)}.${iso.slice(0, 4)}`;
    const last = new Date(Math.min(end, stamp(iso) + (periodDays - 1) * DAY)).toISOString();
    return spanLabel(iso, last);
  };
  const selected = new Set(parameters.map(({ id }) => id));
  const all = selected.size === preview.params.length;
  const groups = new Map();
  function add(time, kind, parameter, count) {
    const index = indexFor(time);
    if (!count || index < 0 || index >= size) return;
    if (!groups.has(index)) groups.set(index, { marked: {}, filled: {}, manual: {} });
    const counts = groups.get(index)[kind];
    counts[parameter] = (counts[parameter] ?? 0) + count;
  }
  let incomplete = false;
  if (hourly) {
    for (const event of events) add(event.time, event.kind, event.parameter.id, 1);
  } else {
    const oldFill = preview.timeline.some((row) => row.filled && !row.filled_parameters);
    for (const row of preview.timeline) {
      for (const [id, count] of Object.entries(row.parameters)) {
        if (selected.has(id)) add(row.time, "marked", id, count);
      }
      for (const [id, count] of Object.entries(row.manual_parameters ?? {})) {
        if (selected.has(id)) add(row.time, "manual", id, count);
      }
      if (!oldFill) {
        for (const [id, count] of Object.entries(row.filled_parameters ?? {})) {
          if (selected.has(id)) add(row.time, "filled", id, count);
        }
      } else if (!completeFills && all) {
        add(row.time, "filled", "all", row.filled);
      }
    }
    if (oldFill && completeFills) {
      for (const event of filledEvents) add(event.time, "filled", event.parameter.id, 1);
    }
    incomplete = oldFill && !completeFills && !all && totals.filled > 0;
  }
  return { groups, size, indexFor, axisLabel, periodLabel, incomplete };
}

function outlierTimeline(timeline, parameters, enabled, focus) {
  const { left, right, w, top, bottom } = chartBox;
  const middle = (top + bottom) / 2;
  const width = chartWidth / timeline.size;
  const barWidth = Math.min(26, width * 0.71);
  let magnitude = 1;
  const marks = [];
  const labels = new Map(parameters.map(({ id, label }) => [id, label]));
  for (const [bucket, group] of [...timeline.groups].sort(([first], [second]) => first - second)) {
    let lower = 0;
    for (const kind of enabled) {
      const counts = group[kind];
      const total = sumCounts(counts);
      if (!total) continue;
      const first = kind === "marked" ? 0 : -lower;
      const last = kind === "marked" ? total : first - total;
      if (kind !== "marked") lower += total;
      magnitude = Math.max(magnitude, Math.abs(last));
      const breakdown = Object.entries(counts).map(([id, count]) => `${labels.get(id) ?? "Все параметры"} · ${fmt(count)}`).join(", ");
      const label = `${timeline.periodLabel(bucket)} · ${timelineLayers[kind].label} · ${breakdown}`;
      marks.push({ bucket, kind, first, last, label });
    }
  }
  const stride = Math.max(1, Math.ceil(timeline.size / 11));
  const step = Math.max(1, Math.ceil(magnitude / 2));
  const y = (value) => middle - value / (2 * step) * (middle - top);
  const grid = [-2, -1, 0, 1, 2].map((factor) => gridLine(Math.abs(step * factor), y(step * factor))).join("");
  const bars = marks.map(({ bucket, kind, first, last, label }) => {
    const x = left + (bucket + 0.5) * width;
    const a = y(first);
    const b = y(last);
    const active = Boolean(focus && bucket >= focus.bucket && bucket <= (focus.endBucket ?? focus.bucket)
      && (!focus.kind || focus.kind === kind));
    return `<rect class="timeline-event" data-file-action="timeline-event" data-file-id="${kind}:${bucket}" role="button"
      tabindex="0" aria-label="${escapeHtml(label)}" aria-pressed="${active}" x="${x - barWidth / 2}" width="${barWidth}" y="${Math.min(a, b)}"
      height="${Math.abs(a - b)}" fill="${timelineLayers[kind].color}"><title>${escapeHtml(label)}</title></rect>`;
  }).join("");
  const axis = [];
  for (let index = 0; index < timeline.size; index += stride) {
    axis.push(axisLabel(timeline.axisLabel(index), left + (index + 0.5) * width, index === 0 && timeline.size > 14 ? "start" : "middle"));
  }
  const zero = `<path d="M${left} ${middle}H${w - right}" class="timeline-zero"/>`;
  return `<svg class="graph timeline-graph" viewBox="0 0 ${w} ${chartBox.h}" role="group" aria-label="Выбросы и заполнения на общей шкале времени">${grid}${zero}${bars}${axis.join("")}</svg>`;
}

function outlierTrace(points, parameter, enabled) {
  const sample = points.map((point) => ({ ...point, stamp: stamp(point.time) }))
    .filter((point) => Number.isFinite(point.stamp)
      && (Number.isFinite(point.value) || Number.isFinite(point.estimate?.value) || Number.isFinite(point.manual_value)))
    .sort((first, last) => first.stamp - last.stamp);
  if (!sample.length) return '<div class="empty-chart"><h3>Нет наблюдений по этому параметру</h3></div>';

  const { left, top, bottom } = chartBox;
  const color = colorFor(parameter.id);
  const observationColor = "#6b8f79";
  const restoredColor = timelineLayers.filled.color;
  const manualColor = timelineLayers.manual.color;
  const start = sample[0].stamp;
  const span = sample.at(-1).stamp - start;
  const x = (time) => left + (span ? (time - start) / span : 0.5) * chartWidth;
  let low = Infinity;
  let high = -Infinity;
  for (const point of sample) {
    for (const value of [point.value, point.estimate?.value, point.manual_value]) {
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
      if (point.marked && enabled.includes("marked")) {
        const label = [point.time, `${fmt(point.value, 2)} ${parameter.unit}`, "к удалению", ...(point.reasons ?? [])].join(" · ");
        highlighted.push(`<circle cx="${position}" cy="${y(point.value)}" r="4.1" fill="${color}" stroke="#fff" stroke-width="1.2"><title>${escapeHtml(label)}</title></circle>`);
      } else if (!point.marked) {
        dots.push(`<circle cx="${position}" cy="${y(point.value)}" r="1.8" fill="${observationColor}" opacity=".64"/>`);
      }
    }
    if (enabled.includes("filled") && Number.isFinite(point.estimate?.value)) {
      const label = `${point.time} · ${fmt(point.estimate.value, 2)} ${parameter.unit} · ${point.estimate.source}`;
      highlighted.push(`<circle cx="${position}" cy="${y(point.estimate.value)}" r="4.1" fill="${restoredColor}" stroke="#fff" stroke-width="1.2"><title>${escapeHtml(label)}</title></circle>`);
    }
    if (enabled.includes("manual") && point.manual && Number.isFinite(point.manual_value)) {
      const label = `${point.time} · ${fmt(point.manual_value, 2)} ${parameter.unit} · Исправлено в загруженном CSV`;
      highlighted.push(`<circle cx="${position}" cy="${y(point.manual_value)}" r="4.1" fill="${manualColor}" stroke="#fff" stroke-width="1.2"><title>${escapeHtml(label)}</title></circle>`);
    }
  }
  const estimates = enabled.includes("filled") && sample.some((point) => Number.isFinite(point.estimate?.value));
  const manual = enabled.includes("manual") && sample.some((point) => point.manual && Number.isFinite(point.manual_value));
  return `<span class="small muted">${escapeHtml(parameter.unit)}</span>
    ${chartSvg(`${parameter.label} с выделенными выбросами`, grid, axis.join("") + dots.join("") + highlighted.join(""))}
    <div class="legend">${legendItem("Наблюдения", observationColor)}${enabled.includes("marked") ? legendItem("К удалению", color) : ""}
      ${estimates ? legendItem("Заполненные значения", restoredColor) : ""}
      ${manual ? legendItem("Ручные правки", manualColor) : ""}</div>`;
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
    saveStations(catalog);
    state.filters = listing.filters.filter(({ id }) => id !== "buddy");
    state.primary = listing.primary;
    state.loading = false;
  } catch (error) {
    state.message = messageFor(error);
    state.error = true;
  }
  render();
}

function coordinateInput(value) {
  const text = value.trim().replace(",", ".");
  return /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text) ? Number(text) : NaN;
}

function newPassport() {
  return {
    label: state.newStation.label.trim(),
    latitude: coordinateInput(state.newStation.latitude),
    longitude: coordinateInput(state.newStation.longitude),
    profile: state.newStation.profile,
  };
}

function validUploadStation() {
  if (state.uploadStation !== "new") return true;
  const passport = newPassport();
  const fields = [
    ["label", Boolean(passport.label), "Введите название станции."],
    ["latitude", Math.abs(passport.latitude) <= 90, "Введите широту от −90 до 90."],
    ["longitude", Math.abs(passport.longitude) <= 180, "Введите долготу от −180 до 180."],
  ];
  for (const [key, valid, message] of fields) {
    const field = document.getElementById(`upload-station-${key}`);
    field.setCustomValidity(valid ? "" : message);
    if (!field.reportValidity()) return false;
  }
  return true;
}

function samePassport(station, passport) {
  return station.label.toLocaleLowerCase("ru-RU") === passport.label.toLocaleLowerCase("ru-RU")
    && station.latitude === passport.latitude && station.longitude === passport.longitude && station.profile === passport.profile;
}

async function stationForBatch(batch) {
  const { passport } = batch;
  const existing = state.stations.find((station) => samePassport(station, passport));
  if (existing) return existing;
  try {
    const { station } = await jsonRequest("/api/stations", jsonPost(passport));
    state.stations.push(station);
    notifyDataChange();
    return station;
  } catch (error) {
    // Another tab or a previous request may already have saved this passport.
    try {
      saveStations(await jsonRequest("/api/stations"));
    } catch {
      throw error;
    }
    const registered = state.stations.find((station) => samePassport(station, passport));
    if (registered) return registered;
    throw error;
  }
}

function appendUploads(files, selected = state.uploadStation) {
  const batch = selected === "new" ? { passport: { ...newPassport(), id: `station-${crypto.randomUUID()}` } } : { station: selected };
  for (const file of files) state.transfers.push({ file, batch, filename: file.name, status: "queued" });
  if (!state.editor) render();
  if (!state.uploading) void uploadNext();
}

async function uploadNext() {
  state.uploading = true;
  while (state.transfers.length) {
    const transfer = state.transfers[0];
    const { file, filename, batch } = transfer;
    transfer.status = "loading";
    if (!state.editor) render();
    try {
      if (!/\.(csv|xlsx)$/i.test(filename)) throw new Error("Поддерживаются форматы CSV и XLSX.");
      if (!batch.station && !batch.error) {
        try {
          const station = await stationForBatch(batch);
          batch.station = station.id;
          if (state.uploadStation === "new" && samePassport(station, newPassport())) state.uploadStation = station.id;
        } catch (error) {
          batch.error = error;
        }
      }
      if (batch.error) throw batch.error;
      const contentType = filename.toLowerCase().endsWith(".xlsx")
        ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv";
      const result = await jsonRequest(filesUrl("upload", { filename, station: batch.station }), {
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
    appendUploads([new File([csv], "kr1-station-2024-04-01.csv", { type: "text/csv" })], "kr1");
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
    id, settings: settingsFor(file), preview: null, parameters: new Set(), hiddenLayers: new Set(), message: "",
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
  if (!validateRanges(editor)) return;
  if (editor.settings.fill_external && !editor.settings.timezone) {
    editor.message = "Укажите часовой пояс исходного файла.";
    render();
    document.getElementById("file-timezone").focus();
    return;
  }
  editor.busy = "preview";
  editor.message = "";
  render();
  try {
    const allowed = new Set(state.filters.map(({ id }) => id));
    const settings = { ...editor.settings, filters: editor.settings.filters.filter((id) => allowed.has(id)) };
    const result = await jsonRequest(filesUrl("preview"), jsonPost({ id: editor.id, ...settingsPayload(settings) }));
    applyPreview(editor, result);
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

function applyPreview(editor, result) {
  const file = saveRecord(result);
  editor.preview = result;
  editor.settings = settingsFor(file);
  editor.parameters.clear();
  editor.hiddenLayers.clear();
  editor.focusEvent = null;
  editor.eventDate = null;
}

async function uploadCorrectedCsv(editor, selected) {
  if (editor.busy || !checkedPreview(editor)) return;
  if (!selected.name.toLowerCase().endsWith(".csv")) {
    editor.message = "Для ручной правки выберите файл CSV.";
    render();
    return;
  }
  editor.busy = "corrected";
  editor.message = "";
  render();
  try {
    const url = filesUrl("corrected", { id: editor.id, revision: editor.preview.revision, filename: selected.name });
    const result = await jsonRequest(url, { method: "POST", headers: { "Content-Type": "text/csv" }, body: selected });
    applyPreview(editor, result);
  } catch (error) {
    editor.message = messageFor(error);
  } finally {
    editor.busy = null;
    if (state.editor === editor) {
      render();
      main.querySelector(editor.message ? '[role="alert"]' : ".manual-csv")?.scrollIntoView({ block: "nearest" });
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
    saveRecord(await jsonRequest(filesUrl("confirm"), jsonPost({
      id: editor.id, revision: editor.preview.revision, ...settingsPayload(editor.settings),
    })));
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
  "file-station": "station", "file-timezone": "timezone",
  "file-subhour": "subhour", "file-pressure": "pressure_reference",
  "file-conflicts": "conflict_policy",
};

function validateRanges(editor) {
  const params = state.files.get(editor.id).params ?? [];
  for (const { id, label } of params) {
    const range = editor.settings.ranges[id];
    if (!range) continue;
    const [low, high] = range;
    if ([low, high].every((value) => value !== "" && Number.isFinite(Number(value))) && Number(low) <= Number(high)) continue;
    editor.boundsOpen = true;
    editor.message = `${label}. Введите две границы. Минимум не должен превышать максимум.`;
    render();
    document.getElementById(`bound-${id}-${low === "" ? 0 : 1}`)?.focus();
    return false;
  }
  for (const [method, values] of Object.entries(editor.settings.thresholds)) {
    const upper = method === "stuck" ? state.primary.max_stuck_hours : state.primary.max_absolute_bound;
    for (const { id, label } of params) {
      if (!Object.hasOwn(values, id) || (Number(values[id]) > 0 && Number(values[id]) <= upper)) continue;
      if (!editor.settings.filters.includes(method)) continue;
      editor.boundsOpen = true;
      editor.message = `${filterText[method][0]}, ${label.toLocaleLowerCase("ru-RU")}. Порог должен быть больше 0 и не превышать ${upper}.`;
      render();
      document.getElementById(`threshold-${method}-${id}`)?.focus();
      return false;
    }
  }
  return true;
}

function updatePreviewPresentation(editor, control) {
  if (!editor?.preview) return;
  const id = control.dataset.fileId;
  const action = control.dataset.fileAction;
  editor.focusEvent = null;
  if (action === "outlier") {
    if (id === "all") editor.parameters.clear();
    else if (!editor.parameters.delete(id)) editor.parameters.add(id);
  } else if (action === "timeline-layer") {
    if (!editor.hiddenLayers.delete(id)) editor.hiddenLayers.add(id);
  } else if (action === "timeline-event") {
    const [kind, bucket] = id.split(":");
    editor.eventDate = null;
    editor.focusEvent = { kind, bucket: Number(bucket) };
  } else if (action === "timeline-reset") {
    editor.eventDate = null;
  }
  const current = main.querySelector(".preview-card-shell");
  const wasOpen = current.querySelector(".outlier-details")?.open;
  current.outerHTML = previewCard(editor, checkedPreview(editor), state.files.get(editor.id));
  const details = main.querySelector(".outlier-details");
  if (details && wasOpen) details.open = true;
  const next = [...main.querySelectorAll(`[data-file-action="${action}"]`)].find((item) => item.dataset.fileId === id);
  (next ?? details?.querySelector("summary"))?.focus({ preventScroll: true });
}

main.addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "upload-station") {
    state.uploadStation = target.value;
    render();
    document.getElementById("upload-station").focus({ preventScroll: true });
    return;
  }
  if (target.id === "upload-station-profile") {
    state.newStation.profile = target.value;
    return;
  }
  const editor = state.editor;
  if (!editor || editor.busy || editor.done) return;
  const { method, fill } = target.dataset;
  const key = settingFields[target.id];
  let focused = target.id;
  if (target.id === "file-pressure-max") {
    if (target.value === "manual") {
      editor.boundsOpen = true;
      focused = "bound-pressure_hpa-1";
    } else {
      const [lower] = editor.settings.ranges.pressure_hpa;
      editor.settings.ranges.pressure_hpa = [lower, Number(target.value)];
    }
  } else if (method) {
    const ids = editor.settings.filters;
    editor.settings.filters = target.checked ? [...ids, method] : ids.filter((id) => id !== method);
  } else if (fill) {
    editor.settings[fill] = target.checked;
  } else if (key) {
    editor.settings[key] = target.value;
  } else {
    return;
  }
  editor.message = "";
  render();
  const controls = method ? [...main.querySelectorAll("[data-method]")].find((input) => input.dataset.method === method) :
    document.getElementById(focused);
  controls?.focus({ preventScroll: true });
});

main.addEventListener("input", (event) => {
  const target = event.target;
  const { newStation, bound, boundIndex, threshold, thresholdParam } = target.dataset;
  if (newStation) {
    state.newStation[newStation] = target.value;
    target.setCustomValidity("");
    return;
  }
  const editor = state.editor;
  if ((!bound && !threshold) || !editor || editor.busy || editor.done) return;
  editor.message = "";
  main.querySelector(".file-editor-feedback")?.remove();
  const checked = checkedPreview(editor);
  if (threshold) {
    editor.settings.thresholds[threshold][thresholdParam] = target.value;
  } else {
    const values = editor.settings.ranges[bound].slice();
    values[Number(boundIndex)] = target.value;
    editor.settings.ranges[bound] = values;
    if (bound === "pressure_hpa" && boundIndex === "1") {
      document.getElementById("file-pressure-max").value = ["1050", "1550"].includes(target.value) ? target.value : "manual";
    }
  }
  document.getElementById("processing-status").textContent = processingStatus(editor);
  if (checked !== checkedPreview(editor)) {
    main.querySelector(".preview-card-shell").outerHTML = previewCard(editor, !checked, state.files.get(editor.id));
  }
});

main.addEventListener("toggle", (event) => {
  if (state.editor && event.target.isConnected && event.target.matches(".file-bounds")) state.editor.boundsOpen = event.target.open;
}, true);

main.addEventListener("submit", (event) => {
  const editor = state.editor;
  if (!editor?.preview || !event.target.matches(".timeline-date")) return;
  event.preventDefault();
  editor.eventDate = event.target.querySelector("input").value;
  editor.focusEvent = null;
  main.querySelector(".preview-card-shell").outerHTML = previewCard(editor, checkedPreview(editor), state.files.get(editor.id));
  main.querySelector(".outlier-details summary")?.focus({ preventScroll: true });
});

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-file-action]");
  if (!control) return;
  const action = control.dataset.fileAction;
  const id = control.dataset.fileId;
  const file = state.files.get(id);
  const editor = state.editor;
  switch (action) {
    case "browse": if (validUploadStation()) picker.click(); break;
    case "corrected-browse": correctedPicker.click(); break;
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
    case "reset-bounds":
      if (editor && !editor.busy) {
        editor.settings.ranges = effectiveRanges();
        editor.settings.thresholds = effectiveThresholds();
        editor.message = "";
        render();
        main.querySelector(".file-bounds summary")?.focus({ preventScroll: true });
      }
      break;
    case "outlier":
    case "timeline-layer":
    case "timeline-event":
    case "timeline-reset":
      updatePreviewPresentation(editor, control);
      break;
  }
});

main.addEventListener("keydown", (event) => {
  const eventMark = event.target.closest('[data-file-action="timeline-event"]');
  if (!eventMark || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  updatePreviewPresentation(state.editor, eventMark);
});

dialog.addEventListener("cancel", (event) => {
  if (dialog.getAttribute("aria-busy") === "true") event.preventDefault();
});

picker.addEventListener("change", () => {
  if (validUploadStation()) appendUploads(picker.files);
  picker.value = "";
});

correctedPicker.addEventListener("change", () => {
  const selected = correctedPicker.files[0];
  correctedPicker.value = "";
  if (state.editor && selected) void uploadCorrectedCsv(state.editor, selected);
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
  if (!state.loading && event.dataTransfer.files.length && validUploadStation()) appendUploads(event.dataTransfer.files);
});

void loadList();
