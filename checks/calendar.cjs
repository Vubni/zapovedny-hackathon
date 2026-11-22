// Optional browser acceptance check. The calendar itself needs only the local API.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {chromium} = require("playwright");

const base = `http://127.0.0.1:${process.env.METEO_TEST_PORT || "8765"}`;
const report = path.resolve(__dirname, "../results/browser/calendar.json");

async function waitForYear(page, heading) {
  await page.waitForFunction(expected =>
    document.querySelector("#temperature-title").textContent === expected &&
    !document.querySelector("#temperature-data").hidden &&
    document.querySelector("#temperature-data").getAttribute("aria-busy") === "false", heading);
}

function day(page, isoDate) {
  return page.locator(`button[data-day="${isoDate}"]`);
}

async function api(page, pathname) {
  const response = await page.request.get(pathname);
  assert.equal(response.status(), 200);
  return response.json();
}

function band(mean) {
  return [-25, -15, -5, 5, 15, 25, Infinity].findIndex(upper => mean < upper);
}

async function assertYear(page, station, year) {
  const params = new URLSearchParams({
    station, param: "t_air", grain: "day", start: `${year}-01-01`, end: `${year}-12-31`,
  });
  const series = await api(page, `${base}/api/series?${params}`);
  const cells = await page.locator("[data-day]").evaluateAll(buttons =>
    buttons.map(button => ({date: button.dataset.day, band: button.dataset.band})));
  const available = series.rows.filter(row => row.valid && Number.isFinite(row.mean));
  assert.equal(cells.length, series.rows.length);
  for (const [index, row] of series.rows.entries()) {
    const expected = row.valid ? String(band(row.mean)) :
      (row.n + row.flagged + row.missing + row.conflicts ? "invalid" : "missing");
    assert.deepEqual(cells[index], {date: row.start, band: expected});
  }
  const coverage = await page.locator("#temperature-coverage").textContent();
  assert.equal(coverage, `${series.stats.valid_days} из ${series.rows.length}`);
  const ordered = [...available].sort((a, b) => a.mean - b.mean);
  const highMean = ordered.at(-1)?.mean;
  const min = ordered[0];
  const max = available.find(row => row.mean === highMean);
  assert.equal(await page.locator("#temperature-minimum").evaluate(element => element.querySelector("button")?.dataset.jump ?? null), min?.start ?? null);
  assert.equal(await page.locator("#temperature-maximum").evaluate(element => element.querySelector("button")?.dataset.jump ?? null), max?.start ?? null);
  return {
    station, year, coverage,
    invalid: cells.filter(cell => cell.band === "invalid").length,
    missing: cells.filter(cell => cell.band === "missing").length,
    minimum: min ? {day: min.start, mean_c: min.mean} : null,
    maximum: max ? {day: max.start, mean_c: max.mean} : null,
  };
}

async function rawReading(page, station, date, expected) {
  const href = await page.locator("#temperature-source").getAttribute("href");
  const raw = await api(page, base + href);
  assert.equal(raw.station, station);
  assert.equal(raw.day, date);
  assert.equal(raw.valid, expected.valid);
  assert.equal(raw.rows.length, expected.sourceRows);
  if (expected.mean !== undefined) assert.equal(raw.mean, expected.mean);
  return {day: raw.day, mean_c: raw.mean, valid: raw.valid, source_rows: raw.rows.length};
}

