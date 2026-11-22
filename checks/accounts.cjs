const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const { chromium, request } = require("playwright");
const { withIsolatedMeteo, loginAccount, createCenter, createAccount } = require("./isolated-meteo.cjs");

const days = ["2024-01-05", "2025-07-05"];
const sessionName = "Личное наблюдение · два интервала";
const fixtureCenters = [
  { suffix: "north", name: "Проверочный северный центр", station: "accounts-north", temperature: 12 },
  { suffix: "south", name: "Проверочный южный центр", station: "accounts-south", temperature: 24 },
];

function credentials(role, fixture) {
  return {
    email: `${role}.${fixture.suffix}@example.test`, name: `${role} · ${fixture.name}`,
    password: randomBytes(24).toString("hex"), role, center_id: fixture.center.id,
  };
}

async function json(pending, status = 200) {
  const response = await pending;
  const body = await response.text();
  assert.equal(response.status(), status, `${response.url()}: ${body.slice(0, 400)}`);
  return JSON.parse(body);
}

function observationPath(station) {
  return "/api/observations?" + new URLSearchParams({
    stations: station, chart: "air", grain: "day", start: days[0], end: days[0],
  });
}

async function provision(base, client, globalApi, fixture) {
  fixture.center = await createCenter(globalApi, base, { name: fixture.name });
  fixture.admin = await createAccount(globalApi, base, credentials("center_admin", fixture));
  fixture.adminApi = await client(fixture.admin);
  fixture.researcher = await createAccount(fixture.adminApi, base, credentials("researcher", fixture));
  fixture.researcherApi = await client(fixture.researcher);
  const passport = {
    id: fixture.station, label: `Метеопост · ${fixture.name}`, params: ["t_air", "rh"], minimum_slots: 2,
  };
  await json(fixture.researcherApi.post(`${base}/api/stations`, { data: passport }), 403);
  const registered = await json(fixture.adminApi.post(`${base}/api/stations`, { data: passport }), 201);
  assert.equal(registered.station.id, fixture.station);
  const readings = ["datetime,t_air,rh", ...days.flatMap(day =>
    [0, 1, 2].map(hour => `${day} 0${hour}:00,${fixture.temperature},70`))].join("\n") + "\n";
  const uploadQuery = new URLSearchParams({ filename: `${fixture.station}.csv`, station: fixture.station });
  const upload = api => api.post(`${base}/api/files/upload?${uploadQuery}`, {
    headers: { "Content-Type": "text/csv" }, data: readings,
  });
  await json(upload(fixture.researcherApi), 403);
  fixture.file = await json(upload(fixture.adminApi), 201);
  assert.equal(fixture.file.rows, 6);
  const settings = { id: fixture.file.id, station: fixture.station };
  const preview = await json(fixture.adminApi.post(`${base}/api/files/preview`, { data: settings }));
  const confirm = api => api.post(`${base}/api/files/confirm`, {
    data: { ...settings, revision: preview.revision },
  });
  await json(confirm(fixture.researcherApi), 403);
  const accepted = await json(confirm(fixture.adminApi));
  assert.equal(accepted.status, "confirmed");
  const graph = await json(fixture.researcherApi.get(base + observationPath(fixture.station)));
  assert.equal(graph.series[0].points[0].y, fixture.temperature);
}

async function checkManagement(base, guest, globalApi, first, second) {
  const me = await json(globalApi.get(`${base}/api/account/me`));
  assert.equal(me.user.role, "global_admin");
  assert.ok([first, second].every(fixture => me.centers.some(center => center.id === fixture.center.id)));
  await json(guest.post(`${base}/api/account/centers`, { data: { name: "Попытка без входа" } }), 401);
  for (const current of [first, second]) {
    const local = await json(current.adminApi.get(`${base}/api/account/users`));
    assert.ok(local.users.some(user => user.id === current.researcher.id));
    assert.ok(local.users.every(user => user.center_id === current.center.id));
    for (const api of [current.adminApi, current.researcherApi]) {
      await json(api.post(`${base}/api/account/centers`, { data: { name: "Недоступное управление" } }), 403);
    }
    await json(current.researcherApi.get(`${base}/api/account/users`), 403);
    await json(current.researcherApi.post(`${base}/api/account/users`, {
      data: { ...credentials("researcher", current), email: `blocked.${current.suffix}@example.test` },
    }), 403);
  }
  await json(first.adminApi.post(`${base}/api/account/users`, {
    data: { ...credentials("researcher", second), email: "blocked.foreign@example.test" },
  }), 403);
  await json(first.adminApi.post(`${base}/api/account/users`, {
    data: { ...credentials("center_admin", first), email: "blocked.role@example.test" },
  }), 403);
  await json(first.researcherApi.post(`${base}/api/account/select-center`, {
    data: { center_id: second.center.id },
  }), 403);
  const stayed = await json(first.researcherApi.get(`${base}/api/account/me`));
  assert.equal(stayed.center.id, first.center.id);
  for (const fixture of [first, second]) {
    const selected = await json(globalApi.post(`${base}/api/account/select-center`, {
      data: { center_id: fixture.center.id },
    }));
    assert.equal(selected.center.id, fixture.center.id);
    const { stations } = await json(globalApi.get(`${base}/api/stations`));
    assert.deepEqual(stations.map(station => station.id), [fixture.station]);
  }
}

