const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const root = `http://127.0.0.1:${process.env.METEO_TEST_PORT || 8765}`;
const reportPath = path.join(__dirname, "..", "results", "observations", "chart-exports.json");
const sourceOrder = "pressure,air_range,soil_05,rh,air";
const cards = [
  { id: "air", title: "Средняя температура воздуха", name: "средняя-температура-воздуха", unit: "°C" },
  { id: "air_range", title: "Диапазон температуры воздуха", name: "диапазон-температуры-воздуха", unit: "°C" },
  { id: "soil_05", title: "Температура почвы · 5 см", name: "температура-почвы-5-см", empty: true },
  { id: "rh", title: "Средняя относительная влажность", name: "средняя-относительная-влажность", unit: "%" },
  { id: "pressure", title: "Среднее атмосферное давление", name: "среднее-атмосферное-давление", unit: "гПа" },
];
const formatNames = { svg: "SVG", png: "PNG", jpeg: "JPEG / JPG" };
const fullPeriod = "01.01.2025 – 31.12.2025";
const stationLabels = ["Кр1 · поляна", "Кр2 · поле"];
const svgDates = /^\d{2}\.\d{2}\.\d{4} – \d{2}\.\d{2}\.\d{4}$/;
const chart = (page, id) => page.locator(`[data-observation-chart="${id}"]`);
const archiveButton = (page) => page.getByRole("button", { name: "Скачать все графики", exact: true });
const archiveDialog = (page) => page.getByRole("dialog", { name: "Скачать все графики", exact: true });

// Python reads the saved browser downloads without relying on the web app's ZIP writer or SVG renderer.
const inspectFiles = String.raw`
from hashlib import sha256
from pathlib import Path
import json
import struct
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zlib import crc32

def inspect(data, suffix):
    result = {"sha256": sha256(data).hexdigest()}
    if suffix == ".svg":
        root = ET.fromstring(data)
        ns = "{http://www.w3.org/2000/svg}"
        assert root.tag == ns + "svg"
        result["title"] = root.findtext(ns + "title")
        result["text"] = [node.text for node in root.iter(ns + "text")]
        result["plot_paths"] = sum(1 for node in root.iter(ns + "path") if node.get("d"))
    elif suffix == ".png":
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
        result["dimensions"] = struct.unpack(">II", data[16:24])
        assert min(result["dimensions"]) > 0
    elif suffix == ".jpeg":
        assert data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    else:
        raise AssertionError(f"Unexpected file format: {suffix}")
    return result

filename, kind = sys.argv[1:]
if kind == "svg":
    output = inspect(Path(filename).read_bytes(), ".svg")
else:
    with ZipFile(filename) as archive:
        assert archive.testzip() is None
        output = []
        for info in archive.infolist():
            contents = archive.read(info)
            assert info.flag_bits & 0x800 and info.file_size == len(contents)
            assert crc32(contents) == info.CRC
            output.append({
                "name": info.filename, "crc32": f"{info.CRC:08x}",
                **inspect(contents, Path(info.filename).suffix)
            })
print(json.dumps(output, ensure_ascii=False))
`;

async function inspect(download, kind) {
  assert.equal(await download.failure(), null);
  const file = await download.path();
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", inspectFiles, file, kind], {
    encoding: "utf8",
  }));
}

async function assertOrder(page, expected) {
  const layout = await page.locator("[data-observation-chart]").evaluateAll(nodes => nodes.map(node => {
    const { top, left, right, width } = node.getBoundingClientRect();
    return { id: node.dataset.observationChart, top, left, right, width };
  }));
  const ids = expected.map(card => card.id);
  assert.deepEqual(layout.map(card => card.id), ids, "Порядок карточек в DOM");
  const visual = [...layout].sort((a, b) => Math.abs(a.top - b.top) < 1 ? a.left - b.left : a.top - b.top);
  assert.deepEqual(visual.map(card => card.id), ids, "Порядок карточек по расположению на экране");
  assert.ok(layout.every(card => card.width > 0), "У всех карточек есть видимый контейнер");
  return layout;
}

async function ready(page) {
  await page.waitForFunction(() => document.querySelector("#download-all-charts")?.disabled === false,
    null, { timeout: 25000 });
}

async function openArchiveDialog(page, count) {
  await ready(page);
  await archiveButton(page).click();
  const dialog = archiveDialog(page);
  assert.match(await dialog.locator("#chart-export-count").innerText(), new RegExp(`архиве: ${count}\\.`));
  return dialog;
}

async function bulkDownload(page, format, expected) {
  const dialog = await openArchiveDialog(page, expected.length);
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 25000 }),
    dialog.getByRole("button", { name: formatNames[format], exact: true }).click(),
  ]);
  assert.equal(download.suggestedFilename(), `Метеоархив_2025-01-01_2025-12-31_${format}.zip`);
  const entries = await inspect(download, "zip");
  assert.deepEqual(entries.map(item => item.name), expected.map((card, index) =>
    `${String(index + 1).padStart(2, "0")}-${card.name}.${format}`));
  return entries;
}

function assertSvg(entries, expected) {
  for (const [index, card] of expected.entries()) {
    const entry = entries[index];
    assert.equal(entry.title, card.title);
    assert.ok(entry.text.includes(card.title), card.id);
    if (card.empty) {
      assert.ok(entry.text.includes("Нет данных за этот период"));
      assert.equal(entry.plot_paths, 0);
    } else {
      assert.ok(entry.text.includes(card.unit), card.id);
      assert.ok(entry.plot_paths > 0, card.id);
    }
  }
}

