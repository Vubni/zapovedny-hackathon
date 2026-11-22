const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

const calendar = "#align-observation-calendar";
const chart = (page, id = "air") => page.locator(`[data-observation-chart="${id}"]`);
const plot = (page, id = "air") => chart(page, id).locator(".obs-chart__svg");
const lines = (page, id = "air") => plot(page, id).locator("path[stroke-linecap=round][fill=none]");
const dates = (page) => chart(page).locator(".obs-chart__tooltip-source-date");
const sourcePeriods = "2024-02-27:2024-03-02,2025-02-27:2025-03-02";
const sourceCharts = "air,temp_diurnal,wind_rose";

function observationsUrl(base, intervals, grain, charts = "air", stations = "kr1") {
  return `${base}/?${new URLSearchParams({ intervals, grain, charts, stations, calendar: 1 })}`;
}

function pathX(data) {
  return Array.from(data.matchAll(/[ML]([\d.]+) /g), match => Number(match[1]));
}

async function downloadText(page, action) {
  const [download] = await Promise.all([page.waitForEvent("download"), action()]);
  assert.equal(await download.failure(), null);
  return fs.readFile(await download.path(), "utf8");
}

async function exportSvg(page, id) {
  const panel = chart(page, id);
  await panel.locator('[data-chart-action="download"]').click();
  return downloadText(page, () => panel.locator('[data-chart-format="svg"]').click());
}

async function exportCSV(page) {
  return downloadText(page, () => chart(page).locator('[data-chart-action="csv"]').click());
}

function csvRows(csv) {
  const [header, ...records] = csv.trim().split(/\r?\n/);
  const names = header.replace(/^\uFEFF/, "").split(";");
  return records.map(line => Object.fromEntries(line.split(";").map((value, index) => [names[index], value])));
}

function measurements(rows) {
  return rows.map(row => [row["Дата или интервал"], row["Конец интервала"], row["Станция"], row["Значение"]]).sort();
}

function svgTexts(svg) {
  const read = 'import json,sys; from xml.etree import ElementTree as ET; print(json.dumps([node.text for node in ET.fromstring(sys.stdin.read()).iter("{http://www.w3.org/2000/svg}text")], ensure_ascii=False))';
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", read], { encoding: "utf8", input: svg }));
}

async function checkLongLegend(browser, page, base) {
  const url = `${base}/?stations=kr1,kr2,ame60&start=2020-01-01&end=2025-12-31&grain=month&charts=wind_rose&mode=modern`;
  const open = async view => {
    await view.goto(url);
    await plot(view, "wind_rose").waitFor();
    const panel = chart(view, "wind_rose");
    const details = panel.locator(".obs-chart__legend-disclosure");
    const trigger = details.locator("summary");
    const rows = details.locator(".legend-key");
    assert.equal(await rows.count(), 18);
    assert.equal(await details.evaluate(node => node.open), false);
    assert.equal(await rows.first().isVisible(), false);
    assert.match(await trigger.textContent(), /18 из 18/);
    return { panel, details, trigger, rows };
  };
  const { panel, details, trigger, rows } = await open(page);
  const expected = await rows.allTextContents();
  const allLabels = svgTexts(await exportSvg(page, "wind_rose"));
  assert(expected.every(label => allLabels.includes(label)));
  if (process.env.METEO_CALENDAR_PDF_DIR) {
    await panel.locator("h2, h3").first().hover();
    await panel.screenshot({ path: path.join(process.env.METEO_CALENDAR_PDF_DIR, "rose-collapsed.png") });
  }
  await trigger.press("Enter");
  assert.equal(await details.evaluate(node => node.open), true);
  assert(await rows.first().isVisible());
  if (process.env.METEO_CALENDAR_PDF_DIR) {
    await panel.screenshot({ path: path.join(process.env.METEO_CALENDAR_PDF_DIR, "rose-expanded.png") });
  }
  await rows.first().press("Space");
  assert.equal(await rows.first().getAttribute("aria-pressed"), "false");
  assert.match(await trigger.textContent(), /17 из 18/);
  await trigger.press("Enter");
  const filteredLabels = svgTexts(await exportSvg(page, "wind_rose"));
  assert(!filteredLabels.includes(expected[0]));
  assert(expected.slice(1).every(label => filteredLabels.includes(label)));
  await trigger.press("Enter");
  await rows.first().press("Space");
  assert.equal(await rows.first().getAttribute("aria-pressed"), "true");

  const touch = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "ru-RU" });
  try {
    const mobile = await open(touch);
    await mobile.trigger.tap();
    assert(await mobile.rows.first().isVisible());
    await mobile.rows.first().tap();
    assert.match(await mobile.trigger.textContent(), /17 из 18/);
    await mobile.rows.first().tap();
    assert.match(await mobile.trigger.textContent(), /18 из 18/);
    await mobile.trigger.tap();
    assert.equal(await mobile.rows.first().isVisible(), false);
    assert.equal(await touch.evaluate(() => document.documentElement.scrollWidth), 390);
  } finally {
    await touch.close();
  }
  return { rows: expected.length, closedInitially: true, keyboardAndTouch: true, exportedWhenClosed: expected.length };
}

