import './account-client.js';

const centralForest = {
  id: 'central-forest',
  name: 'Центрально-Лесной заповедник',
  label: 'Урочище «Красное»',
  point: {latitude: 56.464310, longitude: 32.970290},
  bundled: true,
};
const formatCoordinate = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 6});

export function wrapLongitude(longitude) {
  return ((longitude + 180) % 360 + 360) % 360 - 180;
}

export function coordinates({latitude, longitude}) {
  const north = latitude < 0 ? 'ю. ш.' : 'с. ш.';
  const east = longitude < 0 ? 'з. д.' : 'в. д.';
  return `${formatCoordinate.format(Math.abs(latitude))}° ${north}, ${formatCoordinate.format(Math.abs(longitude))}° ${east}`;
}

function hasCoordinates(station) {
  return Number.isFinite(station?.latitude) && Math.abs(station.latitude) <= 90 &&
    Number.isFinite(station.longitude) && Math.abs(station.longitude) <= 180;
}

export function distanceKm(a, b) {
  if (!hasCoordinates(a) || !hasCoordinates(b)) return Infinity;
  const radians = Math.PI / 180;
  const latitudeDelta = Math.sin((a.latitude - b.latitude) * radians / 2);
  const longitudeDelta = Math.sin((a.longitude - b.longitude) * radians / 2);
  const square = latitudeDelta ** 2 + longitudeDelta ** 2 *
    Math.cos(a.latitude * radians) * Math.cos(b.latitude * radians);
  return 12742 * Math.asin(Math.sqrt(Math.min(1, square)));
}

async function scopedRequest(url) {
  return fetch(url, {cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(5000)});
}

async function accountContext() {
  const response = await scopedRequest('/api/account/me');
  if (!response.ok) throw new Error('Не удалось определить активный заповедник.');
  return response.json();
}

export async function activeWeatherLocation() {
  const account = await accountContext();
  if (account?.center?.id === centralForest.id) return centralForest;
  const center = account?.center;
  if (typeof center?.id !== 'string' || !center.id || typeof center.name !== 'string' || !center.name.trim()) {
    throw new Error('Не удалось определить активный заповедник.');
  }
  const catalog = await scopedRequest('/api/stations');
  if (!catalog.ok) throw new Error('Не удалось загрузить станции этого заповедника.');
  const {stations} = await catalog.json();
  if (!Array.isArray(stations)) throw new Error('Не удалось загрузить станции этого заповедника.');
  const confirmed = await accountContext();
  if (confirmed?.center?.id !== center.id) throw new Error('Активный заповедник изменился.');
  const station = stations.find(item => hasCoordinates(item) && item.archive !== true &&
    item.location_precision !== 'area' && item.location_precision !== 'unknown' &&
    (item.center_id == null || item.center_id === center.id));
  if (!station) return {id: center.id, name: center.name, point: null, bundled: false};
  return {
    id: center.id,
    name: center.name,
    label: String(station.label || station.id || 'Метеостанция'),
    point: {latitude: station.latitude, longitude: station.longitude},
    bundled: false,
  };
}
