const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const origin = `http://127.0.0.1:${process.env.METEO_TEST_PORT || 8765}`;
const firstCenter = { id: "center-a", name: "Заповедник А" };
const secondCenter = { id: "center-b", name: "Заповедник Б" };
const intervals = "2024-07-01:2024-07-31,2025-07-01:2025-07-31";
const settings = {
  stations: "kr1,kr2", start: "2024-07-01", end: "2025-07-31", intervals,
  grain: "day", mode: "togu", charts: "air,pressure", calendar: "1",
  coverage: "1", coverageParameter: "t_air", indicators: "air_mean,pressure_mean",
};

function account(id, role, center, home = center) {
  return {
    user: id ? { id, role, name: id, email: `${id}@example.test`, center_id: home.id } : null,
    center, centers: [firstCenter, secondCenter],
  };
}

const chart = (page, id) => page.locator(`[data-observation-chart="${id}"]`);

function workspace(page) {
  return page.evaluate(async () => (await import("/observation-session.js")).observationWorkspace());
}

async function registrationAvailability(page, allowed) {
  await page.getByRole("button", { name: "Выбрать станции", exact: true }).click();
  const picker = page.locator("dialog.station-picker");
  await picker.waitFor();
  assert.equal(await picker.locator("[data-add]").count(), Number(allowed));
  assert.ok(await picker.getByRole("checkbox").count() > 0);
  await page.keyboard.press("Escape");
  await picker.waitFor({ state: "hidden" });
}

async function defaultWorkspaceOnPage(page) {
  await chart(page, "air").locator(".obs-chart__plot svg").waitFor();
  assert.equal(await chart(page, "pressure").count(), 0);
  assert.equal(await page.locator(".station-chip").count(), 1);
  assert.equal(await page.locator("#coverage-overview").isHidden(), true);
  assert.equal(await page.locator("#observation-indicators").isHidden(), true);
  assert.equal(await page.locator("#align-observation-calendar").isHidden(), true);
}

