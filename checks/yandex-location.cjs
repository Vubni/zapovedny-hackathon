const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

const yandex = "https://yandex.ru";
const widgetPath = "/map-widget/v1/";
const frameSelector = 'iframe[title="Расположение на Яндекс Картах"]';
const points = [
  { id: "test-north", label: "Контрольный северный пост", latitude: 56.57525, longitude: 32.91234 },
  { id: "test-south", label: "Контрольный южный пост", latitude: 56.43567, longitude: 32.78246 },
];
const coincidentPoint = { ...points[0], id: "test-colocated", label: "Контрольный пост с общей точкой" };
const chukotka = { id: "test-chukotka", label: "Контрольная точка Чукотки", latitude: 65.68, longitude: -170.54 };
const noCoordinates = { id: "test-no-gps", label: "Контрольная без координат" };
const area = {longitude: 32.87, latitude: 56.52};
const testPin = ({longitude, latitude}) => `${longitude},${latitude}`;
const expectedPins = [area, ...points, chukotka].map(testPin).sort();

function assertWidget(source, point) {
  const url = new URL(source);
  assert.equal(url.origin, yandex);
  assert.equal(url.pathname, widgetPath);
  const center = url.searchParams.get("ll")?.split(",").map(Number);
  assert.ok(center?.length === 2 && center.every(Number.isFinite), "Координаты ll");
  const zoom = Number(url.searchParams.get("z"));
  assert.ok(zoom >= 1 && zoom <= 21, "Масштаб z");
  assert.ok([...url.searchParams.keys()].every(key => !/key/i.test(key)), "Виджет открывается без ключа");
  assert.equal(url.searchParams.get("l"), "map", "Подложка без слоя пробок");
  const pins = url.searchParams.get("pt")?.split("~").sort();
  assert.deepEqual(pins, expectedPins, "В виджет переданы все отдельные координаты и одна точка района");
  if (point) {
    const expected = [point.longitude, point.latitude];
    assert.deepEqual(center, expected, "Центр после действия «Показать на карте»");
  }
  return { center, zoom, pins };
}

function checkbox(root, label) {
  return root.locator(".station-picker-row").filter({ hasText: label }).getByRole("checkbox");
}

function locate(root, label) {
  return root.getByRole("button", { name: `Показать на карте: ${label}`, exact: true });
}

async function selectByFrame(page, overview, point) {
  const mode = overview.getByRole("button", { name: "Выделение рамкой", exact: true });
  await mode.click();
  await locate(overview, point.label).click();
  const pin = overview.getByRole("button", { name: point.label, exact: true });
  const viewport = overview.locator(".station-map-viewport");
  await viewport.scrollIntoViewIfNeeded();
  await pin.waitFor();
  await page.waitForFunction(({ title }) => {
    const map = document.querySelector("#station-overview .station-map-viewport")?.getBoundingClientRect();
    const station = [...document.querySelectorAll("#station-overview .station-map-marker")]
      .find(marker => marker.getAttribute("aria-label") === title)?.getBoundingClientRect();
    return map && station && Math.abs(station.x + station.width / 2 - map.x - map.width / 2) < 3;
  }, { title: point.label });
  const { x, y, width, height } = await pin.boundingBox();
  const middleX = x + width / 2;
  const middleY = y + height / 2;
  await page.mouse.move(middleX - 24, middleY - 24);
  await page.mouse.down();
  await page.mouse.move(middleX + 24, middleY + 24, { steps: 8 });
  await overview.locator(".station-map-frame").waitFor();
  await page.mouse.up();
  assert.ok(await checkbox(overview, point.label).isChecked());
  assert.ok(new URL(page.url()).searchParams.get("stations").split(",").includes(point.id));
}