async function inaccessibleLikeMissing(api, foreignUrl, unknownUrl) {
  const missing = await api.get(unknownUrl);
  assert.ok([400, 404].includes(missing.status()), `Неожиданный ответ для неизвестного ресурса: ${missing.status()}`);
  const foreign = await api.get(foreignUrl);
  assert.equal(foreign.status(), missing.status(), `Чужой ресурс ${foreignUrl}`);
}

async function checkDataIsolation(base, guest, first, second) {
  for (const [own, other] of [[first, second], [second, first]]) {
    const { researcherApi: api } = own;
    const { stations } = await json(api.get(`${base}/api/stations`));
    assert.deepEqual(stations.map(station => station.id), [own.station]);
    const { files } = await json(api.get(`${base}/api/files`));
    assert.deepEqual(files.map(file => file.id), [own.file.id]);
    await inaccessibleLikeMissing(api, base + observationPath(other.station), base + observationPath("unknown-station"));
    for (const route of ["item", "original"]) {
      await inaccessibleLikeMissing(api, `${base}/api/files/${route}?id=${other.file.id}`,
        `${base}/api/files/${route}?id=${"0".repeat(32)}`);
    }
    const report = await json(api.post(`${base}/api/review?file_id=${own.file.id}&param=t_air`, { data: "" }));
    assert.equal(report.station, own.station);
    await json(other.researcherApi.get(`${base}/api/review?token=${report.token}`), 410);
    const deniedPreview = await api.post(`${base}/api/files/preview`, { data: { id: other.file.id, station: own.station } });
    assert.ok([400, 403, 404].includes(deniedPreview.status()), "Чужой файл недоступен для обработки");
    const preserved = await json(other.researcherApi.get(`${base}/api/files/item?id=${other.file.id}`));
    assert.equal(preserved.status, "confirmed");
    assert.equal(preserved.preview, null);
  }
  const anonymousCatalog = await json(guest.get(`${base}/api/stations`));
  assert.ok(anonymousCatalog.stations.every(station => ![first.station, second.station].includes(station.id)));
  await json(guest.get(`${base}/api/files`), 401);
}

async function assertWorkspace(page, station) {
  const search = new URL(page.url()).searchParams;
  assert.equal(search.get("stations"), station);
  assert.equal(search.get("intervals"), days.map(day => `${day}:${day}`).join(","));
  assert.equal(search.get("grain"), "day");
  assert.equal(search.get("mode"), "togu");
  assert.equal(search.get("coverage"), "1");
  assert.equal(search.get("coverageParameter"), "rh");
  for (const chart of ["air", "rh"]) {
    await page.locator(`[data-observation-chart="${chart}"] .obs-chart__plot svg`).waitFor();
  }
  assert.deepEqual(await page.locator("[data-observation-chart]").evaluateAll(nodes =>
    nodes.map(node => node.dataset.observationChart)), ["air", "rh"]);
  assert.equal(await page.locator("#selected-stations li").count(), 1);
  assert.deepEqual(await page.locator("#observation-date-rows input[type=date]").evaluateAll(inputs =>
    inputs.map(input => input.value)), days.flatMap(day => [day, day]));
  const coverage = page.locator("#coverage-overview");
  assert.equal(await coverage.getByRole("combobox", { name: "Параметр покрытия" }).inputValue(), "rh");
  for (const year of ["2024", "2025"]) {
    const table = coverage.getByRole("table", { name: new RegExp(`${year}$`) });
    await table.getByRole("rowheader", { name: "Влажность", exact: true }).waitFor();
    await table.getByRole("button", { name: /Влажность.*1 из 1/ }).waitFor();
  }
}

