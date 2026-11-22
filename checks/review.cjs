// Browser acceptance checks. Run with the same Chromium setup as checks/browser.cjs.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const base = "http://127.0.0.1:" + (process.env.METEO_TEST_PORT || "8765");
const reportPath = path.resolve(__dirname, "../results/browser/review.json");
const desktop = { width: 1440, height: 900 };
const mobile = { width: 390, height: 844 };

function workbook() {
  return execFileSync("python3", ["-c", `
from io import BytesIO
from sys import stdout
from zipfile import ZipFile, ZIP_DEFLATED

ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
doc = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
rel = "http://schemas.openxmlformats.org/package/2006/relationships"
types = "http://schemas.openxmlformats.org/package/2006/content-types"
book = "application/vnd.openxmlformats-officedocument.spreadsheetml"
out = BytesIO()
with ZipFile(out, "w", ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", f'<Types xmlns="{types}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="{book}.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="{book}.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="{book}.worksheet+xml"/></Types>')
    z.writestr("_rels/.rels", f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Target="xl/workbook.xml" Type="{doc}/officeDocument"/></Relationships>')
    z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{doc}"><sheets><sheet name="Описание" sheetId="1" r:id="rId1"/><sheet name="Измерения" sheetId="2" r:id="rId2"/></sheets></workbook>')
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="{doc}/worksheet"/><Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="{doc}/worksheet"/></Relationships>')
    z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Описание</t></is></c></row></sheetData></worksheet>')
    z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>datetime</t></is></c><c r="B1" t="inlineStr"><is><t>t_air</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>2032-01-04 12:00</t></is></c><c r="B2"><v>3</v></c></row></sheetData></worksheet>')
stdout.buffer.write(out.getvalue())
`]);
}

async function calculate(page, button = page.getByRole("button", { name: "Отфильтровать", exact: true })) {
  const request = page.waitForResponse((response) =>
    response.request().method() === "POST" && new URL(response.url()).pathname === "/api/review");
  await button.click();
  const response = await request;
  if (!response.ok()) {
    throw new Error("Отчёт не построен: " + response.status() + " " + await response.text());
  }
  await page.waitForFunction(() => document.activeElement.id === "review-title");
  return new URL(response.url()).searchParams;
}