async function assertMobile(page, width) {
  await page.setViewportSize({ width, height: 844 });
  const layout = await assertOrder(page, cards);
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  assert.equal(scrollWidth, width);
  assert.ok(layout.every(card => card.left >= 0 && card.right <= width + 1), `Карточки в пределах ${width} px`);
  const dialog = await openArchiveDialog(page, cards.length);
  const box = await dialog.boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= width + 1, `Диалог ZIP в пределах ${width} px`);
  assert.equal(await dialog.evaluate(node => node.scrollWidth), await dialog.evaluate(node => node.clientWidth));
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "hidden" });
  return { width, scrollWidth, archive_dialog_width: box.width };
}

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.METEO_CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, locale: "ru-RU" });
  page.setDefaultTimeout(10000);
  const pageErrors = [];
  const mutations = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("request", request => {
    if (new URL(request.url()).origin === root && !["GET", "HEAD"].includes(request.method())) {
      mutations.push(`${request.method()} ${request.url()}`);
    }
  });

  try {
    await page.goto(`${root}/?stations=kr1,kr2&start=2025-01-01&end=2025-12-31&grain=month&charts=${sourceOrder}`);
    await ready(page);
    const initialOrder = (await assertOrder(page, cards)).map(card => card.id);
    assert.deepEqual(await page.locator(".observation-group-title:visible").allTextContents(), [
      "Температура воздуха", "Температура почвы", "Влажность", "Атмосферное давление",
    ]);
    await chart(page, "soil_05").getByText("Нет данных за этот период", { exact: true }).waitFor();

    const svg = await bulkDownload(page, "svg", cards);
    assertSvg(svg, cards);
    for (const entry of svg) assert.ok(entry.text.includes(fullPeriod), entry.name);
    for (const label of stationLabels) assert.ok(svg[0].text.includes(label));
    const png = await bulkDownload(page, "png", cards);
    const jpeg = await bulkDownload(page, "jpeg", cards);
    const mobile = [];
    for (const width of [390, 320]) mobile.push(await assertMobile(page, width));
    await page.setViewportSize({ width: 1440, height: 960 });

    const air = chart(page, "air");
    const kr2 = air.getByRole("button", { name: stationLabels[1], exact: true });
    await kr2.click();
    assert.equal(await kr2.getAttribute("aria-pressed"), "false");
    await air.getByRole("button", { name: "Увеличить масштаб" }).click();
    assert.equal(await page.getByLabel("Дата с", { exact: true }).inputValue(), "2025-01-01");
    assert.equal(await page.getByLabel("Дата по", { exact: true }).inputValue(), "2025-12-31");

    await chart(page, "rh").locator('[data-chart-action="hide"]').click();
    const remaining = cards.filter(card => card.id !== "rh");
    await assertOrder(page, remaining);
    await air.locator('[data-chart-action="download"]').click();
    const [individualDownload] = await Promise.all([
      page.waitForEvent("download"),
      air.locator('[data-chart-format="svg"]').click(),
    ]);
    assert.equal(individualDownload.suggestedFilename(), `${cards[0].name}.svg`);
    const individual = await inspect(individualDownload, "svg");
    const updated = await bulkDownload(page, "svg", remaining);
    assertSvg(updated, remaining);
    assert.equal(updated[0].sha256, individual.sha256, "Одиночная выгрузка и ZIP передают одинаковый SVG");
    assert.notEqual(updated[0].sha256, svg[0].sha256);
    assert.ok(updated[0].text.includes(stationLabels[0]), "Легенда содержит Кр1");
    assert.ok(!updated[0].text.includes(stationLabels[1]), "Легенда исключает скрытый ряд Кр2");
    const zoomedPeriod = updated[0].text.find(text => svgDates.test(text));
    assert.ok(zoomedPeriod && zoomedPeriod !== fullPeriod, "Подпись периода учитывает масштаб");
    assert.ok(updated[1].text.includes(fullPeriod), "Другие карточки сохраняют исходный масштаб");

    await page.reload();
    await ready(page);
    const reloadedOrder = (await assertOrder(page, remaining)).map(card => card.id);
    assert.equal(new URL(page.url()).searchParams.get("charts"), reloadedOrder.join(","));
    await page.getByRole("button", { name: "Очистить выбор" }).click();
    await air.getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    assert.equal(await archiveButton(page).isDisabled(), true);
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(mutations, []);

    const report = {
      command: "node checks/chart-exports.cjs",
      stations: ["kr1", "kr2"],
      input_order: sourceOrder.split(","),
      visual_and_dom_order: initialOrder,
      zip: {
        svg: svg.map(({ name, crc32 }) => ({ name, crc32 })),
        png_files: png.length,
        jpeg_files: jpeg.length,
        all_entries_passed_zipfile_and_crc32: true,
        empty_soil: { title: svg[2].title, message: "Нет данных за этот период" },
      },
      after_hide_and_zoom: {
        files: updated.length,
        hidden_card: "rh",
        visible_air_legend: [stationLabels[0]],
        period: zoomedPeriod,
        individual_and_bulk_svg_sha256: updated[0].sha256,
        order_on_reload: reloadedOrder,
      },
      mobile,
      download_disabled_without_stations: true,
      mutating_http_requests: mutations.length,
    };
    await fs.mkdir(path.dirname(reportPath), { recursive: true });
    await fs.writeFile(reportPath, JSON.stringify(report, null, 2) + "\n");
    console.log("PASS: порядок карточек, SVG/PNG/JPEG ZIP, пустой график, масштаб и легенда, 390 и 320 px");
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
