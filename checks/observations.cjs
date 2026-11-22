const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const root = `http://127.0.0.1:${process.env.METEO_TEST_PORT || 8765}`;
const screenshotDir = path.join(__dirname, "..", "results", "browser");
const originalStart = "2025-07-01";
const originalEnd = "2025-07-31";
const historyStart = "2005-01-01";
const historyEnd = "2015-12-31";
const chart = (page, id) => page.locator(`[data-observation-chart="${id}"]`);

async function getChart(page, action, expected) {
  const [response] = await Promise.all([
    page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === "/api/observations" &&
        Object.entries(expected).every(([name, value]) => url.searchParams.get(name) === value);
    }, { timeout: 25000 }),
    action(),
  ]);
  assert.equal(response.status(), 200, response.url());
  return response.json();
}

async function chooseDates(page, start, end) {
  await page.getByLabel("Дата с", { exact: true }).fill(start);
  await page.getByLabel("Дата по", { exact: true }).fill(end);
  await page.getByRole("button", { name: "Показать", exact: true }).click();
}

async function assertDates(page, start, end) {
  assert.equal(await page.getByLabel("Дата с", { exact: true }).inputValue(), start);
  assert.equal(await page.getByLabel("Дата по", { exact: true }).inputValue(), end);
  for (const [name, value] of [["Начало диапазона", start], ["Конец диапазона", end]]) {
    assert.equal(await page.getByRole("slider", { name }).getAttribute("aria-valuetext"), value.split("-").reverse().join("."));
  }
}

async function dragHandle(page, name, offset) {
  const slider = page.getByRole("slider", { name });
  const value = Number(await slider.inputValue());
  const maximum = Number(await slider.getAttribute("max"));
  const rendered = await slider.boundingBox();
  const x = rendered.x + 7 + (rendered.width - 14) * value / maximum;
  const y = rendered.y + rendered.height / 2;
  const target = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.getAttribute("aria-label"), { x, y });
  assert.equal(target, name);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + offset, y, { steps: 5 });
  await page.mouse.up();
  assert.notEqual(Number(await slider.inputValue()), value);
}

async function screenshot(page, name, fullPage = false) {
  if (process.env.METEO_CAPTURE_OBSERVATIONS !== "1") return;
  await fs.mkdir(screenshotDir, { recursive: true });
  await page.screenshot({ path: path.join(screenshotDir, `${name}.png`), fullPage });
}

async function selectStations(page, choices, capture) {
  await page.getByRole("button", { name: "Выбрать станции" }).click();
  const picker = page.locator("dialog.station-picker");
  await picker.waitFor();
  for (const [name, checked] of choices) {
    await picker.getByRole("checkbox", { name }).setChecked(checked);
  }
  if (capture) await screenshot(page, capture);
  await picker.locator("[data-apply]").click();
  await picker.waitFor({ state: "hidden" });
}

async function hasPlot(page, id) {
  await chart(page, id).locator(".obs-chart__plot svg").first().waitFor({ timeout: 25000 });
}

