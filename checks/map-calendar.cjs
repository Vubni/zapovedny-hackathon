const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

const root = path.join(__dirname, "..");
const screenshotDir = process.env.METEO_MAP_CALENDAR_SCREENSHOTS;
const days = ["2024-01-05", "2024-07-05", "2025-07-05"];
const cadastralPoints = {
  kr1: { latitude: 56.464310, longitude: 32.970290, sample: "MYX12013", extracted_line: 4870 },
  kr2: { latitude: 56.463800, longitude: 32.970370, sample: "MYX12012", extracted_line: 5043 },
};

async function sourceUrl() {
  const documentation = await fs.readFile(path.join(root, "docs", "КАРТА.md"), "utf8");
  const found = documentation.match(/\[Кадастровые сведения[^\]]*\]\(([^)]+\.pdf)\)/);
  assert.ok(found, "Документация ссылается на первоисточник");
  const source = new URL(found[1]);
  assert.match(source.hostname, /^(www\.)?clgz\.ru$/);
  return source.href;
}

async function capture(locator, name) {
  if (!screenshotDir) return;
  await fs.mkdir(screenshotDir, { recursive: true });
  await locator.screenshot({ path: path.join(screenshotDir, name) });
}

function distanceMetres(first, second) {
  const radians = degree => degree * Math.PI / 180;
  const dLat = radians(second.latitude - first.latitude);
  const dLon = radians(second.longitude - first.longitude);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(radians(first.latitude)) *
    Math.cos(radians(second.latitude)) * Math.sin(dLon / 2) ** 2;
  return Math.round(12_742_000 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)) * 10) / 10;
}

async function checkMap(page, base) {
  const fetched = await page.request.get(`${base}/api/stations`);
  assert.equal(fetched.status(), 200);
  const { stations } = await fetched.json();
  for (const [id, coordinates] of Object.entries(cadastralPoints)) {
    const item = stations.find(station => station.id === id);
    assert.ok(item);
    assert.equal(item.latitude, coordinates.latitude);
    assert.equal(item.longitude, coordinates.longitude);
    assert.equal(item.location_precision, "area");
    assert.match(item.location_reference, /26\.07\.2019/);
    assert.match(item.location_reference, /Координаты прибора не подтверждены/);
  }
  await page.goto(`${base}/?stations=&start=2025-07-05&end=2025-07-05&grain=day&charts=air`);
  const map = page.locator("#station-overview");
  const mark = id => map.locator(`.station-map-marker[data-station-id="${id}"]`);
  for (const id of ["kr1", "kr2", "ame60"]) {
    await mark(id).waitFor();
    assert.equal(await mark(id).getAttribute("aria-pressed"), "false");
  }
  const pins = await map.locator(".station-map-marker:visible").evaluateAll(elements =>
    Object.fromEntries(elements.map(element => {
      const bounds = element.getBoundingClientRect();
      return [element.dataset.stationId, { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 }];
    })));
  assert.notDeepEqual(pins.kr1, pins.kr2);
  assert.notDeepEqual(pins.ame60, pins.kr2);
  const [response] = await Promise.all([
    page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === "/api/observations" && url.searchParams.get("stations") === "kr2";
    }),
    mark("kr2").click(),
  ]);
  assert.equal(response.status(), 200);
  assert.deepEqual((await response.json()).series.map(series => series.station), ["kr2"]);
  assert.equal(new URL(page.url()).searchParams.get("stations"), "kr2");
  for (const id of ["kr1", "kr2", "ame60"]) {
    assert.equal(await mark(id).getAttribute("aria-pressed"), String(id === "kr2"));
  }
  const checked = await map.locator("[data-list] input:checked").count();
  assert.equal(checked, 1);
  assert.deepEqual(await page.locator("#selected-stations li span").allTextContents(), ["Кр2 · поле"]);
  await capture(map, "krasnoe-kr2-only.png");
  return {
    source: {
      publication: "Кадастровые сведения о Центрально-Лесном биосферном заповеднике за 2017–2020 гг.",
      url: await sourceUrl(),
      description: "Точки биологических сборов 26.07.2019, использованные как ориентиры",
      station_coordinates_confirmed: false,
      points: cadastralPoints,
    },
    distance_metres: distanceMetres(cadastralPoints.kr1, cadastralPoints.kr2),
    marker_centres_css_px: pins,
    selected_station_ids: ["kr2"],
    checked_station_rows: checked,
  };
}

async function probe(svg, tooltip, key) {
  await svg.locator(".obs-chart__target").press(key);
  await tooltip.waitFor();
  const circles = await svg.locator('g[visibility="visible"] circle').evaluateAll(nodes =>
    nodes.map(node => {
      const box = node.getBoundingClientRect();
      return {
        svg_x: Number(node.getAttribute("cx")),
        css_x: Math.round((box.x + box.width / 2) * 10) / 10,
      };
    }));
  return {
    heading: await tooltip.locator("strong").first().textContent(),
    source_dates: await tooltip.locator(".obs-chart__tooltip-source-date").allTextContents(),
    circles,
  };
}

