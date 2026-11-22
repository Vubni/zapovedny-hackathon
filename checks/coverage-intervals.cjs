const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

const ranges = [
  { start: "2024-02-28", end: "2024-02-29" },
  { start: "2024-04-01", end: "2024-04-05" },
  { start: "2024-04-25", end: "2024-04-30" },
];
const encoded = ranges.map(({ start, end }) => `${start}:${end}`).join(",");
const selection = { stations: "kr1,ame60", start: ranges[0].start, end: ranges.at(-1).end };

async function json(page, base, route, query, expected = 200) {
  const response = await page.request.get(`${base}${route}?${new URLSearchParams(query)}`);
  assert.equal(response.status(), expected, await response.text());
  return response.json();
}

function matrix(rows) {
  return rows.map(row => [row.station, row.valid_days, row.total_days,
    row.cells.map(cell => [cell.valid_days, cell.observed_days, cell.total_days])]);
}

async function checkHttp(page, base) {
  const route = "/api/observation-coverage";
  const year = { stations: "kr1,ame60", start: "2024-01-01", end: "2024-12-31" };
  const total = await json(page, base, route, year);
  const pressure = await json(page, base, route, { ...year, chart: "pressure" });
  assert.deepEqual([total.rows[0].valid_days, pressure.rows[0].valid_days], [363, 334]);
  assert.equal(pressure.rows[0].total_days, 366);
  assert.deepEqual(pressure.rows[0].cells[3], {
    period: "2024-04-01", valid_days: 2, observed_days: 3, total_days: 30, ratio: 2 / 30,
    status: "excluded", month_valid: false, longest_gap: 28,
  });
  assert.deepEqual(pressure.rows[0].station_cells[3], {
    period: "2024-04-01", valid_days: 28, observed_days: 30, total_days: 30, ratio: 28 / 30,
    status: "gaps", month_valid: true, longest_gap: 2,
  });
  assert.equal(pressure.rows[1].supported, false);
  assert.ok(pressure.rows[1].cells.every(cell => cell.ratio === null));

  const query = { ...selection, intervals: encoded };
  const partial = await json(page, base, route, query);
  const filtered = await json(page, base, route, { ...query, parameter: "pressure_hpa" });
  assert.deepEqual(partial.periods.map(period => [period.id, period.total_days]),
    [["2024-02-01", 2], ["2024-04-01", 11]]);
  assert.deepEqual(partial.periods[1].intervals, ranges.slice(1));
  assert.deepEqual(matrix(partial.rows), [
    ["kr1", 11, 13, [[2, 2, 2], [9, 11, 11]]],
    ["ame60", 0, 13, [[0, 0, 2], [0, 0, 11]]],
  ]);
  assert.deepEqual(matrix(filtered.rows)[0], ["kr1", 4, 13, [[2, 2, 2], [2, 3, 11]]]);
  assert.equal(filtered.rows[0].cells[1].month_valid, null);
  assert.ok(filtered.rows[1].cells.every(cell => cell.ratio === null));

  const cropped = await json(page, base, "/api/observations", { ...query, chart: "pressure", grain: "day" });
  const kr1 = cropped.series.filter(series => series.station === "kr1").flatMap(series => series.points);
  assert.equal(kr1.length, 13);
  assert.ok(kr1.every(point => ranges.some(({ start, end }) => point.x >= start && point.x <= end)));
  assert.equal(kr1.filter(point => point.y != null).length, 4);
  const invalid = await json(page, base, route, { ...query, intervals: "2024-04-05:2024-04-01" }, 400);
  assert.ok(invalid.error);
  return {
    kr1_2024: { total: 366, general_valid: 363, pressure_valid: 334, april_station_valid: 28,
      april_pressure_valid: 2, april_pressure_status: "excluded" },
    selected_month_days: { february: 2, april: 11 },
    selected_kr1_coverage: { total: 13, general_valid: 11, pressure_valid: 4 },
    ame60_pressure_supported: false,
    chart_points_outside_selection: 0,
  };
}