async function check() {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}, hasTouch: true});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base);
    await page.locator('a[href="/calendar"]').first().click();
    await waitForYear(page, "Кр1 · поляна · 2025");
    const evidence = {kr1: await assertYear(page, "kr1", 2025)};

    const scale = page.locator("#temperature-scale");
    const colorsBefore = await scale.locator("[data-band]").evaluateAll(elements =>
      elements.map(element => getComputedStyle(element).backgroundColor));
    assert.equal(new Set(colorsBefore).size, 7);
    const minimum = page.locator("#temperature-minimum button");
    await minimum.press("Enter");
    assert.equal(await page.locator(":focus").getAttribute("data-day"), "2025-02-16");
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator(":focus").getAttribute("data-day"), "2025-02-17");
    assert.match(await page.locator("#temperature-reading").textContent(), /17 февраля 2025 г\./);
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.locator(":focus").getAttribute("data-day"), "2025-02-24");
    evidence.keyboard = {minimum: "2025-02-16", right: "2025-02-17", down: "2025-02-24"};

    await day(page, "2025-07-10").tap();
    assert.match(await page.locator("#temperature-reading").textContent(), /24,31 °C/);
    evidence.warmDay = await rawReading(page, "kr1", "2025-07-10", {
      valid: true, sourceRows: 24, mean: evidence.kr1.maximum.mean_c,
    });

    const dimensions = await page.evaluate(() => ({
      viewport: innerWidth,
      document: document.documentElement.scrollWidth,
      monthColumns: getComputedStyle(document.querySelector("#temperature-months")).gridTemplateColumns.split(" ").length,
    }));
    assert.equal(dimensions.viewport, 390);
    assert.ok(dimensions.document <= dimensions.viewport);
    assert.equal(dimensions.monthColumns, 2);
    evidence.mobile = dimensions;

    await page.locator("#temperature-station").selectOption("kr2");
    await waitForYear(page, "Кр2 · поле · 2025");
    evidence.kr2 = await assertYear(page, "kr2", 2025);
    const invalid = day(page, "2025-05-23");
    await invalid.click();
    assert.match(await page.locator("#temperature-reading").textContent(), /Не прошли контроль качества/);
    assert.match(await page.locator("#temperature-reason").textContent(), /15 часов/);
    const rawInvalid = await api(page, base + await page.locator("#temperature-source").getAttribute("href"));
    assert.equal(rawInvalid.valid, false);
    assert.ok(rawInvalid.rows.length > 0);
    evidence.invalidDay = {day: rawInvalid.day, valid: rawInvalid.valid, source_rows: rawInvalid.rows.length};

    const slider = page.locator("#temperature-year");
    await slider.click({position: {x: 4, y: 12}});
    await waitForYear(page, "Кр2 · поле · 2022");
    await slider.press("End");
    await waitForYear(page, "Кр2 · поле · 2025");
    await slider.press("ArrowLeft");
    await waitForYear(page, "Кр2 · поле · 2024");
    assert.equal(await slider.inputValue(), "2024");
    assert.equal(await day(page, "2024-02-29").count(), 1);
    assert.deepEqual(await scale.locator("[data-band]").evaluateAll(elements =>
      elements.map(element => getComputedStyle(element).backgroundColor)), colorsBefore);
    evidence.fixedScale = colorsBefore;
    evidence.yearSlider = {mouse: 2022, end: 2025, left: 2024};
    await slider.press("ArrowLeft");
    await waitForYear(page, "Кр2 · поле · 2023");
    const zero = day(page, "2023-01-18");
    await zero.click();
    assert.equal(await zero.getAttribute("data-band"), "3");
    assert.match(await page.locator("#temperature-reading").textContent(), / · 0 °C$/);
    evidence.zeroDay = await rawReading(page, "kr2", "2023-01-18", {valid: true, sourceRows: 24, mean: 0});

    await page.locator("#temperature-station").selectOption("ame60");
    await waitForYear(page, "АМЕ-60 · 2021");
    assert.equal(await slider.getAttribute("min"), "1992");
    await page.goto(`${base}/calendar?station=ame60&year=2000&day=2000-02-29`);
    await waitForYear(page, "АМЕ-60 · 2000");
    evidence.emptyYear = await assertYear(page, "ame60", 2000);
    assert.equal(evidence.emptyYear.missing, 366);
    assert.equal(await page.locator("#temperature-minimum").textContent(), "—");
    assert.equal(await page.locator("#temperature-maximum").textContent(), "—");
    assert.match(await page.locator("#temperature-status").textContent(), /нет пригодных суток/);
    assert.equal(await day(page, "2000-02-29").getAttribute("aria-pressed"), "true");
    assert.match(await page.locator("#temperature-reading").textContent(), /29 февраля 2000 г\. · Нет наблюдений/);
    evidence.emptyDay = await rawReading(page, "ame60", "2000-02-29", {valid: false, sourceRows: 0, mean: null});

    await page.setViewportSize({width: 1280, height: 900});
    await page.goto(`${base}/calendar?station=kr1&year=2025`);
    await waitForYear(page, "Кр1 · поляна · 2025");
    evidence.desktop = await page.evaluate(() => ({
      viewport: innerWidth,
      document: document.documentElement.scrollWidth,
      monthColumns: getComputedStyle(document.querySelector("#temperature-months")).gridTemplateColumns.split(" ").length,
    }));
    assert.equal(evidence.desktop.monthColumns, 6);
    assert.ok(evidence.desktop.document <= evidence.desktop.viewport);
    assert.deepEqual(errors, []);
    await fs.writeFile(report, JSON.stringify(evidence, null, 2) + "\n");
    console.log("Проверены три станции, карта 2025, високосные годы, ноль, пропуски, ползунок и ширина 390 px.");
  } finally {
    await browser.close();
  }
}

check().catch(error => { console.error(error); process.exitCode = 1; });
