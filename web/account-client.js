const accountRevisionKey = "meteoarchive.account.revision";
const unavailable = { user: null, center: null, centers: [] };
let contextRequest;

function hasId(value) {
  return (typeof value === "string" && value.trim() !== "") || Number.isSafeInteger(value);
}

export function accountContext({ refresh = false } = {}) {
  if (!contextRequest || refresh) {
    contextRequest = fetch("/api/account/me", { credentials: "same-origin", cache: "no-store" })
      .then(async response => {
        if (!response.ok) return unavailable;
        const context = await response.json();
        if (!hasId(context?.center?.id) || (context.user !== null && !hasId(context.user?.id))) return unavailable;
        return { user: context.user, center: context.center, centers: Array.isArray(context.centers) ? context.centers : [] };
      })
      .catch(() => unavailable);
  }
  return contextRequest;
}

export function accountScope(context) {
  if (!hasId(context?.center?.id) || (context.user !== null && !hasId(context.user?.id))) return null;
  return JSON.stringify([String(context.center.id), context.user === null ? null : String(context.user.id)]);
}

export function canRegisterStations(context) {
  if (!accountScope(context)) return false;
  const user = context.user;
  return user?.role === "global_admin" ||
    (user?.role === "center_admin" && String(user.center_id) === String(context.center.id));
}

function clearAccountContext() {
  contextRequest = undefined;
  window.dispatchEvent(new Event("meteoarchive:account-change"));
}

export function invalidateAccountContext() {
  clearAccountContext();
  try {
    localStorage.setItem(accountRevisionKey, `${Date.now()}:${Math.random()}`);
  } catch {
    // Other views still revalidate when restored from the back-forward cache.
  }
}

window.addEventListener("storage", event => {
  if (event.key === accountRevisionKey) clearAccountContext();
});
