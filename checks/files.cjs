const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
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
from pathlib import Path
from checks.test_observation_workflow import SERVER, workbook_bytes

rows = [["datetime", "t_air", "pressure_hpa"]]
rows += [[f"2026-02-15 {hour:02}:00", 5, 1100 if hour == 8 else 1010] for hour in range(24)]
Path(sys.argv[1]).write_bytes(workbook_bytes(rows))
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
  await page.getByRole("button", { name: "Отфильтровать", exact: true }).click();
  await page.locator(".preview-card-shell").waitFor();
}

async function publish(page, label) {
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("heading", { name: "Данные добавлены в общие графики" }).waitFor();
  await page.getByRole("button", { name: "К очереди", exact: true }).click();
}

async function runChecks(page, base, workbook, operatorPassword) {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const queueItem = (name) => page.locator(".queue-item").filter({ hasText: name });
  const row = (name) => page.getByRole("row").filter({ hasText: name });
  const station = page.getByLabel("Метеостанция", { exact: true });
  const hampel = page.getByRole("checkbox", { name: /^Локальная медиана/ });
  const spike = page.getByRole("checkbox", { name: /^Одиночные скачки/ });
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

  await page.goto(`${base}/files`);
  assert.equal(new URL(page.url()).pathname, "/operator");
  assert.equal((await page.request.get(`${base}/api/files`)).status(), 401);
  const password = page.getByLabel("Пароль оператора", { exact: true });
  await password.fill("неверный пароль");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await page.getByRole("alert").getByText("Неверный пароль оператора.").waitFor();
  await password.fill(operatorPassword);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await page.waitForURL(`${base}/files`);
  await page.getByRole("button", { name: "Выбрать файлы", exact: true }).waitFor();
  assert.equal(await page.getByRole("link", { name: "Расширенный аудит" }).getAttribute("href"), "/review");
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
  assert.equal(await page.getByRole("link", { name: "Аудит исходника" }).getAttribute("href"),
    `/review?file=${csvRecord.id}`);
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
  await page.locator("summary").click();
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

  await page.getByLabel("Способ заполнения", { exact: true }).selectOption("model");
  await applyFilters(page);
  await page.waitForFunction(() => document.querySelector("[data-file-action=publish]")?.disabled === false);
  const updatedDraft = await fileRecord(page, base, csvName);
  assert.notEqual(updatedDraft.preview.revision, draft.preview.revision);
  assert.deepEqual(updatedDraft.preview.settings.filters, ["hampel", "spike"]);
  assert.equal(updatedDraft.preview.stats.filled, 3);

  const replaced = timeline.getByRole("button", { name: /07:00.*Замены и заполнения.*Температура воздуха/ });
  const excluded = timeline.getByRole("button", { name: /07:00.*Исходные выбросы.*Температура воздуха/ });
  const filledGap = timeline.getByRole("button", { name: /12:00.*Замены и заполнения.*Относительная влажность/ });
  assert.equal(await marks.count(), 5);
  assert.ok(Number(await replaced.getAttribute("y")) > Number(await excluded.getAttribute("y")));
  await excluded.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("row").filter({ hasText: "2026-02-14 07:00" }).getByText("К удалению").waitFor();
  assert.match(await page.locator("#timeline-events tbody").innerText(), /(?:предел|диапазон)/i);
  await filledGap.click();
  const fillRow = page.getByRole("row").filter({ hasText: "2026-02-14 12:00" });
  await fillRow.getByText(/Локальная модель.*Регрессия/).waitFor();
  assert.match(await fillRow.innerText(), /Относительная влажность.*78/s);

  await temperature.click();
  assert.equal(await marks.count(), 2);
  await humidity.click();
  assert.equal(await temperature.getAttribute("aria-pressed"), "true");
  assert.equal(await humidity.getAttribute("aria-pressed"), "true");
  assert.equal(await marks.count(), 5);
  const fillLayer = page.getByRole("group", { name: "Слои шкалы" }).getByRole("button", { name: "Замены и заполнения" });
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
  await pressure.selectOption("1550");
  assert.equal(await add.isDisabled(), true);
  await applyFilters(page);
  await page.getByText("0 выбросов", { exact: true }).waitFor();
  assert.equal((await fileRecord(page, base, xlsxName)).preview.settings.pressure_max, "1550");
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

  await row(csvName).getByRole("button", { name: "Обработать снова" }).click();
  const auditLink = await page.getByRole("link", { name: "Аудит исходника" }).getAttribute("href");
  const audit = await page.context().newPage();
  audit.on("pageerror", (error) => pageErrors.push(error.message));
  audit.setDefaultTimeout(10000);
  await audit.goto(new URL(auditLink, base).href);
  await audit.locator("#review-param").selectOption("t_air");
  const [auditResponse] = await Promise.all([
    audit.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "POST" && url.pathname === "/api/review"
        && url.searchParams.get("file_id") === csvRecord.id;
    }),
    audit.getByRole("button", { name: "Отфильтровать", exact: true }).click(),
  ]);
  assert.equal(auditResponse.status(), 200);
  await audit.locator("#review-results").waitFor();
  assert.ok((await audit.locator("#review-context").textContent()).includes(csvName));
  const measurement = audit.getByRole("row").filter({ hasText: "14.02.2026 07:00" });
  assert.equal(await measurement.locator(".review-original").textContent(), "-100");
  const auditedExcel = await download(audit, audit.getByRole("link", { name: "Скачать Excel", exact: true }));
  assert.match(auditedExcel.name, /^review_kr1_t_air_.*\.xlsx$/);
  assert.equal(auditedExcel.bytes.subarray(0, 4).toString("hex"), "504b0304");
  await audit.close();
  await toFiles.click();

  await page.setViewportSize({ width: 390, height: 844 });
  const dropzone = page.locator("#dropzone");
  const transfer = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(new File(["datetime,t_air\n2026-02-16 00:00,0\n"], "Перетащенный.csv", { type: "text/csv" }));
    return data;
  });
  await dropzone.dispatchEvent("dragover", { dataTransfer: transfer });
  assert.match(await dropzone.getAttribute("class"), /\bdrag\b/);
  await dropzone.dispatchEvent("drop", { dataTransfer: transfer });
  await transfer.dispose();
  await queueItem("Перетащенный.csv").getByText("Ожидает обработки").waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
  await queueItem("Перетащенный.csv").getByRole("button", { name: "Настроить" }).click();
  await applyFilters(page);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
  assert.deepEqual(pageErrors, []);
}

async function run() {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "meteo-files-qa-"));
  const database = path.join(temporary, "observations.sqlite3");
  const workbook = path.join(temporary, "observations.xlsx");
  const operatorPassword = randomBytes(32).toString("base64url");
  // All writes use a fresh database and an operating-system-assigned local port.
  const server = spawn(process.env.PYTHON || "python3", ["-u", "-c", qaServer, workbook], {
    cwd: root,
    env: {
      ...process.env, METEO_OBSERVATION_DB: database, METEO_ADMIN_PASSWORD: operatorPassword,
      METEO_ADMIN_ORIGIN: "", PYTHONDONTWRITEBYTECODE: "1",
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
    await runChecks(page, `http://127.0.0.1:${port}`, await fs.readFile(workbook), operatorPassword);
    console.log("PASS /files: очередь CSV/XLSX, шкала, ручные версии CSV, перезагрузка, выгрузка, публикация, замена, удаление, аудит и drop");
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