async function verifyRestored(page) {
  await chart(page, "air").locator('svg[data-axis="calendar"]').waitFor();
  await chart(page, "pressure").locator('svg[data-axis="calendar"]').waitFor();
  assert.equal(await page.locator(".station-chip").count(), 2);
  assert.equal(await page.getByRole("button", { name: "По пособию ТОГУ", exact: true }).getAttribute("aria-pressed"), "true");
  assert.equal(await page.locator("#observation-grain").inputValue(), "day");
  assert.equal(await page.locator("#align-observation-calendar").isChecked(), true);
  assert.equal(await page.locator("#show-coverage").isChecked(), true);
  await page.locator("#coverage-overview .coverage-overview-content").waitFor();
  assert.equal(await page.locator("#coverage-overview").getByLabel("Параметр покрытия", { exact: true }).inputValue(), "t_air");
  assert.equal(await page.locator("[data-observation-indicator]:visible").count(), 2);
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, locale: "ru-RU" });
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    let identity = account("researcher-a", "researcher", firstCenter);
    await context.route("**/api/account/me", route => identity
      ? route.fulfill({ json: identity }) : route.fulfill({ status: 503, json: {} }));
    let savedRequest;
    await context.route("**/api/account/sessions", route => {
      if (route.request().method() !== "POST") return route.continue();
      savedRequest = route.request().postDataJSON();
      return route.fulfill({ status: 201, json: { id: "browser-check" } });
    });
    await context.addInitScript(() => {
      localStorage.setItem("meteoarchive.observations.v1", JSON.stringify({ selected: ["kr2"], charts: ["pressure"] }));
      localStorage.setItem("meteoarchive.observations.coverage", "1");
      localStorage.setItem("meteoarchive.indicators.v1", '["rh_mean"]');
    });

    await page.goto(`${origin}/forecast`);
    const fallback = new URL(await workspace(page), origin);
    assert.equal(fallback.pathname, "/observations");
    assert.equal(fallback.searchParams.get("stations"), "kr1");
    assert.equal(fallback.searchParams.get("coverage"), "0");
    assert.equal(fallback.searchParams.get("indicators"), "");
    const linksRejected = await page.evaluate(async () => {
      const session = await import("/observation-session.js");
      return session.restoreObservationWorkspace("https://example.test/observations?stations=kr1") === false &&
        session.restoreObservationWorkspace("/observations?center=center-b") === false &&
        session.restoreObservationWorkspace("/observations?stations=kr1&stations=kr2") === false;
    });
    assert.equal(linksRejected, true);

    await page.goto(`${origin}/observations?${new URLSearchParams(settings)}`);
    await verifyRestored(page);
    await registrationAvailability(page, false);
    const legend = chart(page, "air").locator(".obs-chart__legend button").first();
    await legend.click();
    assert.equal(await legend.getAttribute("aria-pressed"), "false");
    const legendOnly = new URL(await workspace(page), origin).searchParams.get("presentation");
    assert.deepEqual(Object.keys(JSON.parse(legendOnly).air), ["hidden"]);
    await chart(page, "air").getByRole("button", { name: "Увеличить масштаб", exact: true }).click();
    await chart(page, "air").getByRole("button", { name: "Сбросить масштаб", exact: true }).waitFor();
    const saved = await workspace(page);
    const url = new URL(saved, origin);
    for (const [key, value] of Object.entries(settings)) assert.equal(url.searchParams.get(key), value);
    const presentation = JSON.parse(url.searchParams.get("presentation"));
    assert.deepEqual(Object.keys(presentation), ["air"]);
    assert.deepEqual(Object.keys(presentation.air).sort(), ["hidden", "zoom"]);
    assert.equal(presentation.air.hidden.length, 1);
    assert.equal(presentation.air.zoom.length, 2);
    assert.equal(url.searchParams.has("center"), false);
    assert.equal(url.searchParams.has("user"), false);
    assert.equal(await page.locator("[data-archive-nav] [data-save-observation-workspace]").count(), 0);
    await page.locator("main").getByRole("button", { name: "Сохранить сессию", exact: true }).click();
    const saveDialog = page.getByRole("dialog", { name: "Сохранить сессию", exact: true });
    await saveDialog.getByLabel("Название сессии", { exact: true }).fill("Рабочее место исследователя");
    await saveDialog.getByRole("button", { name: "Сохранить", exact: true }).click();
    await saveDialog.waitFor({ state: "hidden" });
    assert.deepEqual(savedRequest, { name: "Рабочее место исследователя", state: { workspace: saved } });

    await page.goto(`${origin}/forecast`);
    assert.equal(await workspace(page), saved);
    await page.evaluate(async path => (await import("/observation-session.js")).restoreObservationWorkspace(path), saved);
    await page.waitForURL(saved.startsWith("/") ? origin + saved : saved);
    await verifyRestored(page);
    assert.equal(await legend.getAttribute("aria-pressed"), "false");
    await chart(page, "air").getByRole("button", { name: "Сбросить масштаб", exact: true }).waitFor();
    assert.equal(await workspace(page), saved);

    identity = account("researcher-b", "researcher", firstCenter);
    await page.evaluate(async () => (await import("/account-client.js")).invalidateAccountContext());
    await page.waitForURL(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    assert.notEqual(await workspace(page), saved);

    identity = account("researcher-a", "researcher", secondCenter);
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    assert.notEqual(await workspace(page), saved);
    identity = account(null, null, firstCenter);
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    await registrationAvailability(page, false);

    identity = account("center-admin", "center_admin", firstCenter);
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    await registrationAvailability(page, true);
    identity = account("center-admin", "center_admin", secondCenter, firstCenter);
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    await registrationAvailability(page, false);
    identity = account("global-admin", "global_admin", secondCenter, firstCenter);
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    await registrationAvailability(page, true);

    identity = account("researcher-a", "researcher", firstCenter);
    await page.goto(`${origin}/observations`);
    await verifyRestored(page);
    assert.equal(await workspace(page), saved);

    identity = null;
    await page.goto(`${origin}/observations`);
    await defaultWorkspaceOnPage(page);
    assert.equal(await workspace(page), null);
    await registrationAvailability(page, false);
    assert.deepEqual(errors, []);
    console.log("PASS: portable workspace, coverage parameter, indicators, legend, zoom, account and center isolation, station registration roles.");
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
