const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

const person = (role, name) => ({
  email: `${role}-${randomBytes(5).toString("hex")}@example.test`,
  name, password: randomBytes(18).toString("hex"),
});
const centerName = "Центр полевых наблюдений";
const admin = person("admin", "Управляющий центра");
const researcher = person("research", "Полевой исследователь");

async function browserLogin(page, credentials) {
  const form = page.locator("#account-login");
  await form.getByLabel("Электронная почта", { exact: true }).fill(credentials.email);
  await form.getByLabel("Пароль", { exact: true }).fill(credentials.password);
  await form.getByRole("button", { name: "Войти", exact: true }).click();
}

async function logout(page) {
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await page.locator("#account-login").waitFor();
}

async function createUser(page, user, button) {
  const form = page.locator("#account-add-user");
  await form.getByLabel("Имя", { exact: true }).fill(user.name);
  await form.getByLabel("Электронная почта", { exact: true }).fill(user.email);
  await form.getByLabel("Начальный пароль", { exact: true }).fill(user.password);
  await form.getByRole("button", { name: button, exact: true }).click();
  await form.getByText(`Доступ для ${user.email} создан.`, { exact: true }).waitFor();
  assert.equal(await form.getByLabel("Начальный пароль", { exact: true }).inputValue(), "");
}

async function brand(page, name, hasReserveLogo) {
  const sidebar = page.locator("[data-archive-nav]");
  await sidebar.getByText(name, { exact: true }).waitFor();
  assert.equal(await sidebar.locator(".reserve-logo").isVisible(), hasReserveLogo);
}

async function check(base, { globalAdmin }) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, locale: "ru-RU" });
    await page.route("**/*", route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(`${base}/account?return=${encodeURIComponent("//example.test/redirect")}`);
    assert.equal(await page.locator("#account-centers").isVisible(), false);
    await browserLogin(page, globalAdmin);
    await page.getByRole("heading", { name: "Управление центрами" }).waitFor();
    assert.equal(page.url(), base + "/account");

    const create = page.locator("#account-create-center");
    await create.getByLabel("Название нового центра").fill(centerName);
    await create.getByRole("button", { name: "Создать центр", exact: true }).click();
    await create.getByText(`Центр «${centerName}» создан.`, { exact: true }).waitFor();
    await page.locator("#account-user-center").selectOption({ label: centerName });
    await createUser(page, admin, "Пригласить администратора");

    await page.locator("#account-center-choice").selectOption({ label: centerName });
    await page.getByRole("button", { name: "Сменить центр", exact: true }).click();
    await page.locator("#account-center-name").filter({ hasText: centerName }).waitFor();
    await brand(page, centerName, false);
    await page.locator("#account-user-list").getByText(admin.name, { exact: true }).waitFor();
    await logout(page);
    await brand(page, "Центрально-Лесной заповедник", true);

    await browserLogin(page, admin);
    await page.getByRole("heading", { name: "Добавить исследователя", exact: true }).waitFor();
    assert.equal(await page.locator("#account-centers").isVisible(), false);
    assert.equal(await page.locator("#account-user-center").isVisible(), false);
    await createUser(page, researcher, "Добавить исследователя");
    await page.locator("#account-user-list").getByText(researcher.name, { exact: true }).waitFor();
    await logout(page);

    await page.goto(`${base}/account?return=${encodeURIComponent("/forecast?from=account")}`);
    await browserLogin(page, researcher);
    await page.waitForURL(base + "/forecast");
    await brand(page, centerName, false);
    const saveButton = page.getByRole("button", { name: "Сохранить сессию", exact: true, includeHidden: true });
    assert.equal(await saveButton.count(), 0);
    await page.locator("[data-archive-nav]").getByRole("link", { name: "Личный кабинет", exact: true }).click();
    await page.getByRole("heading", { name: researcher.name, exact: true }).waitFor();
    assert.equal(await page.locator("#account-centers").isVisible(), false);
    assert.equal(await page.locator("#account-users").isVisible(), false);
    assert.equal(await saveButton.count(), 0);
    await logout(page);

    const workspace = {
      stations: "kr1,kr2", charts: "air,rh", start: "2024-01-01", end: "2024-02-04",
      intervals: "2024-01-01:2024-01-02,2024-02-03:2024-02-04", mode: "togu", grain: "day",
    };
    await page.goto(base + "/observations?" + new URLSearchParams(workspace));
    await page.waitForFunction(() => document.querySelectorAll("#selected-stations li").length === 2);
    assert.equal(await saveButton.count(), 1);
    await page.locator("main").getByRole("button", { name: "Сохранить сессию", exact: true }).click();
    await page.waitForURL(url => url.pathname === "/account");
    const next = new URL(new URL(page.url()).searchParams.get("return"), base);
    for (const [key, value] of Object.entries(workspace)) assert.equal(next.searchParams.get(key), value);
    await browserLogin(page, globalAdmin);
    await page.waitForURL(url => url.pathname === "/observations" && !url.searchParams.has("save-session"));
    const save = page.getByRole("dialog", { name: "Сохранить сессию", exact: true });
    await save.getByLabel("Название сессии", { exact: true }).fill("Сессия наблюдений после входа");
    for (const [key, value] of Object.entries(workspace)) assert.equal(new URL(page.url()).searchParams.get(key), value);
    await save.getByRole("button", { name: "Сохранить", exact: true }).click();
    await page.locator(".archive-session-feedback").filter({ hasText: "Сессия сохранена в личном кабинете." }).waitFor();
    await page.locator("[data-archive-nav]").getByRole("link", { name: "Личный кабинет", exact: true }).click();
    assert.equal(await saveButton.count(), 0);
    await page.getByRole("button", { name: "Открыть сессию Сессия наблюдений после входа", exact: true }).click();
    await page.waitForURL(url => url.pathname === "/observations");
    assert.equal(new URL(page.url()).origin, base);
    for (const [key, value] of Object.entries(workspace)) assert.equal(new URL(page.url()).searchParams.get(key), value);
    await page.goto(base + "/account");
    const remove = page.getByRole("button", { name: "Удалить сессию Сессия наблюдений после входа", exact: true });
    await remove.click();
    const confirmation = page.getByRole("dialog", { name: "Удалить сессию?", exact: true });
    await confirmation.getByText("Удалить «Сессия наблюдений после входа» из сохранённых сессий?", { exact: true }).waitFor();
    await confirmation.getByRole("button", { name: "Отмена", exact: true }).click();
    await remove.click();
    await confirmation.getByRole("button", { name: "Удалить", exact: true }).click();
    await page.getByText("Сессия удалена. Сохранённых сессий пока нет.", { exact: true }).waitFor();
    assert.equal(await remove.count(), 0);
    await page.setViewportSize({ width: 390, height: 820 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    await page.getByRole("button", { name: "Открыть меню", exact: true }).click();
    assert.equal(await page.locator("[data-archive-nav]").getByRole("link", { name: "Личный кабинет", exact: true }).isVisible(), true);
    console.log("PASS: роли, выбор центра, безопасный вход, рабочее место после входа и удаление с подтверждением");
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check, { accounts: true }).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
