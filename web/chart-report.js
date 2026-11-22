const encoder = new TextEncoder();
const pageWidth = 841.89;
const pageHeight = 595.28;
const margin = 22;

function unicodeString(value) {
  let hex = "FEFF";
  for (let i = 0; i < value.length; i++) hex += value.charCodeAt(i).toString(16).padStart(4, "0");
  return `<${hex}>`;
}

function stream(body, properties = "") {
  const bytes = typeof body === "string" ? encoder.encode(body) : body;
  return [`<< /Length ${bytes instanceof Blob ? bytes.size : bytes.byteLength}${properties} >>\nstream\n`, bytes, "\nendstream"];
}

/** Assembles print-ready chart pages from the same captured views as the image downloads. */
export function createChartReport(sheets) {
  if (!sheets.length) throw new RangeError("В отчёте нет графиков.");
  const objects = [null];
  const reserve = () => objects.push(null) - 1;
  const set = (id, ...parts) => { objects[id] = parts; };
  const add = (...parts) => {
    const id = reserve();
    set(id, ...parts);
    return id;
  };
  const catalog = reserve();
  const pages = reserve();
  const bookmarks = reserve();
  const footerFont = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  const info = add(`<< /Title ${unicodeString("Метеоархив — все графики")} /Creator ${unicodeString("Метеоархив")} >>`);
  const kids = [];
  const outlines = [];

  for (const [index, sheet] of sheets.entries()) {
    const { width, height, blob, title } = sheet;
    const image = add(...stream(blob, ` /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode`));
    const scale = Math.min((pageWidth - 2 * margin) / width, (pageHeight - 2 * margin) / height);
    const w = (width * scale).toFixed(3);
    const h = (height * scale).toFixed(3);
    const left = ((pageWidth - w) / 2).toFixed(3);
    const bottom = (pageHeight - margin - h).toFixed(3);
    const content = add(...stream(
      `q\n${w} 0 0 ${h} ${left} ${bottom} cm\n/Chart Do\nQ\n0.26 0.32 0.28 rg\nBT /F0 9 Tf 767 14 Td (${index + 1} / ${sheets.length}) Tj ET\n`,
    ));
    const page = add(`<< /Type /Page /Parent ${pages} 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] /Resources << /XObject << /Chart ${image} 0 R >> /Font << /F0 ${footerFont} 0 R >> >> /Contents ${content} 0 R >>`);
    kids.push(`${page} 0 R`);
    if (title) outlines.push({ id: reserve(), title, page });
  }

  set(pages, `<< /Type /Pages /Kids [${kids.join(" ")}] /Count ${kids.length} >>`);
  for (const [index, { id, title, page }] of outlines.entries()) {
    const prev = index ? ` /Prev ${outlines[index - 1].id} 0 R` : "";
    const next = index + 1 < outlines.length ? ` /Next ${outlines[index + 1].id} 0 R` : "";
    set(id, `<< /Title ${unicodeString(title)} /Parent ${bookmarks} 0 R /Dest [${page} 0 R /Fit]${prev}${next} >>`);
  }
  const first = outlines[0]?.id;
  const last = outlines.at(-1)?.id;
  set(bookmarks, first ? `<< /Type /Outlines /First ${first} 0 R /Last ${last} 0 R /Count ${outlines.length} >>`
    : "<< /Type /Outlines /Count 0 >>");
  set(catalog, `<< /Type /Catalog /Pages ${pages} 0 R /Outlines ${bookmarks} 0 R >>`);

  const header = encoder.encode("%PDF-1.4\n%âãÏÓ\n");
  const chunks = [header];
  const offsets = ["0000000000 65535 f \n"];
  let offset = header.byteLength;
  for (let id = 1; id < objects.length; id++) {
    offsets.push(`${String(offset).padStart(10, "0")} 00000 n \n`);
    for (const part of [`${id} 0 obj\n`, ...objects[id], "\nendobj\n"]) {
      const bytes = typeof part === "string" ? encoder.encode(part) : part;
      chunks.push(bytes);
      offset += bytes instanceof Blob ? bytes.size : bytes.byteLength;
    }
  }
  chunks.push(`xref\n0 ${objects.length}\n${offsets.join("")}trailer\n<< /Size ${objects.length} /Root ${catalog} 0 R /Info ${info} 0 R >>\nstartxref\n${offset}\n%%EOF\n`);
  return new Blob(chunks, { type: "application/pdf" });
}
