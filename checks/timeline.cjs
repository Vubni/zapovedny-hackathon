const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({executablePath: process.env.METEO_CHROMIUM});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900},locale:"ru-RU"});
    const root = "http://127.0.0.1:" + (process.env.METEO_TEST_PORT || "8765");
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
    console.log("PASS: активный интервал, неделя, переход к границе, шаг в сутки, рамка по дням, прежние интервалы и экран 320 px.");
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
