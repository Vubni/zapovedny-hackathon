const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const root = `http://127.0.0.1:${process.env.METEO_TEST_PORT || 8765}`;
const reportPath = path.join(__dirname, "..", "results", "observations", "chart-exports.json");
const sourceOrder = "pressure,wind_freq,air_range,temp_diurnal,soil_05,wind_rose,rh,air";
const cards = [
  { id: "air", title: "Средняя температура воздуха", name: "средняя-температура-воздуха", unit: "°C" },
  { id: "air_range", title: "Диапазон температуры воздуха", name: "диапазон-температуры-воздуха", unit: "°C" },
  { id: "temp_diurnal", title: "Средний суточный ход температуры", name: "средний-суточный-ход-температуры", unit: "°C" },
  { id: "soil_05", title: "Температура почвы · 5 см", name: "температура-почвы-5-см", empty: true },
  { id: "rh", title: "Средняя относительная влажность", name: "средняя-относительная-влажность", unit: "%" },
  { id: "wind_rose", title: "Роза ветров", name: "роза-ветров", unit: "%" },
  { id: "wind_freq", title: "Повторяемость направлений ветра", name: "повторяемость-направлений-ветра", unit: "%" },
  { id: "pressure", title: "Среднее атмосферное давление", name: "среднее-атмосферное-давление", unit: "гПа" },
];
const pdfName = "Метеоархив — все графики.pdf";
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
import re
import struct
import subprocess
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zlib import crc32

def inspect(data, suffix, render=None):
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
    elif suffix == ".pdf":
        assert data.startswith(b"%PDF-1.4") and data.endswith(b"%%EOF\n")
        result["page_count"] = len(re.findall(rb"/Type /Page\b", data))
        result["bookmarks"] = [bytes.fromhex(title.decode("ascii")).decode("utf-16")
                               for title in re.findall(rb"/Title <([0-9A-Fa-f]+)> /Parent", data)]
        assert result["page_count"] >= len(result["bookmarks"]) > 0
        if render:
            output = Path(render)
            output.parent.mkdir(parents=True, exist_ok=True)
            pdf = output.with_suffix(".pdf")
            pdf.write_bytes(data)
            info = subprocess.check_output(["pdfinfo", str(pdf)], text=True)
            assert int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1]) == result["page_count"]
            assert re.search(r"^Page size:\s+841\.89 x 595\.28 pts", info, re.M)
            subprocess.run(["pdftoppm", "-r", "110", "-png", str(pdf), str(output)], check=True)
            pages = sorted(output.parent.glob(output.name + "-*.png"))
            assert len(pages) == result["page_count"]
            for page in pages:
                contents = page.read_bytes()
                assert contents.startswith(b"\x89PNG\r\n\x1a\n") and len(contents) > 5000
            result["rendered_pages"] = len(pages)
    else:
        raise AssertionError(f"Unexpected file format: {suffix}")
    return result

filename, kind, *render = sys.argv[1:]
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
                **inspect(contents, Path(info.filename).suffix, render[0] if render else None)
            })
