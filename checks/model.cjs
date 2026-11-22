const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const { mkdtemp, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { createInterface } = require("node:readline");
const { chromium } = require("playwright");

const stationId = "test-model-new-station";
const stationLabel = "Полевой пост · приёмка модели";
const dates = ["2026-05-16", "2026-05-17", "2026-05-18"];
const format = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const serverProgram = `
from admin_auth import OperatorHTTPServer
from server import Handler, ROOT

with OperatorHTTPServer(("127.0.0.1", 0), Handler, ROOT) as http:
    print(http.server_port, flush=True)
    http.serve_forever()
`;

async function startServer(directory, password) {
  const child = spawn("python3", ["-c", serverProgram], {
    cwd: path.join(__dirname, ".."),
    env: { ...process.env, METEO_ADMIN_PASSWORD: password, METEO_ADMIN_ORIGIN: "",
      METEO_OBSERVATION_DB: path.join(directory, "observations.sqlite3"), PYTHONDONTWRITEBYTECODE: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk; });
  try {
    const lines = createInterface({ input: child.stdout });
    const port = await new Promise((resolve, reject) => {
      lines.once("line", resolve);
      child.once("error", reject);
      child.once("exit", () => reject(new Error(`Не запустился тестовый HTTP-сервер. ${stderr}`)));
    });
    lines.close();
    assert.match(port, /^\d+$/);
    return { process: child, url: `http://127.0.0.1:${port}` };
  } catch (error) {
    if (child.exitCode === null && child.signalCode === null) child.kill();
    throw error;
  }
}

async function json(request, status = 200) {
  const response = await request;
  assert.equal(response.status(), status, await response.text());
  return response.json();
}

function rows(days) {
  return ["datetime,t_air", ...days.flatMap(([day, value]) => Array.from({ length: 24 }, (_, hour) =>
    `${day} ${String(hour).padStart(2, "0")}:00,${value}`))].join("\n") + "\n";
}

async function preview(api, base, name, days) {
  const query = new URLSearchParams({ filename: name, station: stationId });
  const file = await json(api.post(`${base}/api/files/upload?${query}`, {
    headers: { "Content-Type": "text/csv" }, data: Buffer.from(rows(days)),
  }), 201);
  const data = { id: file.id, station: stationId, filters: "", fill: "none" };
  const staged = await json(api.post(`${base}/api/files/preview`, { data }));
  return { ...data, revision: staged.revision };
}

function waitForPrediction(page, station, mode) {
  return page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === "/api/model" && url.searchParams.get("station") === station && url.searchParams.get("mode") === mode;
  });
}

function assertPrediction(page, data) {
  return Promise.all([
    page.getByRole("heading", { name: `Прогноз на ${data.target_day.split("-").reverse().join(".")}` }).waitFor(),
    page.waitForFunction(value => document.querySelector("#model-prediction").textContent === value,
      `${format.format(data.prediction_c)} °C`),
  ]);
}

async function useDataRevision(page, contextPage) {
  const result = page.waitForResponse(response => new URL(response.url()).pathname === "/api/model/catalog");
  await contextPage.evaluate(() => localStorage.setItem("meteoarchive.dataRevision", crypto.randomUUID()));
  return json(result);
}

async function capture(page, directory, filename) {
  if (!directory) return;
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({ path: path.join(directory, filename), fullPage: true });
}