async function checkZip(page, individual) {
  const button = page.locator("#download-all-charts");
  await page.waitForFunction(() => !document.querySelector("#download-all-charts")?.disabled);
  await button.click();
  const [download] = await Promise.all([page.waitForEvent("download"), page.locator('[data-archive-format="svg"]').click()]);
  assert.equal(await download.failure(), null);
  const inspector = String.raw`
from pathlib import Path
from zipfile import ZipFile
import json, os, subprocess, sys
from xml.etree import ElementTree as ET

namespace = "{http://www.w3.org/2000/svg}"
with ZipFile(sys.argv[1]) as archive:
    assert archive.testzip() is None
    figures = {}
    for name in archive.namelist():
        data = archive.read(name)
        if name.endswith(".pdf"):
            assert data.startswith(b"%PDF-1.4")
            directory = os.environ.get("METEO_CALENDAR_PDF_DIR")
            if directory:
                output = Path(directory)
                output.mkdir(parents=True, exist_ok=True)
                pdf = output / "calendar-alignment.pdf"
                pdf.write_bytes(data)
                subprocess.run(["pdftoppm", "-r", "110", "-png", str(pdf), str(output / "calendar-alignment")], check=True)
            continue
        root = ET.fromstring(data)
        title = root.findtext(namespace + "title")
        figures[title] = {"axis": root.get("data-axis"), "data": data.decode("utf-8")}
    assert len(archive.namelist()) == len(figures) + 1
print(json.dumps(figures, ensure_ascii=False))
`;
  const data = JSON.parse(execFileSync(process.env.PYTHON || "python3",
    ["-c", inspector, await download.path()], { encoding: "utf8" }));
  assert.equal(data["Средняя температура воздуха"].axis, "calendar");
  assert.equal(data["Средняя температура воздуха"].data, individual);
  assert.equal(data["Средний суточный ход температуры"].axis, "hour");
  assert.equal(data["Роза ветров"].axis, "direction");
  return Object.fromEntries(Object.entries(data).map(([title, item]) => [title, item.axis]));
}

