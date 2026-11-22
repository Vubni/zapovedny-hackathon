const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

async function width(sidebar) {
  return Math.round((await sidebar.boundingBox()).width);
}

async function checkBrand(sidebar) {
  const brand = sidebar.getByRole("link", { name: /Центрально-Лесной заповедник/ });
  const title = brand.locator(".archive-sidebar-name");
  const logo = brand.locator("img");
  assert.equal(await title.textContent(), "Центрально-Лесной заповедник");
  assert.equal(await title.isVisible(), true);
  assert.ok(await logo.evaluate(async image => {
    await image.decode();
    return image.naturalWidth > 0;
  }));
  const [shell, group] = await Promise.all([sidebar.boundingBox(), brand.boundingBox()]);
  assert.ok(Math.abs(shell.x + shell.width / 2 - group.x - group.width / 2) < 2);
}

async function check(base) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, locale: "ru-RU" });
    await page.route("**/*", route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(base);
    const sidebar = page.locator("[data-archive-nav]");
    const nav = sidebar.getByRole("navigation", { name: "Основная навигация" });
    const toggle = sidebar.locator(".archive-sidebar-toggle");
    const resize = sidebar.getByRole("separator", { name: "Ширина боковой панели" });
    await checkBrand(sidebar);
    assert.deepEqual(await nav.getByRole("link").evaluateAll(links => links.map(link => [link.textContent, link.getAttribute("href")])),
      [["Наблюдения", "/"], ["Прогноз", "/forecast"], ["Файлы и импорт", "/files"]]);

    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "К наблюдениям");
    await page.keyboard.press("Enter");
    assert.equal(await page.evaluate(() => document.activeElement.id), "main");

    const before = await width(sidebar);
    const handle = await resize.boundingBox();
    const x = handle.x + handle.width / 2;
    const y = handle.y + handle.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 56, y, { steps: 5 });
    await page.mouse.up();
    assert.equal(await width(sidebar), before + 56);
    await page.reload();
    await resize.waitFor();
    assert.equal(await width(sidebar), before + 56);

    await resize.press("ArrowRight");
    assert.equal(await width(sidebar), before + 56 + 16);
    await resize.press("Home");
    assert.equal(await width(sidebar), Number(await resize.getAttribute("aria-valuemin")));
    await resize.press("End");
    assert.equal(await width(sidebar), Number(await resize.getAttribute("aria-valuemax")));
    await resize.press("ArrowLeft");
    const saved = await width(sidebar);
    await resize.press("Enter");
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    assert.equal(await width(sidebar), 66);
    assert.equal(await page.evaluate(() => document.activeElement.className), "archive-sidebar-toggle");
    assert.equal(await nav.getByRole("link", { name: "Прогноз" }).isVisible(), true);
    await toggle.press("Enter");
    assert.equal(await width(sidebar), saved);
    assert.equal(await page.locator("main").evaluate(main => getComputedStyle(main).marginLeft), `${saved}px`);
    await checkBrand(sidebar);

    await nav.getByRole("link", { name: "Прогноз" }).click();
    await page.waitForURL("**/forecast");
    assert.equal(await nav.getByRole("link", { name: "Прогноз" }).getAttribute("aria-current"), "page");
    assert.equal(await width(sidebar), saved);
    assert.equal(await page.locator('a[href="/journal"], a[href="/model"], a[href="/review"], a[href="/operator"]').count(), 0);

    await page.setViewportSize({ width: 760, height: 860 });
    await page.waitForFunction(() => document.querySelector(".archive-sidebar-resize").getAttribute("aria-valuenow") === "300");
    assert.equal(await width(sidebar), 300);
    await page.setViewportSize({ width: 390, height: 860 });
    await page.getByRole("button", { name: "Открыть меню", exact: true }).waitFor();
    await checkBrand(sidebar);
    assert.equal(await nav.isVisible(), false);
    assert.equal(await page.locator("main").evaluate(main => getComputedStyle(main).marginLeft), "0px");
    await toggle.click();
    assert.equal(await nav.isVisible(), true);
    await nav.getByRole("link", { name: "Файлы и импорт" }).click();
    await page.waitForURL("**/files");
    assert.equal(await toggle.getAttribute("aria-expanded"), "true");
    await nav.getByRole("link", { name: "Файлы и импорт" }).press("Escape");
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    await page.reload();
    await toggle.waitFor();
    assert.equal(await nav.isVisible(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);

    await page.setViewportSize({ width: 1280, height: 860 });
    await page.getByRole("button", { name: "Свернуть боковую панель", exact: true }).waitFor();
    assert.equal(await width(sidebar), saved);
    await toggle.click();
    await page.reload();
    await toggle.waitFor();
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    assert.equal(await width(sidebar), 66);
    console.log("PASS: общий shell, центрирование эмблемы, мышь, клавиатура и сохранение мобильного и настольного меню");
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