async function checkCalendar(page, base) {
  const query = new URLSearchParams({
    stations: "kr2", start: days[0], end: days.at(-1),
    intervals: days.map(day => `${day}:${day}`).join(","),
    charts: "air", grain: "day", calendar: "0",
  });
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === "/api/observations"),
    page.goto(`${base}/?${query}`),
  ]);
  assert.equal(response.status(), 200);
  const apiPoints = (await response.json()).series.flatMap(series => series.points);
  assert.deepEqual(apiPoints.map(point => point.x), days);
  assert.ok(apiPoints.every(point => Number.isFinite(point.y)));
  const card = page.locator('[data-observation-chart="air"]');
  const tooltip = card.locator(".obs-chart__tooltip");
  const periodAxis = card.locator('.obs-chart__plot svg[data-axis="time"]');
  await periodAxis.waitFor();
  const periodAxisPoints = [];
  for (const key of ["Home", "ArrowRight", "ArrowRight"]) {
    periodAxisPoints.push(await probe(periodAxis, tooltip, key));
  }
  assert.deepEqual(periodAxisPoints.map(point => point.heading), ["05.01.2024", "05.07.2024", "05.07.2025"]);
  assert.ok(periodAxisPoints.every(point => point.circles.length === 1));
  assert.equal(new Set(periodAxisPoints.map(point => point.circles[0].svg_x)).size, 3);

  const toggle = page.getByRole("checkbox", { name: "Считать от одной точки отсчёта" });
  await toggle.check();
  const calendar = card.locator('.obs-chart__plot svg[data-axis="calendar"]');
  await calendar.getByText("День и месяц", { exact: true }).waitFor();
  const january = await probe(calendar, tooltip, "Home");
  const july = await probe(calendar, tooltip, "ArrowRight");
  assert.equal(january.heading, "5 января");
  assert.equal(july.heading, "5 июля");
  assert.deepEqual(january.source_dates, ["05.01.2024"]);
  assert.deepEqual([...july.source_dates].sort(), ["05.07.2024", "05.07.2025"]);
  assert.equal(january.circles.length, 1);
  assert.equal(july.circles.length, 2);
  assert.deepEqual(july.circles[0], july.circles[1], "Оба 5 июля рисуются на одной x-координате");
  assert.ok(january.circles[0].css_x < july.circles[0].css_x, "Январь и июль на разных x-координатах");
  const plottedX = await calendar.locator('path[stroke-width="1.4"][fill="#ffffff"]').evaluateAll(paths =>
    paths.flatMap(node => [...node.getAttribute("d").matchAll(/M(-?\d+(?:\.\d+)?) /g)]
      .map(match => Math.round((Number(match[1]) + 2.6) * 10) / 10))
      .sort((first, second) => first - second));
  assert.equal(plottedX.length, 3);
  assert.ok(plottedX[0] < plottedX[1] && plottedX[1] === plottedX[2], "Совпали координаты нарисованных точек");
  await capture(card, "calendar-axis-5-july.png");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    card.getByRole("button", { name: "Скачать таблицу", exact: true }).click(),
  ]);
  const csv = (await fs.readFile(await download.path(), "utf8")).replace(/^\uFEFF/, "").trimEnd();
  const [header, ...lines] = csv.split(/\r?\n/).map(line => line.split(";"));
  assert.equal(header.at(-1), "День и месяц на общей оси");
  assert.deepEqual(lines.map(row => [row[0], row.at(-1)]), [
    ["2024-01-05", "05.01"], ["2024-07-05", "05.07"], ["2025-07-05", "05.07"],
  ]);
  await page.reload();
  await calendar.waitFor();
  assert.equal(await toggle.isChecked(), true);
  assert.equal(new URL(page.url()).searchParams.get("calendar"), "1");
  const inputs = await page.locator("#observation-date-rows input[type=date]").evaluateAll(elements =>
    elements.map(element => element.value));
  assert.deepEqual(inputs, days.flatMap(day => [day, day]));
  return {
    api_original_dates: apiPoints.map(point => point.x),
    chronological_css_x: periodAxisPoints.map(point => point.circles[0].css_x),
    shared_axis: { january, july, plotted_x_svg: plottedX },
    csv_original_date_and_calendar_date: lines.map(row => [row[0], row.at(-1)]),
    mode_and_original_ranges_persisted_after_reload: true,
  };
}

async function check(base) {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 920 }, locale: "ru-RU" });
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const map = await checkMap(page, base);
    const calendar = await checkCalendar(page, base);
    assert.deepEqual(errors, []);
    return { command: "node checks/map-calendar.cjs", browser: browser.version(), map, calendar };
  } finally {
    await browser.close();
  }
}

withIsolatedMeteo(check).then(async report => {
  const destination = path.join(root, "results", "observations", "map-calendar-check.json");
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(destination, JSON.stringify(report, null, 2) + "\n");
  console.log("PASS: отдельный выбор Кр2, общая календарная ось и исходные даты");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
