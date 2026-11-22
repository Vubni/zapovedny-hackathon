const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const images = path.join(root, "results", "browser");
const csvName = "Кр1 — температура и влажность.csv";
const xlsxName = "Кр1 — наблюдения за 15 февраля.xlsx";
const failureName = "Файл с неверными полями.csv";
const csv = Buffer.from([
  "datetime,t_air,rh",
  ...Array.from({ length: 24 }, (_, hour) =>
    `2026-02-14 ${String(hour).padStart(2, "0")}:00,${hour === 7 ? -100 : 4 + hour % 3},${hour === 8 ? 120 : hour === 12 ? "" : 78}`),
  "",
].join("\n"));

const qaServer = String.raw`
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from checks.test_observation_workflow import SERVER, workbook_bytes
from external_fill import ModelData, _metadata

def known_weather(model, start, end, snapshot, point, parameter=None):
    first = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    hours = ((end - start).days + 1) * 24
    defaults = {"rh": (79.0,) * 3, "pressure_hpa": (1010.0,) * 3}
    temperatures = (5.0, 3.0, 7.0) if model.id == "era5" else (4.2, 5.2, 6.2)
    series = temperatures if parameter == "t_air" else defaults[parameter]
    values = {first + timedelta(hours=hour): (series[hour % 3],) * 2 for hour in range(hours)}
    return ModelData(dict(_metadata(model, start, end, snapshot, point, parameter), status="available",
                          coordinates={"latitude": point[0], "longitude": point[1]}),
                     {parameter: values})

rows = [["datetime", "t_air", "pressure_hpa"]]
rows += [[f"2026-02-15 {hour:02}:00", 5, 1100 if hour == 8 else 1010] for hour in range(24)]
Path(sys.argv[1]).write_bytes(workbook_bytes(rows))
# Keep the actual HTTP, coordinate handling, candidate eligibility and correction policy.
# Only the remote provider responses are replaced for this isolated browser test.
with patch("external_fill._cached_model", side_effect=known_weather):
    exec(SERVER)
`;

async function fileRecord(page, base, name) {
  const response = await page.request.get(`${base}/api/files`);
  assert.equal(response.status(), 200);
  return (await response.json()).files.find((file) => file.filename === name);
}

async function download(page, trigger) {
  const [item] = await Promise.all([page.waitForEvent("download"), trigger.click()]);
  return { name: item.suggestedFilename(), bytes: await fs.readFile(await item.path()) };
}

async function correctTemperature(page, name, value) {
  const exported = await download(page, page.getByRole("link", { name: "Скачать CSV для правки" }));
  assert.match(exported.name, /\.csv$/);
  const lines = exported.bytes.toString("utf8").replace(/^\uFEFF/, "").trimEnd().split(/\r?\n/).map((line) => line.split(";"));
  const index = lines[0].indexOf("t_air");
  assert.ok(index > 0);
  const target = lines.find((line) => /^2026-02-14[ T]07:00/.test(line[0]));
  assert.ok(target);
  target[index] = value;
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.getByRole("button", { name: "Загрузить исправленный CSV" }).click(),
  ]);
  const response = page.waitForResponse((event) => new URL(event.url()).pathname === "/api/files/corrected" &&
    event.request().method() === "POST");
  await chooser.setFiles([{
    name, mimeType: "text/csv", buffer: Buffer.from(lines.map((line) => line.join(";")).join("\n") + "\n"),
  }]);
  const imported = await response;
  assert.equal(imported.status(), 200, await imported.text());
  return imported.json();
}

async function applyFilters(page) {
  const [response] = await Promise.all([
    page.waitForResponse((event) => new URL(event.url()).pathname === "/api/files/preview" && event.request().method() === "POST"),
    page.getByRole("button", { name: "Отфильтровать", exact: true }).click(),
  ]);
  assert.ok(response.ok(), await response.text());
  await page.locator(".preview-card-shell").waitFor();
}

async function publish(page, label) {
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("heading", { name: "Данные добавлены в общие графики" }).waitFor();
  await page.getByRole("button", { name: "К очереди", exact: true }).click();
}

