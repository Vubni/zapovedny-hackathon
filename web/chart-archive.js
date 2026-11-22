// ZIP "stored" entries keep the individual SVG, PNG and JPEG files byte-for-byte.
const crcTable = Uint32Array.from({ length: 256 }, (_, byte) => {
  let value = byte;
  for (let bit = 0; bit < 8; bit++) {
    value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  }
  return value;
});

function crc32(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) value = crcTable[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function fileHeader(name, size, checksum, localOffset) {
  const central = localOffset !== undefined;
  const sizeOfHeader = central ? 46 : 30;
  const bytes = new Uint8Array(sizeOfHeader + name.byteLength);
  const view = new DataView(bytes.buffer);
  view.setUint32(0, central ? 0x02014b50 : 0x04034b50, true);
  const shift = central ? 2 : 0;
  if (central) view.setUint16(4, 20, true);        // Creator version
  view.setUint16(4 + shift, 20, true);             // Extractor version
  view.setUint16(6 + shift, 0x0800, true);         // UTF-8 names
  view.setUint16(12 + shift, 0x0021, true);        // 1980-01-01 (earliest ZIP date)
  view.setUint32(14 + shift, checksum, true);
  view.setUint32(18 + shift, size, true);
  view.setUint32(22 + shift, size, true);
  view.setUint16(26 + shift, name.byteLength, true);
  if (central) view.setUint32(42, localOffset, true);
  bytes.set(name, sizeOfHeader);
  return bytes;
}

export async function createChartArchive(files) {
  const encoder = new TextEncoder();
  const entries = [];
  const directory = [];
  let directorySize = 0;
  let offset = 0;
  for (const [index, { blob, filename }] of files.entries()) {
    const name = encoder.encode(`${String(index + 1).padStart(2, "0")}-${filename}`);
    const checksum = crc32(new Uint8Array(await blob.arrayBuffer()));
    const local = fileHeader(name, blob.size, checksum);
    const central = fileHeader(name, blob.size, checksum, offset);
    entries.push(local, blob);
    directory.push(central);
    offset += local.byteLength + blob.size;
    directorySize += central.byteLength;
  }
  if (offset + directorySize > 0xffffffff) throw new RangeError("Превышен размер ZIP-архива.");
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, directorySize, true);
  end.setUint32(16, offset, true);
  return new Blob([...entries, ...directory, end], { type: "application/zip" });
}
