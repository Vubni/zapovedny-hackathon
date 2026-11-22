// WGS84 bounds at the outside pixel edges. Longitude is unwrapped for the national image.
export const russiaRaster = {
  id: 'russia', src: '/russia-relief.webp', bounds: [18, 40, 193, 84],
};
export const reserveRaster = {
  id: 'reserve', src: '/reserve-imagery.webp', bounds: [32.10, 56.10, 33.75, 56.95],
};

const images = new Map();

export function loadRaster({src}) {
  if (!images.has(src)) {
    const image = new Image();
    image.src = src;
    images.set(src, image.decode().then(() => image).catch(() => null));
  }
  return images.get(src);
}

export function rasterOffsets([left, bottom, right, top], {west, south, east, north}) {
  if (north <= bottom || south >= top) return [];
  const first = Math.ceil((west - right) / 360);
  const last = Math.floor((east - left) / 360);
  return Array.from({length: Math.max(0, last - first + 1)}, (_, index) => (first + index) * 360);
}