async function prepareFullExcel(page, fileId, parameter) {
  await page.getByRole("button", { name: "Excel с ручными решениями" }).click();
  const dialog = page.getByRole("dialog", { name: "Excel с ручными решениями" });
  await dialog.getByLabel("Показатель", { exact: true }).selectOption(parameter);
  const [response] = await Promise.all([
    page.waitForResponse((event) => {
      const url = new URL(event.url());
      return event.request().method() === "POST" && url.pathname === "/api/review"
        && url.searchParams.get("file_id") === fileId;
    }),
    dialog.getByRole("button", { name: "Подготовить Excel" }).click(),
  ]);
  assert.equal(response.status(), 200, await response.text());
  await dialog.getByRole("link", { name: "Скачать Excel с решениями" }).waitFor();
  return { dialog, report: await response.json() };
}

async function verifyJsonImport(page, base) {
  const name = "kr1-station-2024-04-01.json";
  const sample = await fs.readFile(path.join(root, "results", "examples", name));
  const redirects = await Promise.all(["/import", "/import.html"].map((route) =>
    page.request.get(base + route, { maxRedirects: 0 })));
  for (const redirect of redirects) {
    assert.equal(redirect.status(), 303);
    assert.equal(redirect.headers().location, "/files?source=upload");
  }
  await page.goto(base + "/import");
  await page.waitForURL(base + "/files?source=upload");
  assert.match(await page.locator("#fileInput").getAttribute("accept"), /application\/json/);
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.getByRole("button", { name: "Выбрать файлы" }).click(),
  ]);
  await chooser.setFiles([
    { name, mimeType: "application/json", buffer: sample },
    { name: "Несохранённый JSON.json", mimeType: "application/json", buffer: Buffer.from("{") },
  ]);
  const queued = page.locator(".queue-item").filter({ hasText: name });
  await queued.getByText(/Кр1 · поляна · 48 строк/).waitFor();
  await page.getByText(/Не удалось прочитать JSON. Ошибка в строке 1/).waitFor();
  const file = await fileRecord(page, base, name);
  assert.equal(file.station, "kr1");
  assert.equal(file.rows, 48);
  assert.equal(file.start, "2024-04-01");
  assert.equal(file.end, "2024-04-01");
  assert.equal(file.params.find(({ id }) => id === "pressure_hpa").unit, "гПа");

  await queued.getByRole("button", { name: "Настроить" }).click();
  const station = page.getByLabel("Метеостанция", { exact: true });
  await station.selectOption("ame60");
  await page.getByText(/У выбранной станции нет следующих показателей файла.*атмосферное давление/).waitFor();
  await station.selectOption("kr1");
  await page.getByRole("button", { name: "Исходный JSON" }).click();
  const dialog = page.getByRole("dialog", { name });
  await dialog.getByText("Начальные строки исходного файла.").waitFor();
  assert.match(await dialog.locator("pre").innerText(), /"datetime".*"2024-04-01 00:02"/s);
  const original = await download(page, dialog.getByRole("link", { name: "Скачать исходный JSON" }));
  assert.equal(original.name, name);
  assert.deepEqual(original.bytes, sample);
  const downloadResponse = await page.request.get(`${base}/api/files/original?id=${file.id}`);
  assert.match(downloadResponse.headers()["content-type"], /^application\/json\b/);
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "hidden" });

  await applyFilters(page);
  await page.getByRole("button", { name: "Атмосферное давление 10" }).click();
  await page.getByRole("img", { name: "Атмосферное давление с выделенными выбросами" }).waitFor();
  const processed = await fileRecord(page, base, name);
  const preview = await page.request.get(`${base}/api/files/preview?id=${file.id}&revision=${processed.preview.revision}`);
  assert.equal(preview.status(), 200);
  const report = await preview.json();
  const accepted = report.points.pressure_hpa.filter(({ value, marked }) => Number.isFinite(value) && !marked);
  const mean = accepted.reduce((sum, { value }) => sum + value, 0) / accepted.length;
  const legacy = await page.request.post(base + "/api/import?station=kr1&year=2024&param=pressure_hpa&grain=day", {
    headers: { "Content-Type": "application/json", Origin: base }, data: sample,
  });
  assert.equal(legacy.status(), 200);
  const prior = await legacy.json();
  assert.equal(prior.raw_rows, file.rows);
  assert.deepEqual([prior.rows[0].n, prior.rows[0].flagged], [38, 10]);
  assert.equal(accepted.length, prior.rows[0].n);
  assert.equal(report.stats.parameters.pressure_hpa.marked, prior.rows[0].flagged);
  assert.ok(Math.abs(mean - prior.rows[0].mean) < 1e-9);
  assert.ok(Math.abs(mean - 1005.45) < 1e-9);
  assert.ok(report.points.pressure_hpa.some(({ value, marked, qc_flag }) => value === 770.1 && marked && qc_flag === "pressure_hpa:range"));
  assert.equal(processed.confirmed, null);
}