async function checkBrowser(page, base) {
  const query = new URLSearchParams({
    ...selection, intervals: encoded.split(",").reverse().join(","), grain: "day", charts: "pressure",
  });
  await page.goto(`${base}/?${query}`);
  const overview = page.locator("#coverage-overview");
  assert.equal(await overview.isVisible(), false);
  const fields = page.locator("#observation-date-rows input[type=date]");
  assert.deepEqual(await fields.evaluateAll(inputs => inputs.map(input => input.value)),
    ranges.flatMap(({ start, end }) => [start, end]));
  await page.getByRole("button", { name: "Добавить график", exact: true }).click();
  const catalog = page.getByRole("dialog", { name: "Каталог графиков" });
  const matrixToggle = catalog.getByRole("checkbox", { name: "Покрытие наблюдений по станциям" });
  await matrixToggle.check();
  await catalog.getByRole("button", { name: "Готово", exact: true }).click();
  const table = overview.getByRole("table", { name: /Кр1.*2024/ });
  await table.waitFor();
  const cell = (row, column) => table.locator(`button[data-row="${row}"][data-column="${column}"]`);
  const ame = overview.getByRole("table", { name: /АМЕ-60.*2024/ });
  const aprilAme = ame.locator('button[data-row="1"][data-column="1"]');
  assert.equal(await table.locator(".coverage-overview-period").count(), 2);
  assert.deepEqual(await table.locator("tbody th").allInnerTexts(), ["Станция", "Давление"]);
  assert.equal(await cell(0, 1).textContent(), "9/11");
  assert.equal(await cell(1, 1).textContent(), "2/11");
  assert.equal(await ame.locator('button[data-row="0"][data-column="1"]').getAttribute("data-level"), "absent");
  assert.equal(await aprilAme.getAttribute("data-level"), "unsupported");
  await cell(0, 0).focus();
  await cell(0, 0).press("End");
  assert.equal(await cell(0, 1).getAttribute("aria-pressed"), "true");
  const detail = overview.locator(".coverage-overview-detail");
  assert.match(await detail.textContent(), /9 из 11 сут/);
  await cell(0, 1).press("ArrowDown");
  assert.equal(await cell(1, 1).getAttribute("aria-pressed"), "true");
  assert.match(await detail.textContent(), /2 из 11 сут/);
  await aprilAme.focus();
  assert.equal(await aprilAme.getAttribute("aria-pressed"), "true");
  assert.match(await detail.textContent(), /Станция не измеряет этот параметр/);
  const field = overview.getByRole("combobox", { name: "Параметр покрытия" });
  await field.selectOption("t_air");
  await table.getByRole("rowheader", { name: "Температура воздуха" }).waitFor();
  await field.selectOption("pressure_hpa");
  await table.getByRole("rowheader", { name: "Давление" }).waitFor();

  await page.reload();
  await table.waitFor();
  assert.deepEqual(await fields.evaluateAll(inputs => inputs.map(input => input.value)),
    ranges.flatMap(({ start, end }) => [start, end]));
  const coverageDetail = page.locator('[data-observation-chart="pressure"] details.obs-chart__coverage');
  await coverageDetail.locator("summary").click();
  const cardTable = coverageDetail.getByRole("table", { name: /Кр1.*2024/ });
  assert.equal(await cardTable.locator('button[data-row="1"][data-column="1"]').textContent(), "2/11");
  await page.setViewportSize({ width: 320, height: 840 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320);
  await page.getByRole("button", { name: "Очистить выбор", exact: true }).click();
  await overview.getByText("Выберите станции, чтобы увидеть покрытие.", { exact: true }).waitFor();
  assert.equal(await table.count(), 0);
  return { keyboard_navigation: true, intervals_persisted_on_reload: true, coverage_cleared_without_stations: true,
    station_and_parameter_rows: true, same_coverage_in_chart: true };
}

async function check(base) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 840 }, locale: "ru-RU" });
    page.setDefaultTimeout(10000);
    await page.route("**/*", route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const http = await checkHttp(page, base);
    const chromium = await checkBrowser(page, base);
    return {
      command: "node checks/coverage-intervals.cjs",
      database: "Temporary METEO_OBSERVATION_DB",
      intervals: ranges,
      http,
      chromium,
    };
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check).then(async report => {
  const destination = path.join(__dirname, "..", "results", "observations", "coverage-intervals.json");
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(destination, JSON.stringify(report, null, 2) + "\n");
  console.log("PASS: архивное покрытие, три интервала, клавиатура, пустые станции и 320 px");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