async function checkSavedWorkspace(browser, base, first, second) {
  const original = await browser.newContext({ locale: "ru-RU", viewport: { width: 1280, height: 900 } });
  const restored = await browser.newContext({ locale: "ru-RU", viewport: { width: 1280, height: 900 } });
  const errors = [];
  for (const context of [original, restored]) {
    context.on("page", page => page.on("pageerror", error => errors.push(error.message)));
  }
  try {
    await loginAccount(original.request, base, first.researcher);
    const sourcePage = await original.newPage();
    sourcePage.setDefaultTimeout(12000);
    const periods = new URLSearchParams({
      stations: first.station, start: days[0], end: days[1], intervals: days.map(day => `${day}:${day}`).join(","),
      charts: "air,rh", grain: "day", mode: "togu",
    });
    await sourcePage.goto(`${base}/observations?${periods}`);
    await sourcePage.getByRole("button", { name: "Добавить график", exact: true }).click();
    const catalog = sourcePage.getByRole("dialog", { name: "Каталог графиков" });
    await catalog.getByRole("checkbox", { name: "Покрытие наблюдений по станциям" }).check();
    await catalog.getByRole("button", { name: "Готово", exact: true }).click();
    const coverageParameter = sourcePage.locator("#coverage-overview").getByRole("combobox", { name: "Параметр покрытия" });
    await coverageParameter.selectOption("rh");
    await sourcePage.waitForURL(url => url.searchParams.get("coverageParameter") === "rh");
    await assertWorkspace(sourcePage, first.station);
    const saveName = { name: "Сохранить сессию", exact: true };
    const nav = sourcePage.locator("aside[data-archive-nav]");
    await nav.getByRole("link", { name: "Наблюдения", exact: true }).waitFor();
    const saveButton = sourcePage.locator("main").getByRole("button", saveName);
    await saveButton.waitFor();
    assert.equal(await nav.getByRole("button", { ...saveName, includeHidden: true }).count(), 0);
    assert.equal(await sourcePage.getByRole("button", { ...saveName, includeHidden: true }).count(), 1);
    await saveButton.click();
    const save = sourcePage.getByRole("dialog", { name: "Сохранить сессию", exact: true });
    await save.getByLabel("Название сессии", { exact: true }).fill(sessionName);
    const [response] = await Promise.all([
      sourcePage.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname === "/api/account/sessions" && response.request().method() === "POST";
      }),
      save.getByRole("button", { name: "Сохранить", exact: true }).click(),
    ]);
    const { session } = await json(response, 201);
    assert.equal(session.name, sessionName);
    assert.equal(session.center_id, first.center.id);
    for (const other of [first.adminApi, second.researcherApi]) {
      const { sessions } = await json(other.get(`${base}/api/account/sessions`));
      assert.ok(sessions.every(item => item.id !== session.id));
      const denied = await other.get(`${base}/api/account/session?id=${session.id}`);
      assert.equal(denied.status(), 404, "Сохранение приватно и внутри своего центра");
    }

    assert.deepEqual(await restored.storageState(), { cookies: [], origins: [] });
    const page = await restored.newPage();
    page.setDefaultTimeout(12000);
    await page.goto(`${base}/account`);
    const loginForm = page.locator("#account-login");
    await loginForm.getByLabel("Электронная почта", { exact: true }).fill(first.researcher.email);
    await loginForm.getByLabel("Пароль", { exact: true }).fill(first.researcher.password);
    const [login] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === "/api/account/login"),
      loginForm.getByRole("button", { name: "Войти", exact: true }).click(),
    ]);
    assert.equal(login.status(), 200);
    await page.locator("#account-sessions").waitFor();
    assert.equal((await json(restored.request.get(`${base}/api/account/me`))).user.id, first.researcher.id);
    assert.equal(await page.getByRole("button", { ...saveName, includeHidden: true }).count(), 0);
    await page.locator("#account-session-list").getByRole("button", { name: `Открыть сессию ${sessionName}`, exact: true }).click();
    await page.waitForURL(url => url.pathname === "/observations" || url.pathname === "/");
    await assertWorkspace(page, first.station);
    assert.deepEqual(errors, []);
  } finally {
    await original.close();
    await restored.close();
  }
}

async function run(base, { globalAdmin }) {
  const apiContexts = [];
  async function client(account) {
    const api = await request.newContext();
    apiContexts.push(api);
    if (account) {
      const me = await loginAccount(api, base, account);
      if (account.role) assert.equal(me.user.role, account.role);
      if (account.center_id) assert.equal(me.center.id, account.center_id);
    }
    return api;
  }
  try {
    const guest = await client();
    const globalApi = await client(globalAdmin);
    const [first, second] = fixtureCenters.map(fixture => ({ ...fixture }));
    for (const fixture of [first, second]) await provision(base, client, globalApi, fixture);
    await checkManagement(base, guest, globalApi, first, second);
    await checkDataIsolation(base, guest, first, second);
    const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
    try {
      await checkSavedWorkspace(browser, base, first, second);
    } finally {
      await browser.close();
    }
    console.log("PASS: два центра, управление по ролям, границы данных и личное восстановление графиков, дат и покрытия.");
  } finally {
    for (const api of apiContexts) await api.dispose();
  }
}

withIsolatedMeteo(run, { accounts: true }).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