async function runChecks(page, base, workbook) {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const queueItem = (name) => page.locator(".queue-item").filter({ hasText: name });
  const row = (name) => page.getByRole("row").filter({ hasText: name });
  const station = page.getByLabel("Метеостанция", { exact: true });
  const hampel = page.getByRole("checkbox", { name: /^Значение выбивается из ряда/ });
  const spike = page.getByRole("checkbox", { name: /^Скачок с возвратом/ });
  const external = page.getByRole("checkbox", { name: /^Сверить с метеосервисами/ });
  const model = page.getByRole("checkbox", { name: /^Восстановить по данным станции/ });
  const add = page.getByRole("button", { name: "Добавить в базу", exact: true });
  const toFiles = page.getByRole("button", { name: "К файлам", exact: true });
  const pressure = page.getByLabel("Верхняя граница давления", { exact: true });
  const timeline = page.getByRole("group", { name: "Выбросы и заполнения на общей шкале времени" });
  const marks = timeline.locator(".timeline-event");
  const temperature = page.getByRole("button", { name: "Температура воздуха 1", exact: true });
  const humidity = page.getByRole("button", { name: "Относительная влажность 1", exact: true });

  const otherTab = await page.context().newPage();
  await otherTab.goto(`${base}/api`);
  await otherTab.evaluate(() => {
    window.dataRevisions = [];
    window.addEventListener("storage", (event) => {
      if (event.key === "meteoarchive.dataRevision") window.dataRevisions.push(event.newValue);
    });
  });

  await page.goto(`${base}/files?source=observations&station=kr1&param=t_air&start=2025-07-01&end=2025-07-03`);
  assert.equal(new URL(page.url()).pathname, "/files");
  assert.equal((await page.request.get(`${base}/api/files`)).status(), 200);
  await page.getByText(/Ориентир «Красное»/).waitFor();
  assert.equal(await page.getByLabel("Станция", { exact: true }).inputValue(), "kr1");
  assert.equal(await page.getByLabel("Показатель", { exact: true }).inputValue(), "t_air");
  assert.equal(await page.getByLabel("Дата с", { exact: true }).inputValue(), "2025-07-01");
  assert.equal(await page.getByLabel("Дата по", { exact: true }).inputValue(), "2025-07-03");
  assert.equal(await page.getByRole("button", { name: /Попробовать на примере/ }).count(), 0);
  async function prepareSelection() {
    const [prepared] = await Promise.all([
      page.waitForResponse((response) => new URL(response.url()).pathname === "/api/files/from-observations"),
      page.getByRole("button", { name: "Подготовить данные" }).click(),
    ]);
    assert.ok(prepared.ok(), await prepared.text());
    await page.getByRole("heading", { name: "Обработка наблюдений" }).waitFor();
    await page.locator(".preview-card-shell").waitFor();
    return prepared.json();
  }
  const archived = await prepareSelection();
  assert.equal(archived.provenance.kind, "observations");
  assert.ok(archived.provenance.measurements > 0);
  assert.equal(archived.confirmed, null);
  assert.equal(await station.isDisabled(), true);
  await page.getByRole("button", { name: "Исходный CSV" }).click();
  const source = page.getByRole("dialog");
  await source.getByText("Начальные строки исходного файла.").waitFor();
  assert.match(await source.locator("pre").innerText(), /datetime.*t_air.*origin/);
  await page.keyboard.press("Escape");
  await source.waitFor({ state: "hidden" });
  await model.check();
  await page.getByLabel("Показатель для заполнения").selectOption("t_air");
  await page.getByLabel("Заполнение с").fill("2025-07-02");
  await page.getByLabel("Заполнение по").fill("2025-07-02");
  await applyFilters(page);
  await page.waitForFunction(() => document.querySelector("[data-file-action=publish]")?.disabled === false);
  const scoped = await fileRecord(page, base, archived.filename);
  assert.equal(scoped.start, "2025-07-01");
  assert.equal(scoped.preview.settings.fill_start, "2025-07-02");
  assert.equal(scoped.preview.settings.fill_end, "2025-07-02");
  assert.deepEqual(scoped.preview.settings.fill_params, ["t_air"]);
  await toFiles.click();
  assert.equal((await prepareSelection()).id, archived.id);
  await toFiles.click();

  const uploadMode = page.getByRole("button", { name: "Загрузить файлы", exact: true });
  await uploadMode.click();
  await page.getByRole("button", { name: "Выбрать файлы", exact: true }).waitFor();
  assert.equal(await page.locator('a[href="/review"], a[href="/operator"]').count(), 0);
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.getByRole("button", { name: "Выбрать файлы", exact: true }).click(),
  ]);
  assert.equal(chooser.isMultiple(), true);
  await chooser.setFiles([
    { name: csvName, mimeType: "text/csv", buffer: csv },
    { name: xlsxName, mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: workbook },
    { name: failureName, mimeType: "text/csv", buffer: Buffer.from("datetime,unknown\n2026-02-16 00:00,5\n") },
  ]);
  await queueItem(csvName).getByText("Ожидает обработки").waitFor();
  await queueItem(xlsxName).getByText("Ожидает обработки").waitFor();
  await queueItem(failureName).getByText("Ошибка чтения").waitFor();
  const csvRecord = await fileRecord(page, base, csvName);
  assert.equal(csvRecord.rows, 24);
  assert.equal((await fileRecord(page, base, xlsxName)).rows, 24);
  await queueItem(csvName).getByRole("button", { name: "Настроить" }).click();
  assert.equal(await pressure.count(), 0);
  assert.equal(await page.getByRole("checkbox", { name: /сверка с соседней станцией/i }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Excel с ручными решениями" }).count(), 1);
  await station.selectOption("kr2");
  await hampel.check();
  await applyFilters(page);
  await page.getByText("2 выброса", { exact: true }).waitFor();
  assert.equal(await marks.count(), 2);

  const draft = await fileRecord(page, base, csvName);
  assert.equal(draft.status, "preview");
  assert.equal(draft.confirmed, null);
  assert.equal(draft.preview.stats.marked, 2);
  assert.equal((await fileRecord(page, base, xlsxName)).preview, null);
  assert.equal(await otherTab.evaluate(() => window.dataRevisions.length), 0);

  await temperature.click();
  const graph = page.getByRole("img", { name: "Температура воздуха с выделенными выбросами" });
  assert.match(await graph.locator("circle title").textContent(), /100.+к удалению/);
  await page.locator(".outlier-details summary").click();
  await page.getByRole("row").filter({ hasText: "2026-02-14 07:00" }).waitFor();
  await spike.check();
  await page.getByText("Настройки изменились. Запустите фильтрацию повторно.").waitFor();
  assert.equal(await add.isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "Скачать отфильтрованный файл" }).isDisabled(), true);
  await toFiles.click();
  await queueItem(xlsxName).getByRole("button", { name: "Настроить" }).click();
  assert.equal(await station.inputValue(), "kr1");
  assert.equal(await spike.isChecked(), false);
  await toFiles.click();
  await queueItem(csvName).getByRole("button", { name: "Настроить" }).click();
  assert.equal(await spike.isChecked(), true);
  assert.equal(await add.isDisabled(), true);

  await external.check();
  await page.getByText("Метеосервисы дадут справочные значения. Пропуски останутся в данных.").waitFor();
  await model.check();
  assert.equal(await external.isChecked(), true);
  assert.equal(await model.isChecked(), true);
  await page.getByText("В основе остаётся оценка станции. Два согласующихся метеосервиса могут немного её скорректировать.").waitFor();
  await page.getByLabel("Часовой пояс исходного файла").waitFor();
  await external.uncheck();
  assert.equal(await model.isChecked(), true);
  await page.locator(".file-bounds summary").click();
  await page.getByLabel("Значение выбивается из ряда: Температура воздуха", { exact: true }).fill("4.5");
  await applyFilters(page);
  await page.waitForFunction(() => document.querySelector("[data-file-action=publish]")?.disabled === false);
  const updatedDraft = await fileRecord(page, base, csvName);
  assert.notEqual(updatedDraft.preview.revision, draft.preview.revision);
  assert.deepEqual(updatedDraft.preview.settings.filters, ["hampel", "spike"]);
  assert.equal(updatedDraft.preview.settings.thresholds.hampel.t_air, 4.5);
  assert.equal(updatedDraft.preview.settings.fill_external, false);
  assert.equal(updatedDraft.preview.settings.fill_model, true);
  assert.equal(updatedDraft.preview.stats.filled, 3);

  const replaced = timeline.getByRole("button", { name: /07:00.*Восстановлено.*Температура воздуха/ });
  const excluded = timeline.getByRole("button", { name: /07:00.*Исходные выбросы.*Температура воздуха/ });
  const filledGap = timeline.getByRole("button", { name: /12:00.*Восстановлено.*Относительная влажность/ });
  assert.equal(await marks.count(), 5);
  assert.ok(Number(await replaced.getAttribute("y")) > Number(await excluded.getAttribute("y")));
  await excluded.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("row").filter({ hasText: "2026-02-14 07:00" }).getByText("К удалению").waitFor();
  assert.match(await page.locator("#timeline-events tbody").innerText(), /(?:предел|диапазон)/i);
  await filledGap.click();
  const fillRow = page.getByRole("row").filter({ hasText: "2026-02-14 12:00" });
  await fillRow.getByText(/Модель станции.*интерполяция/i).waitFor();
  assert.match(await fillRow.innerText(), /Относительная влажность.*78/s);

  await temperature.click();
  assert.equal(await marks.count(), 2);
  await humidity.click();
  assert.equal(await temperature.getAttribute("aria-pressed"), "true");
  assert.equal(await humidity.getAttribute("aria-pressed"), "true");
  assert.equal(await marks.count(), 5);
  const fillLayer = page.getByRole("group", { name: "Слои шкалы" }).getByRole("button", { name: "Восстановлено" });
  await fillLayer.click();
  assert.equal(await fillLayer.getAttribute("aria-pressed"), "false");
  assert.equal(await marks.count(), 2);
  await fillLayer.click();
  assert.equal(await marks.count(), 5);

  await page.reload();
  await queueItem(csvName).getByText("Проверка результата", { exact: true }).waitFor();
  await queueItem(csvName).getByRole("button", { name: "Настроить" }).click();
  await page.locator(".preview-card-shell").waitFor();
  assert.equal(await station.inputValue(), "kr2");
  assert.equal(await spike.isChecked(), true);
  assert.equal(await add.isEnabled(), true);

  const excel = await download(page, page.getByRole("link", { name: "Скачать отфильтрованный файл" }));
  assert.match(excel.name, /-filtered\.xlsx$/);
  assert.equal(excel.bytes.subarray(0, 4).toString("hex"), "504b0304");
  const firstManual = await correctTemperature(page, "Исправленный.csv", "4.25");
  assert.equal(firstManual.manual.changed, 1);
  assert.equal(firstManual.manual.parent_revision, updatedDraft.preview.revision);
  assert.equal(firstManual.file.confirmed, null);
  await page.getByText("Исправленный.csv", { exact: true }).waitFor();
  const manualMark = timeline.getByRole("button", { name: /07:00.*Ручные правки.*Температура воздуха/ });
  await manualMark.click();
  const correctedRow = page.getByRole("row").filter({ hasText: "2026-02-14 07:00" });
  await correctedRow.getByText("Исправлено в загруженном CSV").waitFor();
  assert.match(await correctedRow.innerText(), /4,25/);
  const secondManual = await correctTemperature(page, "Повторная правка.csv", "4.75");
  assert.equal(secondManual.manual.parent_revision, firstManual.revision);
  assert.equal(secondManual.manual.changed, 1);
  assert.equal(secondManual.file.confirmed, null);
  await page.reload();
  await queueItem(csvName).getByText("Проверка результата", { exact: true }).waitFor();
  await queueItem(csvName).getByRole("button", { name: "Настроить" }).click();
  await page.getByText("Повторная правка.csv", { exact: true }).waitFor();
  await manualMark.click();
  assert.match(await correctedRow.innerText(), /4,75/);
  await page.locator(".preview-card-shell").evaluate((section) =>
    section.scrollIntoView({ block: "center", behavior: "instant" }));
  await page.screenshot({ path: path.join(images, "files-preview-1280.png") });
  assert.equal(await otherTab.evaluate(() => window.dataRevisions.length), 0);
  await publish(page, "Добавить в базу");
  assert.equal((await fileRecord(page, base, csvName)).confirmed.revision, secondManual.revision);
  await otherTab.waitForFunction(() => window.dataRevisions.length === 1);
  await row(csvName).getByText("В общих графиках").waitFor();
  assert.match(await row(csvName).innerText(), /Кр2 · поле/);
  await queueItem(xlsxName).getByText("Ожидает обработки").waitFor();
  const filenameBox = await queueItem(xlsxName).locator("strong").boundingBox();
  const editBox = await queueItem(xlsxName).getByRole("button", { name: "Настроить" }).boundingBox();
  assert.ok(editBox.y > filenameBox.y + filenameBox.height);
  await row(csvName).getByText("С ручными правками").waitFor();
  await page.screenshot({ path: path.join(images, "files-queue-1280.png"), fullPage: true });

  await queueItem(xlsxName).getByRole("button", { name: "Настроить" }).click();
  assert.equal(await pressure.inputValue(), "1050");
  await applyFilters(page);
  assert.equal((await fileRecord(page, base, xlsxName)).preview.stats.parameters.pressure_hpa.marked, 1);
  await pressure.selectOption("manual");
  const lowPressure = page.getByLabel("Минимум: Атмосферное давление", { exact: true });
  const highPressure = page.getByLabel("Максимум: Атмосферное давление", { exact: true });
  await highPressure.fill("1200");
  await lowPressure.fill("1300");
  assert.equal(await add.isDisabled(), true);
  await page.getByRole("button", { name: "Отфильтровать", exact: true }).click();
  await page.getByText(/Минимум не должен превышать максимум/).waitFor();
  await lowPressure.fill("940");
  await applyFilters(page);
  await page.getByText("0 выбросов", { exact: true }).waitFor();
  assert.deepEqual((await fileRecord(page, base, xlsxName)).preview.settings.ranges.pressure_hpa, [940, 1200]);
  await pressure.selectOption("1550");
  assert.equal(await add.isDisabled(), true);
  await applyFilters(page);
  await page.getByText("0 выбросов", { exact: true }).waitFor();
  assert.equal((await fileRecord(page, base, xlsxName)).preview.settings.pressure_max, "1550");
  const fullWorkbook = await prepareFullExcel(page, (await fileRecord(page, base, xlsxName)).id, "pressure_hpa");
  assert.equal(fullWorkbook.report.original_workbook, true);
  const workbookCopy = await download(page, fullWorkbook.dialog.getByRole("link", { name: "Скачать копию исходного XLSX с отметками" }));
  assert.equal(workbookCopy.bytes.subarray(0, 4).toString("hex"), "504b0304");
  await page.keyboard.press("Escape");
  await fullWorkbook.dialog.waitFor({ state: "hidden" });
  await publish(page, "Добавить в базу");
  await otherTab.waitForFunction(() => window.dataRevisions.length === 2);
  await row(xlsxName).getByText("В общих графиках").waitFor();

  await row(csvName).getByRole("button", { name: "Изменить фильтрацию" }).click();
  await station.selectOption("kr1");
  await applyFilters(page);
  await page.getByText("Текущая версия остаётся в графиках").waitFor();
  const replacement = await fileRecord(page, base, csvName);
  assert.equal(replacement.confirmed.settings.station, "kr2");
  assert.equal(replacement.preview.settings.station, "kr1");
  assert.equal(replacement.preview.stats.marked, 2);
  await publish(page, "Заменить в базе");
  await otherTab.waitForFunction(() => window.dataRevisions.length === 3);
  await row(csvName).getByText("Кр1 · поляна").waitFor();

  const remove = row(csvName).getByRole("button", { name: /^Удалить обработанные данные/ });
  await remove.click();
  const confirm = page.getByRole("dialog", { name: "Удалить обработанные данные?" });
  await confirm.getByText("Очищенные данные будут исключены из общих графиков. Исходный файл сохранится.").waitFor();
  await page.keyboard.press("Escape");
  await confirm.waitFor({ state: "hidden" });
  await row(csvName).getByText("В общих графиках").waitFor();
  assert.equal(await otherTab.evaluate(() => window.dataRevisions.length), 3);
  await remove.click();
  await confirm.getByRole("button", { name: "Удалить данные", exact: true }).click();
  await row(csvName).getByText("Только исходник").waitFor();
  await row(xlsxName).getByText("В общих графиках").waitFor();
  await otherTab.waitForFunction(() => window.dataRevisions.length === 4);
  const revisions = await otherTab.evaluate(() => window.dataRevisions);
  assert.equal(new Set(revisions).size, revisions.length);
  assert.ok(revisions.every((revision) => /^[a-f0-9-]{36}$/.test(revision)));
  await otherTab.close();

  await row(csvName).getByRole("button", { name: /^Открыть исходник/ }).click();
  const raw = page.getByRole("dialog", { name: csvName });
  await raw.getByText("Начальные строки исходного файла.").waitFor();
  const original = await download(page, raw.getByRole("link", { name: "Скачать исходный CSV" }));
  assert.equal(original.name, csvName);
  assert.deepEqual(original.bytes, csv);
  await page.keyboard.press("Escape");
  await raw.waitFor({ state: "hidden" });

  await page.goto(`${base}/files?file=${csvRecord.id}`);
  await page.getByRole("heading", { name: csvName, exact: true }).waitFor();
  const fullCsv = await prepareFullExcel(page, csvRecord.id, "t_air");
  const measurement = fullCsv.report.rows.find(({ time }) => time === "2026-02-14 07:00");
  assert.equal(measurement.value, -100);
  const auditedExcel = await download(page, fullCsv.dialog.getByRole("link", { name: "Скачать Excel с решениями" }));
  assert.match(auditedExcel.name, /^review_kr1_t_air_.*\.xlsx$/);
  assert.equal(auditedExcel.bytes.subarray(0, 4).toString("hex"), "504b0304");
  await page.keyboard.press("Escape");
  await fullCsv.dialog.waitFor({ state: "hidden" });
  await toFiles.click();
  await uploadMode.click();

  await page.setViewportSize({ width: 390, height: 844 });
  const dropzone = page.locator("#dropzone");
  const uploadStation = page.getByLabel("Станция для загрузки", { exact: true });
  const longitude = page.getByLabel("Точная долгота", { exact: true });
  const label = "QA · станция у реки";
  let registrations = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/stations" && request.method() === "POST") registrations++;
  });
  await uploadStation.selectOption("new");
  await page.getByLabel("Название новой станции").fill(label);
  await page.getByLabel("Точная широта").fill("0");
  await page.getByRole("button", { name: "Выбрать файлы" }).click();
  assert.equal(await longitude.evaluate((input) => input.validity.valid), false);
  assert.equal(registrations, 0);
  await longitude.fill("-71,123456789");
  const pressureParam = page.getByRole("checkbox", { name: "Атмосферное давление" });
  await pressureParam.check();
  assert.equal(await page.getByLabel("Профиль прибора").count(), 0);
  async function dropFiles(names) {
    const transfer = await page.evaluateHandle((filenames) => {
      const data = new DataTransfer();
      const readings = ["datetime,t_air", ...Array.from({ length: 72 }, (_, index) =>
        `2026-02-${16 + Math.floor(index / 24)} ${String(index % 24).padStart(2, "0")}:00,${index === 55 ? -100 : 4 + index % 3}`)].join("\n");
      for (const name of filenames) data.items.add(new File([readings], name, { type: "text/csv" }));
      return data;
    }, names);
    await dropzone.dispatchEvent("dragover", { dataTransfer: transfer });
    assert.match(await dropzone.getAttribute("class"), /\bdrag\b/);
    await dropzone.dispatchEvent("drop", { dataTransfer: transfer });
    await transfer.dispose();
    for (const name of names) await queueItem(name).getByText("Ожидает обработки").waitFor();
  }
  await dropFiles(["Перетащенный.csv", "Второй замер.csv"]);
  await uploadStation.selectOption("new");
  await dropFiles(["Повторная загрузка.csv"]);
  assert.equal(registrations, 1);
  const catalogResponse = await page.request.get(`${base}/api/stations`);
  const created = (await catalogResponse.json()).stations.filter((item) => item.label === label);
  assert.equal(created.length, 1);
  const [registered] = created;
  assert.equal(registered.latitude, 0);
  assert.equal(registered.longitude, -71.123456789);
  assert.equal(registered.location_precision, "exact");
  assert.deepEqual(registered.params.map(({ id }) => id).sort(), ["pressure_hpa", "t_air"]);
  assert.equal(registered.profile, undefined);
  for (const name of ["Перетащенный.csv", "Второй замер.csv", "Повторная загрузка.csv"]) {
    assert.equal((await fileRecord(page, base, name)).station, registered.id);
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
  await queueItem("Перетащенный.csv").getByRole("button", { name: "Настроить" }).click();
  assert.equal(await station.inputValue(), registered.id);
  await applyFilters(page);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
  const timelineDate = page.getByLabel("Дата на шкале");
  await timelineDate.fill("2026-02-18");
  await page.getByRole("button", { name: "Перейти", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "2026-02-18 07:00" }).getByText("К удалению").waitFor();
  assert.equal(await timeline.locator(".timeline-event[aria-pressed=true]").count(), 1);
  await timelineDate.fill("2026-02-16");
  await page.getByRole("button", { name: "Перейти", exact: true }).click();
  await page.getByText("В предпросмотре нет отмеченных сроков на выбранную дату.").waitFor();

  await external.check();
  await page.getByLabel("Часовой пояс исходного файла").selectOption("UTC");
  await page.getByLabel("Показатель для заполнения").selectOption("t_air");
  await page.getByLabel("Заполнение с").fill("2026-02-18");
  await page.getByLabel("Заполнение по").fill("2026-02-18");
  await applyFilters(page);
  const referenceMark = timeline.getByRole("button", { name: /18\.02\.2026.*Сверка с метеосервисами/ });
  await referenceMark.click();
  const referenceRow = page.getByRole("row").filter({ hasText: "2026-02-18 07:00" });
  await referenceRow.getByText("Пропуск", { exact: true }).waitFor();
  assert.match(await referenceRow.innerText(), /Open-Meteo.*NASA POWER/s);
  const referenceOnly = await fileRecord(page, base, "Перетащенный.csv");
  assert.equal(referenceOnly.preview.stats.filled, 0);

  await model.check();
  await applyFilters(page);
  const adjustedMark = timeline.getByRole("button", { name: /18\.02\.2026.*Восстановлено/ });
  await adjustedMark.click();
  await referenceRow.getByText(/Прошлые наблюдения не подтвердили пользу поправки/).waitFor();

  await page.getByLabel("Заполнение с").fill("2026-02-16");
  await applyFilters(page);
  await adjustedMark.click();
  await referenceRow.getByText(/Поправка.*по двум согласующимся источникам/).waitFor();
  const current = await fileRecord(page, base, "Перетащенный.csv");
  const reportResponse = await page.request.get(`${base}/api/files/preview?` + new URLSearchParams({
    id: current.id, revision: current.preview.revision,
  }));
  assert.equal(reportResponse.status(), 200);
  const weatherReport = await reportResponse.json();
  const { estimate } = weatherReport.points.t_air.find(({ time }) => time === "2026-02-18 07:00");
  assert.equal(estimate.method, "model_adjusted");
  assert.equal(estimate.provenance.verification.external_value, 5.1);
  assert.notEqual(estimate.value, 5.1);
  assert.ok(estimate.provenance.verification.delta > 0 && estimate.provenance.verification.delta <= 0.5);
  assert.deepEqual(weatherReport.external.t_air.requested_coordinates, { latitude: 0, longitude: -71.123456789 });
  await publish(page, "Добавить в базу");
  const updatedCatalog = await page.request.get(`${base}/api/stations`);
  assert.equal((await updatedCatalog.json()).stations.find(({ id }) => id === registered.id).start, "2026-02-16");
  await page.getByRole("button", { name: "Архив и сохранённые данные" }).click();
  await page.getByLabel("Станция", { exact: true }).selectOption(registered.id);
  await page.locator("#observation-selection").getByLabel("Показатель", { exact: true }).selectOption("t_air");
  await page.getByText(/Координаты станции · 0°.*71,123456789/).waitFor();
  const reused = await prepareSelection();
  assert.equal(reused.provenance.station, registered.id);
  assert.ok(reused.provenance.sources.confirmed);
  assert.equal(reused.confirmed, null);
  await verifyJsonImport(page, base);
  assert.deepEqual(pageErrors, []);
}

