import { accountContext, accountScope } from "/account-client.js";

export const maxObservationWorkspaceBytes = 8192;
export const defaultObservationWorkspace = Object.freeze({
  stations: "kr1", start: "2025-07-01", end: "2025-07-31", grain: "day", mode: "modern", charts: "air",
  calendar: "0", coverage: "0", coverageParameter: "", indicators: "", presentation: "{}",
});
const workspaceKeys = new Set([
  "stations", "start", "end", "intervals", "mode", "grain", "charts", "calendar",
  "coverage", "coverageParameter", "indicators", "presentation",
]);
const storagePrefix = "meteoarchive.observations.workspace.v2:";
let liveWorkspace;

export function validObservationWorkspace(path) {
  if (typeof path !== "string" || /[\u0000-\u0020\u007f]/.test(path) || !/^\/observations(?:\?|$)/.test(path) ||
      new TextEncoder().encode(path).length > maxObservationWorkspaceBytes) return null;
  const url = new URL(path, location.origin);
  if (url.pathname !== "/observations" || url.hash || url.origin !== location.origin) return null;
  const seen = new Set();
  for (const key of url.searchParams.keys()) {
    if (!workspaceKeys.has(key) || seen.has(key)) return null;
    seen.add(key);
  }
  const normalized = url.pathname + url.search;
  return new TextEncoder().encode(normalized).length <= maxObservationWorkspaceBytes ? normalized : null;
}

export function readObservationWorkspace(scope) {
  if (!scope) return null;
  try {
    return validObservationWorkspace(localStorage.getItem(storagePrefix + scope));
  } catch {
    return null;
  }
}

export function rememberObservationWorkspace(scope, path) {
  const safe = validObservationWorkspace(path);
  if (!scope || !safe) return;
  try {
    localStorage.setItem(storagePrefix + scope, safe);
  } catch {
    // The current page can still provide its live workspace.
  }
}

export function registerObservationWorkspace(scope, read) {
  liveWorkspace = { scope, read };
}

async function defaultWorkspace() {
  const params = new URLSearchParams(defaultObservationWorkspace);
  try {
    const response = await fetch("/api/stations", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("Station catalog unavailable");
    const { stations } = await response.json();
    if (!Array.isArray(stations) || !stations.some(station => station.id === defaultObservationWorkspace.stations)) {
      params.set("stations", "");
    }
  } catch {
    params.set("stations", "");
  }
  return "/observations?" + params;
}

export async function observationWorkspace() {
  const scope = accountScope(await accountContext());
  if (!scope) return null;
  if (liveWorkspace?.scope === scope) {
    const current = validObservationWorkspace(liveWorkspace.read());
    if (current) return current;
  }
  return readObservationWorkspace(scope) || defaultWorkspace();
}

export function restoreObservationWorkspace(path) {
  const safe = validObservationWorkspace(path);
  if (!safe) return false;
  location.assign(safe);
  return true;
}