async function assertDownload(page, trigger, extension, header) {
  const [download] = await Promise.all([page.waitForEvent("download"), trigger()]);
  assert.match(download.suggestedFilename(), new RegExp(`\\.${extension}$`, "i"));
  const bytes = await fs.readFile(await download.path());
  assert.ok(bytes.length > 20);
  assert.ok(header(bytes), `Недопустимое содержимое ${extension}`);
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, locale: "ru-RU" });
  page.setDefaultTimeout(10000);
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));

  try {
    const initial = await getChart(page, () => page.goto(root), {
      stations: "kr1", start: originalStart, end: originalEnd, grain: "day", chart: "air", mode: "modern",
    });
    assert.ok(initial.series[0].points.some(point => Number.isFinite(point.y)));
    await hasPlot(page, "air");
    await assertDates(page, originalStart, originalEnd);
    assert.equal(await page.locator("[data-observation-chart]").count(), 1);
    assert.equal(await page.getByRole("link", { name: "Наблюдения", exact: true }).getAttribute("aria-current"), "page");
    await screenshot(page, "observations");
    await dragHandle(page, "Начало диапазона", -65);
    assert.ok((await page.getByLabel("Дата с", { exact: true }).inputValue()) < originalStart);
    await dragHandle(page, "Конец диапазона", 8);
    assert.ok((await page.getByLabel("Дата по", { exact: true }).inputValue()) > originalEnd);

    const historical = await getChart(page, () => chooseDates(page, historyStart, historyEnd), {
      stations: "kr1", start: historyStart, end: historyEnd, chart: "air",
    });
    assert.ok(historical.series.every(series => series.points.every(point => point.y == null)));
    await chart(page, "air").getByText("Нет данных за этот период", { exact: true }).waitFor();
    await assertDates(page, historyStart, historyEnd);

    const ame = await getChart(page, () => selectStations(page, [[/Кр1/, false], [/АМЕ/, true]], "observations-stations"), {
      stations: "ame60", start: historyStart, end: historyEnd, chart: "air",
    });
    assert.ok(ame.series.some(series => series.points.some(point => Number.isFinite(point.y))));
    assert.ok(ame.series.some(series => series.points.some(point => point.y == null)));
    await hasPlot(page, "air");
    await assertDates(page, historyStart, historyEnd);

    await page.getByLabel("Дата с", { exact: true }).fill("2016-01-01");
    await page.getByText("Дата начала должна быть раньше даты окончания или совпадать с ней.", { exact: true }).waitFor();
    await page.getByRole("slider", { name: "Начало диапазона" }).press("ArrowRight");
    await assertDates(page, "2005-01-02", historyEnd);
    await page.getByRole("slider", { name: "Конец диапазона" }).press("ArrowLeft");
    await assertDates(page, "2005-01-02", "2015-12-30");
    assert.equal(await page.locator("#date-error").isHidden(), true);

    await page.getByRole("button", { name: "Убрать станцию АМЕ", exact: false }).click();
    await chart(page, "air").getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("stations"), "");
    await page.reload();
    await chart(page, "air").getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    await assertDates(page, "2005-01-02", "2015-12-30");
    assert.equal(await page.locator(".station-chip").count(), 0);

    await chooseDates(page, "2025-01-01", "2025-12-31");
    await page.getByLabel("Группировка").selectOption("month");
    const comparison = await getChart(page, () => selectStations(page, [[/Кр1/, true], [/Кр2/, true]]), {
      stations: "kr1,kr2", start: "2025-01-01", end: "2025-12-31", grain: "month", chart: "air",
    });
    assert.deepEqual(comparison.series.map(series => series.station), ["kr1", "kr2"]);
    assert.ok(comparison.series.every(series => series.points.length === 12));
    await hasPlot(page, "air");
    const legend = chart(page, "air").locator(".obs-chart__legend button").filter({ hasText: "Кр2" });
    await legend.click();
    assert.equal(await legend.getAttribute("aria-pressed"), "false");
    await legend.click();

    await page.getByRole("button", { name: "Добавить график" }).click();
    const picker = page.getByRole("dialog", { name: "Каталог графиков" });
    assert.equal(await picker.getByRole("button", { name: "Средняя температура воздуха" }).getAttribute("aria-pressed"), "true");
    const difference = await getChart(page, () => picker.getByRole("button", { name: "Разница температур · Кр2 − Кр1" }).click(), {
      stations: "kr1,kr2", grain: "month", chart: "temp_difference",
    });
    assert.ok(difference.series.some(series => /Кр2\s*[-−–]\s*Кр1/.test(series.label)));
    const diurnal = await getChart(page, () => picker.getByRole("button", { name: "Средний суточный ход температуры" }).click(), {
      stations: "kr1,kr2", grain: "month", chart: "temp_diurnal",
    });
    assert.ok(diurnal.series.every(series => series.points.length === 24));
    await picker.getByRole("button", { name: "Готово" }).click();
    await Promise.all([hasPlot(page, "temp_difference"), hasPlot(page, "temp_diurnal")]);
    assert.equal(new URL(page.url()).searchParams.get("charts"), "air,temp_difference,temp_diurnal");
    await screenshot(page, "observations-comparison", true);

    await getChart(page, () => page.getByRole("button", { name: "По пособию ТОГУ" }).click(), {
      stations: "kr1,kr2", start: "2025-01-01", end: "2025-12-31", grain: "month", chart: "air", mode: "togu",
    });
    await getChart(page, () => page.getByRole("button", { name: "По Семёнову" }).click(), {
      stations: "kr1,kr2", start: "2025-01-01", end: "2025-12-31", grain: "month", chart: "air", mode: "semenov",
    });
    await getChart(page, () => page.getByRole("button", { name: "Современные" }).click(), {
      stations: "kr1,kr2", start: "2025-01-01", end: "2025-12-31", grain: "month", chart: "air", mode: "modern",
    });
    await hasPlot(page, "air");
    await chart(page, "air").locator('[data-chart-action="fullscreen"]').click();
    const fullscreen = page.locator(".obs-chart__fullscreen[open]");
    await fullscreen.waitFor();
    for (const [format, extension, check] of [
      ["svg", "svg", bytes => bytes.toString("utf8").includes("<svg")],
      ["png", "png", bytes => bytes.subarray(1, 4).toString() === "PNG"],
      ["jpeg", "jpe?g", bytes => bytes[0] === 0xff && bytes[1] === 0xd8],
    ]) {
      const formatButton = fullscreen.locator(`[data-chart-format="${format}"]`);
      if (!(await formatButton.isVisible())) {
        await fullscreen.locator('[data-chart-action="download"]').click();
      }
      await assertDownload(page, () => formatButton.click(), extension, check);
    }
    await assertDownload(page, () => fullscreen.locator('[data-chart-action="csv"]').click(), "csv", bytes => bytes.toString("utf8").includes("Кр1"));
    await page.keyboard.press("Escape");
    await fullscreen.waitFor({ state: "hidden" });
    await assertDates(page, "2025-01-01", "2025-12-31");

    await chart(page, "temp_diurnal").locator('[data-chart-action="hide"]').click();
    assert.equal(await chart(page, "temp_diurnal").count(), 0);
    await page.getByRole("link", { name: "Журнал наблюдений", exact: true }).click();
    await page.waitForURL("**/journal");
    await page.getByRole("link", { name: "Переходы через 0 °C" }).waitFor();
    await page.getByRole("link", { name: "Календарь температуры по годам" }).waitFor();
    await page.locator('a[href^="/api/export/latest-five"]').waitFor();
    await page.goBack();
    await hasPlot(page, "air");
    await assertDates(page, "2025-01-01", "2025-12-31");
    assert.equal(await page.locator(".station-chip").count(), 2);
    assert.equal(await chart(page, "temp_difference").count(), 1);
    assert.equal(await chart(page, "temp_diurnal").count(), 0);
    assert.equal(await page.getByLabel("Группировка").inputValue(), "month");

    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    }
    await page.getByRole("button", { name: "Очистить выбор" }).click();
    await chart(page, "air").getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    await assertDates(page, "2025-01-01", "2025-12-31");
    await page.getByRole("button", { name: "Весь период" }).click();
    const stationResponse = await page.request.get(`${root}/api/stations`);
    assert.equal(stationResponse.status(), 200);
    const { start, end } = await stationResponse.json();
    await assertDates(page, start, end);
    const freshPage = await browser.newPage({ viewport: { width: 1440, height: 960 }, locale: "ru-RU" });
    const share = `${root}/?stations=kr1,kr2&start=2025-01-01&end=2025-12-31&grain=month&charts=air,temp_difference`;
    const shared = await getChart(freshPage, () => freshPage.goto(share), {
      stations: "kr1,kr2", start: "2025-01-01", end: "2025-12-31", grain: "month", chart: "air",
    });
    assert.deepEqual(shared.series.map(series => series.station), ["kr1", "kr2"]);
    await assertDates(freshPage, "2025-01-01", "2025-12-31");
    await hasPlot(freshPage, "temp_difference");
    await freshPage.goto(share.replace("stations=kr1,kr2", "stations="));
    await chart(freshPage, "air").getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    assert.equal(await freshPage.locator(".station-chip").count(), 0);
    await assertDates(freshPage, "2025-01-01", "2025-12-31");
    await freshPage.close();
    assert.deepEqual(pageErrors, []);
    console.log("Наблюдения: стартовые данные, диапазон 2005–2015, АМЕ-60, ползунки мышью и клавиатурой, пустой выбор, сравнение, режимы, экспорт, ссылки журнала, общий URL, 390 и 320 px.");
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