async function downloadXlsx(page, name) {
  const pending = page.waitForEvent("download");
  await page.getByRole("link", { name, exact: true }).click();
  const artifact = await pending;
  const buffer = await fs.readFile(await artifact.path());
  assert.match(artifact.suggestedFilename(), /\.xlsx$/);
  assert.equal(buffer.subarray(0, 2).toString(), "PK");
  return { name: artifact.suggestedFilename(), bytes: buffer.length };
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: desktop, locale: "ru-RU", acceptDownloads: true });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const row = page.locator("#review-rows tr");
    const context = page.locator("#review-context");
    const conflicts = page.getByRole("checkbox", { name: /^Конфликты/ });
    const conflictPolicy = page.getByLabel("При конфликте");
    const buddy = page.getByRole("checkbox", { name: /^Поляна и поле/ });
    const original = page.getByRole("link", { name: "Скачать исходную книгу с отметками" });
    const notes = page.locator("#review-estimate-options summary");
    const chooseFile = (file) => page.locator("#review-file").setInputFiles(file);

    await page.goto(base + "/review");
    await page.waitForFunction(() => !document.getElementById("review-fields").disabled);
    assert.equal(await page.locator("#review-param").inputValue(), "pressure_hpa");
    assert.equal(await page.locator("#review-start").inputValue(), "2024-04-01");
    assert.equal(await conflicts.isChecked(), false);
    assert.equal(await conflictPolicy.isDisabled(), true);
    assert.equal(await buddy.isDisabled(), true);
    assert.equal((await calculate(page)).get("filters"), "");
    const metrics = await page.locator("#review-metrics .metric-value").allTextContents();
    assert.deepEqual(metrics, ["240", "38", "202", "38"]);
    assert.equal(await row.count(), 100);
    assert.equal(await original.isVisible(), false);
    await page.getByRole("button", { name: "Далее" }).click();
    await page.waitForFunction(() => document.querySelector("#review-page").textContent.startsWith("101–200"));
    await page.getByRole("button", { name: "Назад" }).click();
    await page.waitForFunction(() => document.querySelector("#review-page").textContent.startsWith("1–100"));
    await page.getByRole("button", { name: "Месяцы" }).click();
    assert.match(await page.locator("#review-summaries").innerText(), /Часть месяца/);
    const workbookDownload = await downloadXlsx(page, "Скачать Excel");

    await conflicts.check();
    assert.equal(await conflictPolicy.isEnabled(), true);
    await conflictPolicy.selectOption("best");
    const conflictOptions = await calculate(page);
    assert.equal(conflictOptions.get("filters"), "conflicts");
    assert.equal(conflictOptions.get("conflict_policy"), "best");
    await conflicts.uncheck();
    assert.equal(await conflictPolicy.isDisabled(), true);

    await notes.click();
    const exampleOptions = await calculate(page, page.getByRole("button", { name: "Открыть пример с оценкой" }));
    assert.equal(exampleOptions.get("timezone"), "Europe/Moscow");
    assert.equal(exampleOptions.get("subhour"), "skip");
    const hourly = row.filter({ hasText: "21.04.2024 11:00" }).locator(".review-estimate-cell");
    assert.match(await hourly.innerText(), /^9\s+ERA5 \+ MERRA-2 · среднее за час/);
    await hourly.locator("summary").click();
    assert.match(await hourly.innerText(), /ERA5 · 9,8 °C/);
    assert.match(await hourly.innerText(), /MERRA-2 · 8,2 °C/);
    assert.equal(await buddy.isEnabled(), true);

    for (const field of ["#review-start", "#review-end"]) {
      await page.locator(field).fill("2024-04-15");
    }
    await page.getByRole("checkbox", { name: /^Фильтр Хэмпеля/ }).check();
    await page.getByRole("checkbox", { name: /^Оценивать промежуточные минуты/ }).check();
    const minuteOptions = await calculate(page);
    assert.equal(minuteOptions.get("subhour"), "interpolate");
    const minute = row.filter({ hasText: "15.04.2024 15:32" });
    assert.match(await minute.innerText(), /Исключено/);
    assert.match(await minute.locator("td").nth(2).innerText(), /^5,97\s+Оценка/);
    assert.match(await minute.locator(".review-estimate-cell").innerText(), /^5,97\s+ERA5 \+ MERRA-2 · интерполяция к минуте/);

    await page.getByText("Свой файл", { exact: true }).click();
    assert.equal(await buddy.isDisabled(), true);
    assert.equal(await page.locator("#review-start").inputValue(), "");
    assert.equal(await page.getByLabel("Часовой пояс измерений").inputValue(), "");
    await page.getByRole("checkbox", { name: /^Подобрать внешние оценки/ }).uncheck();
    await chooseFile({
      name: "observations-2031.csv", mimeType: "text/csv",
      buffer: Buffer.from("datetime,t_air,qc_flag\n2031-02-10 12:00,5,\n2031-02-10 13:00,6,\n"),
    });
    await calculate(page);
    assert.match(await context.innerText(), /observations-2031.csv.*10\.02\.2031/);
    assert.equal(await row.count(), 2);
    assert.equal(await original.isVisible(), false);

    const plainText = "<img src=x onerror=alert(1)>";
    await chooseFile({
      name: "observations.json", mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify([{ datetime: "2031-02-10 12:00", t_air: plainText }])),
    });
    await calculate(page);
    assert.match(await row.first().innerText(), /Исключено/);
    assert.ok((await row.first().innerText()).includes(plainText));
    assert.equal(await row.locator("img").count(), 0);

    await chooseFile({
      name: "observations-2032.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: workbook(),
    });
    await calculate(page);
    assert.match(await context.innerText(), /observations-2032.xlsx.*04\.01\.2032/);
    assert.match(await row.first().innerText(), /Принято/);
    assert.equal(await original.isVisible(), true);
    const originalDownload = await downloadXlsx(page, "Скачать исходную книгу с отметками");

    await page.setViewportSize(mobile);
    await page.goto(base + "/review");
    await calculate(page);
    const overflow = await page.locator(".review-table-wrap").first().evaluate((region) => ({
      viewport: innerWidth, document: document.documentElement.scrollWidth,
      table: region.scrollWidth, region: region.clientWidth,
    }));
    assert.equal(overflow.document, overflow.viewport);
    assert.ok(overflow.table > overflow.region);
    assert.deepEqual(errors, []);

    const report = {
      browser: browser.version(),
      viewports: [desktop, mobile],
      archive: { raw: 240, working: 38, official: 38, pageNavigation: true, partialMonth: true },
      filters: { baseOnlyByDefault: true, conflictPolicy: "best" },
      estimates: { hourly: 9, interpolated: 5.97, timezone: exampleOptions.get("timezone") },
      upload: { csv: true, json: true, xlsxWithDataOnSecondSheet: true, plainText: true },
      downloads: { review: workbookDownload, annotatedSource: originalDownload },
      mobile: overflow,
      pageErrors: errors,
    };
    await fs.writeFile(reportPath, JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