async function dialogLayout(page, overview, width, initial, choose) {
  await page.setViewportSize({ width, height: 840 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
  await overview.locator("[data-expand-map]").click();
  const dialog = page.getByRole("dialog", { name: "Метеостанции", exact: true });
  await dialog.waitFor();
  assert.ok(await dialog.evaluate(element => element.matches(":modal")));
  const mapMode = dialog.locator('[data-provider="local"]');
  if (await mapMode.count()) await mapMode.click();
  await dialog.getByRole("button", { name: "Выделение рамкой", exact: true }).waitFor();
  const box = await dialog.boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
  assert.ok(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1));
  assert.ok(await checkbox(dialog, initial.label).isChecked());
  const apply = dialog.getByRole("button", { name: "Применить", exact: true });
  const footer = await apply.boundingBox();
  assert.ok(footer.y + footer.height <= 840);
  if (choose) {
    await checkbox(dialog, choose.label).check();
    await Promise.all([
      page.waitForURL(url => url.searchParams.get("stations")?.split(",").includes(choose.id)),
      page.waitForResponse(response => new URL(response.url()).pathname === "/api/stations"),
      apply.click(),
    ]);
    assert.ok(await checkbox(overview, choose.label).isChecked());
  } else {
    await page.keyboard.press("Escape");
  }
  await dialog.waitFor({ state: "hidden" });
  return { width, dialog_width: Math.round(box.width), fits_horizontally: true };
}

async function check(base) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 840 }, locale: "ru-RU" });
    page.setDefaultTimeout(10000);
    const blocked = [];
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        const spacer = document.createElement("div");
        spacer.id = "widget-lazy-test-spacer";
        spacer.style.height = "200vh";
        document.querySelector("#station-overview")?.before(spacer);
      }, { once: true });
    });
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin === base) return route.continue();
      blocked.push(url.href);
      return route.abort();
    });
    await page.goto(`${base}/?stations=kr1&start=2025-07-01&end=2025-07-31&grain=day&charts=air`);
    const overview = page.locator("#station-overview");
    const provider = overview.locator(".station-provider");
    const frame = overview.locator(frameSelector);
    const yandexButton = provider.getByRole("button", { name: "Яндекс Карты", exact: true });
    const localButton = provider.getByRole("button", { name: "Схема станций", exact: true });
    await yandexButton.waitFor({ state: "attached" });
    assert.equal(await localButton.getAttribute("aria-pressed"), "true");
    assert.equal(await frame.count(), 0);
    assert.equal(blocked.length, 0);
    await overview.scrollIntoViewIfNeeded();
    await overview.locator(".station-map-viewport .station-map-marker-area").waitFor();
    assert.equal(await frame.count(), 0);
    assert.equal(blocked.length, 0);
    const firstRequest = page.waitForRequest(request => {
      const url = new URL(request.url());
      return url.origin === yandex && url.pathname === widgetPath;
    });
    await yandexButton.click();
    await frame.waitFor();
    assert.equal(await yandexButton.getAttribute("aria-pressed"), "true");
    assert.equal(await frame.getAttribute("loading"), "lazy");
    assertWidget((await firstRequest).url());
    const initial = assertWidget(await frame.getAttribute("src"));
    assert.ok(initial.center[0] > 95 && initial.center[0] < 115, "Широкий каталог центрирован между заповедником и Чукоткой");
    const labels = overview.getByRole("group", {name: "Метки на Яндекс Картах"});
    const areaLabel = labels.getByRole("button", {name: /^Район заповедника · 3 станции/});
    const groupName = await areaLabel.getAttribute("aria-label");
    assert.match(groupName, /Кр1.*Кр2|Кр2.*Кр1/);
    assert.match(groupName, /АМЕ-60/);
    assert.equal(await labels.getByRole("button", {name: `${points[0].label}, ${coincidentPoint.label}`}).count(), 1);
    assert.equal(await labels.getByRole("button", {name: noCoordinates.label}).count(), 0);
    await page.locator("#widget-lazy-test-spacer").evaluate(element => element.remove());

    const located = [];
    for (const point of points) {
      await locate(overview, point.label).click();
      located.push(assertWidget(await frame.getAttribute("src"), point));
      const link = overview.getByRole("link", { name: /^Открыть в Яндекс Картах/ });
      const target = new URL(await link.getAttribute("href"));
      assert.equal(target.origin, yandex);
      assert.equal(target.pathname, "/maps/");
      assert.deepEqual(target.searchParams.get("pt").split("~").sort(), located.at(-1).pins);
    }
    assert.notDeepEqual(located[0].center, located[1].center);
    await areaLabel.click();
    assertWidget(await frame.getAttribute("src"), area);
    await labels.getByRole("button", {name: "Все метки"}).click();
    assert.deepEqual(assertWidget(await frame.getAttribute("src")).center, initial.center);
    assert.equal(await locate(overview, noCoordinates.label).count(), 0);
    await checkbox(overview, points[0].label).check();
    assert.ok(new URL(page.url()).searchParams.get("stations").split(",").includes(points[0].id));

    await localButton.click();
    assert.equal(await localButton.getAttribute("aria-pressed"), "true");
    await frame.waitFor({ state: "hidden" });
    await overview.locator(".station-map-viewport canvas").waitFor();
    await selectByFrame(page, overview, points[1]);
    assert.equal(await checkbox(overview, points[0].label).isChecked(), false);

    const layouts = [
      await dialogLayout(page, overview, 1280, points[1], noCoordinates),
      await dialogLayout(page, overview, 320, noCoordinates),
    ];
    await overview.getByRole("button", {name: "Вся Россия"}).click();
    const eastMarker = overview.getByRole("button", {name: chukotka.label, exact: true});
    await eastMarker.waitFor();
    const eastBox = await eastMarker.boundingBox();
    const westBox = await overview.locator(".station-map-marker-area").boundingBox();
    assert.ok(eastBox.x > westBox.x, "Чукотка за 180° показана восточнее заповедника");
    await overview.getByRole("button", {name: "К станциям"}).click();
    await eastMarker.waitFor();
    assert.deepEqual(errors, []);
    assert.ok(blocked.some(url => url.startsWith(`${yandex}${widgetPath}`)));
    return {
      command: "node checks/yandex-location.cjs",
      database: "Temporary METEO_OBSERVATION_DB",
      iframe: { origin: yandex, path: widgetPath, loading: "lazy", requests_before_explicit_choice: 0, initial, located },
      provider_request_blocked: true,
      local_rectangle_selection: true,
      selection_without_coordinates_in_dialog: true,
      russia_view_crosses_antimeridian: true,
      layouts,
    };
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check, { stations: [...points, coincidentPoint, chukotka, noCoordinates] }).then(async report => {
  const destination = path.join(__dirname, "..", "results", "observations", "yandex-location.json");
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(destination, JSON.stringify(report, null, 2) + "\n");
  console.log("PASS: схема в первом кадре, все метки Яндекса, группировка, рамка, обзор России и диалог 1280/320 px");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