async function run(base) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1420, height: 940 }, locale: "ru-RU" });
  const pageErrors = [];
  const requests = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/observations") requests.push(request.url());
  });
  try {
    await page.goto(observationsUrl(base, sourcePeriods, "day", sourceCharts));
    await plot(page).locator("text").filter({ hasText: /^День и месяц$/ }).waitFor();
    assert.equal(await plot(page).getAttribute("data-axis"), "calendar");
    await plot(page, "temp_diurnal").waitFor();
    await plot(page, "wind_rose").waitFor();
    assert.equal(await plot(page, "temp_diurnal").getAttribute("data-axis"), "hour");
    assert.equal(await plot(page, "wind_rose").getAttribute("data-axis"), "direction");
    const [leap, ordinary] = await lines(page).evaluateAll(nodes => nodes.map(node => node.getAttribute("d")));
    const leapX = pathX(leap);
    assert.equal(leapX.length, 5);
    assert.deepEqual(pathX(ordinary), [leapX[0], leapX[1], leapX[3], leapX[4]]);
    assert.equal(ordinary.match(/M/g).length, 2, "29 февраля остаётся разрывом ряда невисокосного года");

    const hit = chart(page).locator(".obs-chart__target");
    await hit.focus();
    assert.deepEqual(await dates(page).allTextContents(), ["27.02.2024", "27.02.2025"]);
    await hit.press("ArrowRight");
    await hit.press("ArrowRight");
    assert.deepEqual(await dates(page).allTextContents(), ["29.02.2024"]);
    assert.equal(await chart(page).locator(".obs-chart__tooltip > strong").textContent(), "29 февраля");

    const alignedCsv = csvRows(await exportCSV(page));
    assert.equal(alignedCsv.length, 9);
    assert.equal(alignedCsv.filter(row => row["День и месяц на общей оси"] === "29.02").length, 1);
    const alignedSvg = await exportSvg(page);
    assert.match(alignedSvg, /День и месяц/);
    assert.match(alignedSvg, /27\.02\.2024 – 02\.03\.2024/);
    assert.doesNotMatch(alignedSvg, />[^<]*\b2000\b[^<]*</);
    const exportedAxes = await checkZip(page, alignedSvg);
    const callsBefore = requests.length;
    await page.locator(calendar).uncheck();
    await plot(page).locator("text").filter({ hasText: /^День и месяц$/ }).waitFor({ state: "hidden" });
    assert.equal(await plot(page).getAttribute("data-axis"), "time");
    assert.equal(requests.length, callsBefore);
    assert.deepEqual(measurements(csvRows(await exportCSV(page))), measurements(alignedCsv));

    await page.goto(observationsUrl(base, "2023-12-29:2024-01-04,2024-12-29:2025-01-04", "day"));
    await plot(page).waitFor();
    assert.equal(await plot(page).getAttribute("data-axis"), "calendar");
    const crossYear = await lines(page).evaluateAll(nodes => nodes.map(node => node.getAttribute("d")));
    assert.equal(crossYear.length, 3, "Декабрь и январь отрисовываются отдельно; пустой декабрь исключён из линий");
    for (const line of crossYear) {
      const x = pathX(line);
      assert.ok(Math.max(...x) - Math.min(...x) < 15, "Граница года не пересекается линией через весь график");
    }
    const crossYearLabels = await chart(page).locator(".obs-chart__legend button").allTextContents();
    assert.equal(crossYearLabels.length, 4);
    assert(crossYearLabels.some(label => label.includes("· 2023 ·")));
    assert(crossYearLabels.some(label => label.includes("· 2025 ·")));

    await page.goto(observationsUrl(base, "2023-11-01:2025-01-31,2025-07-01:2025-08-31", "month"));
    await plot(page).waitFor();
    const multiyearLabels = await chart(page).locator(".obs-chart__legend button").allTextContents();
    for (const year of [2023, 2024, 2025]) assert(multiyearLabels.some(label => label.includes(`· ${year} ·`)));
    assert.equal(multiyearLabels.length, 4);
    await page.getByLabel("Группировка").selectOption("year");
    await chart(page).locator('svg[data-axis="time"]').waitFor();
    assert(await page.locator(calendar).isDisabled());

    await page.goto(observationsUrl(base, "2008-01-01:2012-12-31,2013-01-01:2017-12-31", "month", "air_trend", "ame60"));
    await chart(page, "air_trend").getByText(/Тренд по исходной хронологии/).waitFor();
    await chart(page, "air_trend").locator('svg[data-axis="time"]').waitFor();
    assert.equal(await chart(page, "air_trend").locator(".obs-chart__legend button").count(), 2);

    const roseLegend = await checkLongLegend(browser, page, base);

    assert.deepEqual(pageErrors, []);
    const evidence = { exportedAxes, originalCsvRows: alignedCsv.length, leapDayRows: 1, crossYearLabels, multiyearLabels,
      dataRequestsOnToggle: 0, trendKeepsTimeline: true, roseLegend };
    if (process.env.METEO_CALENDAR_REPORT) {
      await fs.writeFile(process.env.METEO_CALENDAR_REPORT, JSON.stringify(evidence, null, 2) + "\n");
    }
    console.log("PASS: календарные координаты, 29.02, декабрь–январь, многолетние периоды, хронология тренда, CSV, SVG и ZIP/PDF");
    console.log("PASS: свёрнутая легенда из 18 рядов трёх станций, клавиатура, касание и экспорт");
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(run).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