print(json.dumps(output, ensure_ascii=False))
`;

async function inspect(download, kind, render) {
  assert.equal(await download.failure(), null);
  const file = await download.path();
  const renderPath = render && process.env.METEO_TEST_PDF_RENDER_DIR
    ? [path.join(process.env.METEO_TEST_PDF_RENDER_DIR, render)] : [];
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", inspectFiles, file, kind, ...renderPath], {
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
  const description = await dialog.locator("#chart-export-count").innerText();
  assert.match(description, new RegExp(`графики \\(${count}\\)`));
  assert.match(description, /общий PDF/);
  return dialog;
}

async function bulkDownload(page, format, expected, { part = "2025-01-01_2025-12-31", multi = false, render } = {}) {
  const dialog = await openArchiveDialog(page, expected.length);
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 25000 }),
    dialog.getByRole("button", { name: formatNames[format], exact: true }).click(),
  ]);
  assert.equal(download.suggestedFilename(), `Метеоархив_${part}_${format}.zip`);
  const entries = await inspect(download, "zip", render);
  const filenames = expected.map((card, index) => `${String(index + 1).padStart(2, "0")}-${card.name}${multi ? `_${part}` : ""}.${format}`);
  assert.deepEqual(entries.map(item => item.name), [...filenames, pdfName]);
  const pdf = entries.at(-1);
  assert.deepEqual(pdf.bookmarks, expected.map(card => card.title));
  assert.equal(pdf.page_count >= expected.length, true);
  return { images: entries.slice(0, -1), pdf };
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
      "Температура воздуха", "Температура почвы", "Влажность", "Направление ветра", "Атмосферное давление",
    ]);
    await chart(page, "soil_05").getByText("Нет данных за этот период", { exact: true }).waitFor();

    await page.context().setOffline(true);
    const { images: svg, pdf } = await bulkDownload(page, "svg", cards, { render: "overview" });
    await page.context().setOffline(false);
    assertSvg(svg, cards);
    assert.equal(pdf.page_count, cards.length);
    for (const entry of svg) assert.ok(entry.text.includes(fullPeriod), entry.name);
    for (const label of stationLabels) assert.ok(svg[0].text.includes(label));
    const { images: png, pdf: pngPdf } = await bulkDownload(page, "png", cards);
    const { images: jpeg, pdf: jpegPdf } = await bulkDownload(page, "jpeg", cards);
    assert.equal(pngPdf.sha256, pdf.sha256);
    assert.equal(jpegPdf.sha256, pdf.sha256);
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
    const { images: updated, pdf: updatedPdf } = await bulkDownload(page, "svg", remaining, { render: "after-hide-and-zoom" });
    assertSvg(updated, remaining);
    assert.equal(updated[0].sha256, individual.sha256, "Одиночная выгрузка и ZIP передают одинаковый SVG");
    assert.notEqual(updated[0].sha256, svg[0].sha256);
    assert.ok(updated[0].text.includes(stationLabels[0]), "Легенда содержит Кр1");
    assert.ok(!updated[0].text.includes(stationLabels[1]), "Легенда исключает скрытый ряд Кр2");
    const zoomedPeriod = updated[0].text.find(text => svgDates.test(text));
    assert.ok(zoomedPeriod && zoomedPeriod !== fullPeriod, "Подпись периода учитывает масштаб");
    assert.ok(updated[1].text.includes(fullPeriod), "Другие карточки сохраняют исходный масштаб");
    assert.notEqual(updatedPdf.sha256, pdf.sha256);

    await page.reload();
    await ready(page);
    const reloadedOrder = (await assertOrder(page, remaining)).map(card => card.id);
    assert.equal(new URL(page.url()).searchParams.get("charts"), reloadedOrder.join(","));
    await page.getByRole("button", { name: "Очистить выбор" }).click();
    await air.getByText("Выберите хотя бы одну станцию", { exact: true }).waitFor();
    assert.equal(await archiveButton(page).isDisabled(), true);

    const windows = ["01", "03", "05", "07"].map((month, index) => [`2025-${month}-01`, `2025-${month}-${index === 2 ? "30" : "31"}`]);
    const query = new URLSearchParams({ stations: "kr1,kr2", intervals: windows.map(window => window.join(":")).join(","),
      grain: "month", charts: "wind_rose", mode: "togu" });
    const part = windows.map(window => window.join("_")).join("__");
    const intervalPage = await browser.newPage({ viewport: { width: 1440, height: 960 }, locale: "ru-RU" });
    intervalPage.on("pageerror", error => pageErrors.push(error.message));
    await intervalPage.goto(`${root}/?${query}`);
    const { pdf: rosePdf } = await bulkDownload(intervalPage, "svg", cards.filter(card => card.id === "wind_rose"),
      { part, multi: true, render: "wind-rose-intervals" });
    assert.ok(rosePdf.page_count > 1, "Розы ветров за несколько интервалов переходят на страницы продолжения");
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
        empty_soil: { title: svg.find(entry => entry.title === "Температура почвы · 5 см").title, message: "Нет данных за этот период" },
        pdf_in_all_formats: pdf,
        pdf_downloaded_offline: true,
        pdf_rose_continuation: rosePdf,
      },
      after_hide_and_zoom: {
        files: updated.length,
        hidden_card: "rh",
        visible_air_legend: [stationLabels[0]],
        period: zoomedPeriod,
        individual_and_bulk_svg_sha256: updated[0].sha256,
        order_on_reload: reloadedOrder,
        pdf: updatedPdf,
      },
      mobile,
      download_disabled_without_stations: true,
      mutating_http_requests: mutations.length,
    };
    await fs.mkdir(path.dirname(reportPath), { recursive: true });
    await fs.writeFile(reportPath, JSON.stringify(report, null, 2) + "\n");
    console.log("PASS: PDF и SVG/PNG/JPEG в ZIP, порядок и масштаб, розы по интервалам, офлайн-экспорт, 390 и 320 px");
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
