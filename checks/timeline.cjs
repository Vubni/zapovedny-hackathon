const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const oneDay = 86400000;
const offsetDate = (value, days) => new Date(Date.parse(value) + days * oneDay).toISOString().slice(0, 10);
const datePair = (start, end) => `${start}:${end}`;

(async () => {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900},locale:"ru-RU"});
    const root = "http://127.0.0.1:" + (process.env.METEO_TEST_PORT || "8765");
    const errors = [];
    const report = {};
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(root + "/?stations=kr1&intervals=2024-02-28:2024-02-29,2024-04-01:2024-04-10");
    const timeline = page.locator("#archive-range");
    await timeline.locator("[data-interval]").selectOption("1");
    await timeline.locator("[data-zoom]").selectOption("7");
    assert.equal(await timeline.locator("#range-start").getAttribute("max"), "6");
    await timeline.locator('[data-jump="end"]').click();
    await timeline.getByRole("slider", {name:"Конец диапазона"}).press("ArrowLeft");
    assert.equal(await page.locator("#dash-end-2").inputValue(), "2024-04-09");
    assert.equal(await page.locator("#dash-start").inputValue(), "2024-02-28");
    assert.equal(await page.locator("#dash-end").inputValue(), "2024-02-29");
    await timeline.locator("[data-prev]").click();
    const tick = await timeline.locator(".timeline-ticks span").first().textContent();
    const [dd,mm,yyyy] = tick.split(".");
    const first = Date.parse(yyyy+"-"+mm+"-"+dd+"T00:00:00Z");
    const at = d => new Date(first+d*86400000).toISOString().slice(0,10);
    const box = await timeline.locator(".nav-track").boundingBox();
    const x = n => box.x + box.width*n/6;
    const y = box.y + box.height/2;
    await page.mouse.move(x(2),y); await page.mouse.down();
    await page.mouse.move(x(4),y,{steps:7}); await page.mouse.up();
    assert.equal(await page.locator("#dash-start-2").inputValue(),at(2));
    assert.equal(await page.locator("#dash-end-2").inputValue(),at(4));
    assert.equal(new URL(page.url()).searchParams.get("intervals"),"2024-02-28:2024-02-29,"+at(2)+":"+at(4));
    await page.setViewportSize({width:320,height:830});
    assert.ok(await timeline.evaluate(el=>el.scrollWidth <= el.clientWidth+2));
    await timeline.locator("[data-zoom]").selectOption("archive");
    await timeline.locator("[data-interval]").selectOption("0");
    assert.equal(await timeline.locator("[data-selection]").textContent(),"28.02.2024 — 29.02.2024");
    report.leapDayAndWeeklyDrag = { first: "2024-02-28:2024-02-29", resizedSecond: datePair(at(2), at(4)), width: 320 };

    const stationResponse = await page.request.get(`${root}/api/stations`);
    const { end: archiveEnd } = await stationResponse.json();
    const originalStart = offsetDate(archiveEnd, -2);
    const newStart = offsetDate(archiveEnd, -5);
    const newEnd = offsetDate(archiveEnd, -3);
    const expected = [datePair(newStart, newEnd), datePair(originalStart, archiveEnd)];
    await page.goto(`${root}/?stations=&start=${originalStart}&end=${archiveEnd}&charts=air`);
    const add = page.getByRole("button", { name: "Добавить интервал", exact: true });
    await add.focus();
    await add.press("Enter");
    assert.equal(await timeline.locator("[data-interval]").inputValue(), "0");
    assert.equal(await page.locator("#dash-start").inputValue(), newStart);
    assert.equal(await page.locator("#dash-end").inputValue(), newEnd);
    assert.equal(new URL(page.url()).searchParams.get("intervals"), expected.join(","));
    await timeline.getByRole("slider", { name: "Конец диапазона" }).press("ArrowRight");
    assert.equal(await page.locator("#dash-end").inputValue(), newEnd);
    await timeline.locator("[data-interval]").selectOption("1");
    await timeline.getByRole("slider", { name: "Начало диапазона" }).press("ArrowLeft");
    assert.equal(await page.locator("#dash-start-2").inputValue(), originalStart);
    report.adjacentAtArchiveEnd = { ranges: expected, activeOnAdd: 0, keyboardClampedAtBoundary: true };

    await page.reload();
    await timeline.locator("[data-interval]").waitFor();
    assert.equal(new URL(page.url()).searchParams.get("intervals"), expected.join(","));
    assert.equal(await page.locator("[data-date-interval]").count(), 2);
    const overlap = `${datePair(newStart, newEnd)},${datePair(newEnd, archiveEnd)}`;
    const invalid = await page.request.get(`${root}/api/observations`, { params: { intervals: overlap } });
    assert.equal(invalid.status(), 400);
    assert.match((await invalid.json()).error, /пересекаться/);
    await page.goto(`${root}/?stations=&intervals=${overlap}&charts=air`);
    await page.getByText("В ссылке указаны неверные или пересекающиеся интервалы. Показан сохранённый выбор.", { exact: true }).waitFor();
    assert.equal(await page.locator("#dash-end").inputValue(), newEnd);
    assert.equal(await page.locator("#dash-start-2").inputValue(), originalStart);
    report.overlap = { apiStatus: 400, preservedSavedRanges: expected };

    await page.getByRole("button", { name: "Весь период", exact: true }).click();
    await add.click();
    await page.getByText("Все даты архива уже выбраны. Сократите один из интервалов, чтобы добавить ещё один.", { exact: true }).waitFor();
    assert.equal(await page.locator("[data-date-interval]").count(), 1);
    assert.equal(new URL(page.url()).searchParams.has("intervals"), false);
    assert.deepEqual(errors, []);
    report.fullArchive = { addDidNotOverlap: true, browserErrors: errors.length };

    if (process.env.METEO_WRITE_TIMELINE_REPORT === "1") {
      const directory = path.join(__dirname, "..", "results", "observations");
      await fs.mkdir(directory, { recursive: true });
      await fs.writeFile(path.join(directory, "timeline-periods.json"), JSON.stringify(report, null, 2) + "\n");
    }
    console.log("PASS: активный интервал, неделя, переход к границе, рамка по дням, клавиатура у общей границы, соседние периоды, URL, занятый архив и 320 px.");
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
