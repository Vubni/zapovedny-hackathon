import { accountContext } from "./account-client.js";
import { observationWorkspace, validObservationWorkspace } from "./observation-session.js";

const returnRoutes = new Set(["/", "/observations", "/forecast", "/files", "/precip", "/calendar", "/events"]);
let saveDialog;
let preparingSave = false;
let feedbackTimer;

export function safeReturnPath(value) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")
      || /[\\\u0000-\u001f\u007f]/.test(value)) return null;
  const url = new URL(value, location.origin);
  if (url.origin !== location.origin || !returnRoutes.has(url.pathname)) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

export function accountUrl({ returnTo = null, save = false } = {}) {
  const query = new URLSearchParams();
  const safe = safeReturnPath(returnTo);
  if (safe) query.set("return", safe);
  if (save) query.set("save", "1");
  return query.size ? `/account?${query}` : "/account";
}

export async function accountRequest(path, body) {
  const options = { cache: "no-store", credentials: "same-origin" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`/api/account/${path}`, options);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("Сервис личного кабинета недоступен.");
  }
  if (!response.ok) {
    const error = new Error(typeof data?.error === "string" ? data.error : "Не удалось выполнить действие.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function showFeedback(text) {
  let message = document.querySelector(".archive-session-feedback");
  if (!message) {
    message = document.createElement("p");
    message.className = "archive-session-feedback";
    message.setAttribute("role", "status");
    document.body.append(message);
  }
  clearTimeout(feedbackTimer);
  message.hidden = false;
  message.textContent = text;
  feedbackTimer = setTimeout(() => { message.hidden = true; }, 6500);
}

function showSaveDialog(workspace) {
  const dialog = document.createElement("dialog");
  dialog.className = "archive-session-dialog";
  dialog.setAttribute("aria-labelledby", "archive-session-dialog-title");
  dialog.innerHTML = `
    <form method="post" action="/api/account/sessions">
      <h2 id="archive-session-dialog-title">Сохранить сессию</h2>
      <p>Сохранятся станции, период и графики из раздела «Наблюдения».</p>
      <label for="archive-session-name">Название сессии</label>
      <input id="archive-session-name" name="name" type="text" maxlength="100" required>
      <p class="archive-session-error" role="status" hidden></p>
      <div class="archive-session-actions">
        <button class="archive-session-cancel" type="button">Отмена</button>
        <button class="archive-session-submit" type="submit">Сохранить</button>
      </div>
    </form>`;
  const form = dialog.querySelector("form");
  const input = form.elements.name;
  const save = form.querySelector("button[type=submit]");
  const cancel = form.querySelector(".archive-session-cancel");
  const error = form.querySelector(".archive-session-error");
  const today = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short" }).format(new Date());
  input.value = `Наблюдения · ${today}`;
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", event => {
    if (save.disabled) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    saveDialog = null;
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const name = input.value.trim();
    error.hidden = true;
    if (!name) {
      error.textContent = "Укажите название сессии.";
      error.hidden = false;
      input.focus();
      return;
    }
    save.disabled = cancel.disabled = input.disabled = true;
    try {
      await accountRequest("sessions", { name, state: { workspace } });
    } catch (failure) {
      if (failure.status === 401) {
        location.assign(accountUrl({ returnTo: workspace, save: true }));
        return;
      }
      error.textContent = failure.message;
      error.hidden = false;
      save.disabled = cancel.disabled = input.disabled = false;
      return;
    }
    dialog.close();
    showFeedback("Сессия сохранена в личном кабинете.");
  });
  document.body.append(dialog);
  saveDialog = dialog;
  dialog.showModal();
  input.select();
}

export async function openSaveSession({ workspace } = {}) {
  if (saveDialog?.open || preparingSave) return;
  preparingSave = true;
  try {
    const { user } = await accountContext();
    const currentWorkspace = validObservationWorkspace(workspace) || await observationWorkspace();
    if (!user) {
      location.assign(accountUrl({ returnTo: currentWorkspace || "/observations", save: true }));
      return;
    }
    if (!currentWorkspace) {
      showFeedback("Откройте «Наблюдения», чтобы настроить рабочее место.");
      return;
    }
    showSaveDialog(currentWorkspace);
  } catch (error) {
    showFeedback(error.message || "Не удалось открыть сохранение сессии.");
  } finally {
    preparingSave = false;
  }
}