async function run() {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "meteo-files-qa-"));
  const database = path.join(temporary, "observations.sqlite3");
  const workbook = path.join(temporary, "observations.xlsx");
  // All writes use a fresh database and an operating-system-assigned local port.
  const server = spawn(process.env.PYTHON || "python3", ["-u", "-c", qaServer, workbook], {
    cwd: root,
    env: {
      ...process.env, METEO_DATABASE_URL: "", METEO_OBSERVATION_DB: database, PYTHONDONTWRITEBYTECODE: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const lines = readline.createInterface({ input: server.stdout });
  let serverErrors = "";
  server.stderr.on("data", (chunk) => { serverErrors += chunk.toString(); });
  let browser;
  try {
    const port = await Promise.race([
      once(lines, "line", { signal: AbortSignal.timeout(10000) }).then(([line]) => line),
      once(server, "exit").then(([code]) => { throw new Error(`Тестовый сервер завершился с кодом ${code}`); }),
    ]);
    assert.match(port, /^\d+$/);
    await fs.mkdir(images, { recursive: true });
    browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
    const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, locale: "ru-RU", reducedMotion: "reduce" });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    await runChecks(page, `http://127.0.0.1:${port}`, await fs.readFile(workbook));
    console.log("PASS /files: архив и сохранённые данные, CSV/XLSX/JSON, модель и сверка, Excel, регистрация и публикация");
  } catch (error) {
    if (serverErrors) console.error(serverErrors.trim());
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null) {
      const stopped = once(server, "close");
      server.kill();
      await stopped;
    }
    lines.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