async function run() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "meteo-model-browser-"));
  const password = randomBytes(24).toString("base64url");
  let server;
  let browser;
  try {
    server = await startServer(directory, password);
    const base = server.url;
    browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
    const context = await browser.newContext({ viewport: { width: 1380, height: 950 }, locale: "ru-RU" });
    const page = await context.newPage();
    const errors = [];
    const predictions = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname === "/api/model") predictions.push(url.searchParams.get("station"));
    });

    const api = context.request;
    await json(api.post(`${base}/api/operator/login`, { data: { password } }));
    await json(api.post(`${base}/api/stations`, {
      data: { id: stationId, label: stationLabel, profile: "kr1", params: ["t_air"], latitude: -33.8, longitude: 151.2 },
    }), 201);
    const stationCatalog = await json(api.get(`${base}/api/stations`));
    const initialCatalog = await json(api.get(`${base}/api/model/catalog`));

    const archiveResponse = waitForPrediction(page, initialCatalog.default_station, "archive");
    await page.goto(`${base}/model`);
    const archive = await json(archiveResponse);
    await assertPrediction(page, archive);
    const station = page.getByLabel("Станция", { exact: true });
    const mode = page.getByLabel("Источник наблюдений");
    const day = page.getByLabel("Последний день наблюдений");
    const calculate = page.getByRole("button", { name: "Рассчитать" });
    const known = await station.locator("option").evaluateAll(options => options.map(option => option.value).sort());
    assert.deepEqual(known, stationCatalog.stations.map(option => option.id).sort());
    assert(archive.inputs.every(input => input.origin === "archive"));
    assert(Number.isFinite(archive.observed_c));
    assert(archive.fit.training_through < archive.target_day);
    assert.equal(await page.locator("#model-observed").textContent(), `${format.format(archive.observed_c)} °C`);
    assert.equal(await page.locator("[data-model-test='dates'] tbody tr").count(), initialCatalog.tests.length);
    assert.equal(await page.locator("[data-model-test='transfer'] tbody tr").count(), initialCatalog.transfer_tests.length);
    assert.equal(await page.locator(`[data-model-test='transfer'] tr[data-station='${stationId}']`).count(), 0);
    await page.getByText("Точность в других районах пока не проверялась.", { exact: false }).waitFor();
    await page.locator(".model-method summary").click();
    await page.getByText(/широте из паспорта станции/).waitFor();

    await station.selectOption(stationId);
    await page.getByText(/пока нет подтверждённых наблюдений/).waitFor();
    assert(await calculate.isDisabled());
    assert(await page.locator("#model-result").isHidden());
    assert(predictions.every(id => id !== stationId));

    await mode.selectOption("manual");
    const newStationCalendar = initialCatalog.stations.find(item => item.id === stationId);
    assert.match(newStationCalendar.last_complete_day, /^\d{4}-\d{2}-\d{2}$/);
    assert.equal(await day.inputValue(), newStationCalendar.last_complete_day);
    assert.equal(await day.getAttribute("max"), newStationCalendar.last_complete_day);
    await page.getByText(newStationCalendar.calendar_label, { exact: false }).waitFor();
    await day.fill(dates[1]);
    await page.getByLabel("За предыдущий день, °C").fill("9.5");
    await page.getByLabel("За выбранный день, °C").fill("13.25");
    const manualResponse = waitForPrediction(page, stationId, "manual");
    await calculate.click();
    const manual = await json(manualResponse);
    await assertPrediction(page, manual);
    assert.equal(manual.target_day, dates[2]);
    assert.equal(manual.baseline_c, 13.25);
    assert.equal(manual.fit.kind, "transfer");
    assert(manual.inputs.every(input => input.origin === "manual"));
    assert(await page.locator("#model-observed-row").isHidden());
    const screenshots = process.env.METEO_MODEL_SCREENSHOT_DIR;
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    await capture(page, screenshots, "meteo-model-manual-mobile.png");
    await page.setViewportSize({ width: 1380, height: 950 });

    const contextPage = await context.newPage();
    await contextPage.goto(`${base}/api/stations`);
    const gapFile = await preview(api, base, "model-gap.csv", [[dates[0], 7], [dates[2], 12]]);
    await json(api.post(`${base}/api/files/confirm`, { data: gapFile }));
    const withGap = await useDataRevision(page, contextPage);
    const gapStation = withGap.stations.find(item => item.id === stationId);
    assert.equal(gapStation.has_observations, true);
    assert.equal(gapStation.archive, null);
    await mode.selectOption("archive");
    await page.getByText(/нет двух последовательных суток/).waitFor();
    assert(await calculate.isDisabled());
    assert(await page.locator("#model-result").isHidden());

    const bridge = await preview(api, base, "model-complete.csv", [[dates[1], 10]]);
    const pending = await json(api.get(`${base}/api/model/catalog`));
    assert.equal(pending.stations.find(item => item.id === stationId).archive, null);
    await json(api.post(`${base}/api/files/confirm`, { data: bridge }));
    const confirmedResponse = waitForPrediction(page, stationId, "archive");
    const confirmedCatalog = await useDataRevision(page, contextPage);
    const confirmed = await json(confirmedResponse);
    await assertPrediction(page, confirmed);
    assert.equal(confirmedCatalog.stations.find(item => item.id === stationId).archive.default_source_day, dates[2]);
    assert.deepEqual(confirmed.inputs.map(input => input.temperature_c), [10, 12]);
    assert(confirmed.inputs.every(input => input.origin === "confirmed"));
    assert.equal(await page.locator("#model-inputs li").filter({ hasText: "Подтверждённый файл" }).count(), 2);
    assert.equal(await page.locator("#model-observed").textContent(), "Нет данных");
    await capture(page, screenshots, "meteo-model-confirmed.png");

    await station.selectOption("ame60");
    await page.waitForFunction(() => document.querySelector("#model-result-note").textContent.includes("АМЕ-60"));
    await day.fill("2000-01-10");
    const missingResponse = waitForPrediction(page, "ame60", "archive");
    await calculate.click();
    const missing = await json(missingResponse, 400);
    await page.getByText(missing.error, { exact: true }).waitFor();
    assert(await page.locator("#model-result").isHidden());

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    await capture(page, screenshots, "meteo-model-mobile.png");
    assert.deepEqual(errors, []);

    const report = {
      stationOptions: known.length,
      archive: { station: archive.station, target: archive.target_day, prediction_c: archive.prediction_c,
        baseline_c: archive.baseline_c, observed_c: archive.observed_c, training_through: archive.fit.training_through },
      newStation: { initialArchive: null, separatedDaysArchive: gapStation.archive,
        defaultManualDay: newStationCalendar.last_complete_day, calendar: newStationCalendar.calendar_label, manual, confirmed },
      historical: initialCatalog.tests.map(({ station, days, model_mae_c, baseline_mae_c }) =>
        ({ station, days, model_mae_c, baseline_mae_c })),
      transfer: initialCatalog.transfer_tests.map(({ station, days, model_mae_c, baseline_mae_c, reason }) =>
        ({ station, days, model_mae_c, baseline_mae_c, reason })),
      missingDay: missing.error,
      mobileWidth: 390,
    };
    if (process.env.METEO_MODEL_REPORT) await writeFile(process.env.METEO_MODEL_REPORT, JSON.stringify(report, null, 2) + "\n");
    console.log("Модель: каталог, архив, ручной ввод, подтверждённые данные новой станции, пропуски и историческая проверка прошли.");
  } finally {
    await browser?.close();
    if (server && server.process.exitCode === null && server.process.signalCode === null) {
      const exited = once(server.process, "exit");
      server.process.kill();
      await exited;
    }
    await rm(directory, { recursive: true, force: true });
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
