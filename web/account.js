import { accountContext, invalidateAccountContext } from "./account-client.js";
import { accountRequest, accountUrl, safeReturnPath } from "./account-actions.js";
import { restoreObservationWorkspace, validObservationWorkspace } from "./observation-session.js";

const get = id => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" });
const roleLabels = {
  researcher: "Исследователь",
  center_admin: "Администратор центра",
  global_admin: "Глобальный администратор",
};
const query = new URLSearchParams(location.search);
const returnTo = safeReturnPath(query.get("return"));
const returnPage = returnTo ? new URL(returnTo, location.origin).pathname : null;
const saveAfterLogin = query.get("save") === "1";
let context;

function returnToSaving() {
  const target = new URL(validObservationWorkspace(returnTo) || "/observations", location.origin);
  target.searchParams.set("save-session", "1");
  location.replace(target.pathname + target.search);
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function message(node, text, error = false) {
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle("account-error", error);
}

function report(error, node) {
  if (error.status === 401 && context?.user) {
    invalidateAccountContext();
    location.replace(accountUrl());
    return;
  }
  message(node, error.message || "Не удалось выполнить действие.", true);
}

function bindForm(id, run) {
  const form = get(id);
  const submit = form.querySelector("button[type=submit]");
  const status = form.querySelector("[data-message]");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (submit.disabled) return;
    const data = new FormData(form);
    message(status, "");
    submit.disabled = true;
    form.setAttribute("aria-busy", "true");
    try {
      await run(data, status, form);
    } catch (error) {
      report(error, status);
    } finally {
      submit.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
}

function formValue(data, key) {
  return String(data.get(key) ?? "").trim();
}

function centerOption(id, label) {
  const option = element("option", "", label);
  option.value = id;
  return option;
}

function renderCenterOptions() {
  const centers = context.centers;
  for (const select of [get("account-center-choice"), get("account-user-center")]) {
    select.replaceChildren(...centers.map(center => centerOption(center.id, center.name)));
    if (context.center?.id != null) select.value = String(context.center.id);
  }
}

function findCenter(value) {
  const center = context.centers.find(item => String(item.id) === String(value));
  if (!center) throw new Error("Выберите центр из списка.");
  return center;
}

function timeText(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf()) ? dateFormat.format(date) : "";
}

function confirmSessionDeletion(session) {
  const dialog = element("dialog", "archive-session-dialog account-delete-dialog");
  dialog.setAttribute("aria-labelledby", "account-delete-title");
  const title = element("h2", "", "Удалить сессию?");
  title.id = "account-delete-title";
  const detail = element("p", "", `Удалить «${session.name}» из сохранённых сессий?`);
  const status = element("p", "account-status account-error");
  status.setAttribute("role", "status");
  status.hidden = true;
  const actions = element("div", "archive-session-actions");
  const cancel = element("button", "archive-session-cancel", "Отмена");
  const confirm = element("button", "account-delete-submit", "Удалить");
  actions.append(cancel, confirm);
  dialog.append(title, detail, status, actions);
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("cancel", event => {
    if (confirm.disabled) event.preventDefault();
  });
  cancel.addEventListener("click", () => dialog.close());
  confirm.addEventListener("click", async () => {
    cancel.disabled = confirm.disabled = true;
    message(status, "");
    try {
      await accountRequest("sessions/delete", { id: session.id });
      dialog.close();
      await loadSessions("Сессия удалена.");
      get("account-sessions-title").focus();
    } catch (error) {
      report(error, status);
      cancel.disabled = confirm.disabled = false;
    }
  });
  dialog.showModal();
  cancel.focus();
}

function sessionRow(session) {
  const li = element("li");
  const text = element("div");
  text.append(element("strong", "", session.name));
  const saved = timeText(session.created_at);
  if (saved) text.append(element("span", "muted", saved));
  const open = element("button", "btn line", "Открыть");
  open.type = "button";
  open.setAttribute("aria-label", `Открыть сессию ${session.name}`);
  open.addEventListener("click", async () => {
    open.disabled = true;
    const status = get("account-sessions-status");
    message(status, "");
    try {
      const query = new URLSearchParams({ id: session.id });
      const saved = await accountRequest(`session?${query}`);
      if (typeof saved.session?.state?.workspace !== "string" ||
          await restoreObservationWorkspace(saved.session.state.workspace) === false) {
        throw new Error("Эту сессию не удалось восстановить.");
      }
    } catch (error) {
      report(error, status);
      open.disabled = false;
    }
  });
  const deleteButton = element("button", "btn line account-delete-button", "Удалить");
  deleteButton.type = "button";
  deleteButton.setAttribute("aria-label", `Удалить сессию ${session.name}`);
  deleteButton.addEventListener("click", () => confirmSessionDeletion(session));
  const actions = element("div", "account-row-actions");
  actions.append(open, deleteButton);
  li.append(text, actions);
  return li;
}

async function loadSessions(success = "") {
  const status = get("account-sessions-status");
  const list = get("account-session-list");
  list.setAttribute("aria-busy", "true");
  message(status, "Загрузка сессий…");
  try {
    const { sessions } = await accountRequest("sessions");
    list.replaceChildren(...sessions.map(sessionRow));
    message(status, [success, sessions.length ? "" : "Сохранённых сессий пока нет."].filter(Boolean).join(" "));
  } catch (error) {
    report(error, status);
  } finally {
    list.removeAttribute("aria-busy");
  }
}

function userRow(user) {
  const li = element("li");
  const text = element("div");
  text.append(element("strong", "", user.name || user.email), element("span", "muted", user.email));
  const center = context.centers.find(item => String(item.id) === String(user.center_id));
  const label = [roleLabels[user.role], context.user.role === "global_admin" ? center?.name : null].filter(Boolean);
  if (label.length) text.append(element("span", "muted", label.join(" · ")));
  li.append(text);
  return li;
}

async function loadUsers() {
  const status = get("account-users-status");
  const list = get("account-user-list");
  list.setAttribute("aria-busy", "true");
  message(status, "Загрузка списка пользователей…");
  try {
    const { users } = await accountRequest("users");
    list.replaceChildren(...users.map(userRow));
    message(status, users.length ? "" : "В центре пока нет других пользователей.");
  } catch (error) {
    report(error, status);
  } finally {
    list.removeAttribute("aria-busy");
  }
}

function render() {
  const { user, center } = context;
  const loggedIn = Boolean(user);
  const global = user?.role === "global_admin";
  const manage = global || user?.role === "center_admin";
  for (const id of ["account-profile", "account-logout", "account-sessions"]) get(id).hidden = !loggedIn;
  get("account-guest").hidden = loggedIn;
  get("account-centers").hidden = !global;
  get("account-users").hidden = !manage;
  message(get("account-status"), "");
  if (!loggedIn) return;

  get("account-name").textContent = user.name || user.email;
  get("account-profile-email").textContent = user.email;
  get("account-center-name").textContent = center?.name || "";
  get("account-role").textContent = roleLabels[user.role] || "";
  if (global) renderCenterOptions();
  if (manage) {
    get("account-user-center-label").hidden = get("account-user-center").hidden = !global;
    get("account-add-user-title").textContent = global ? "Пригласить администратора центра" : "Добавить исследователя";
    get("account-user-submit").textContent = global ? "Пригласить администратора" : "Добавить исследователя";
  }
}

bindForm("account-login", async (data, status, form) => {
  await accountRequest("login", { email: formValue(data, "email"), password: data.get("password") });
  form.reset();
  invalidateAccountContext();
  if (saveAfterLogin) {
    returnToSaving();
  } else {
    location.replace(returnPage || "/account");
  }
});

get("account-logout").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await accountRequest("logout", {});
    invalidateAccountContext();
    location.replace("/account");
  } catch (error) {
    report(error, get("account-status"));
    button.disabled = false;
  }
});

bindForm("account-switch-center", async data => {
  if (context.user?.role !== "global_admin") return;
  const selected = findCenter(data.get("center_id"));
  if (String(selected.id) === String(context.center?.id)) return;
  await accountRequest("select-center", { center_id: selected.id });
  invalidateAccountContext();
  location.replace("/account");
});

bindForm("account-create-center", async (data, status, form) => {
  if (context.user?.role !== "global_admin") return;
  const { center } = await accountRequest("centers", { name: formValue(data, "name") });
  form.reset();
  context = await accountContext({ refresh: true });
  renderCenterOptions();
  get("account-user-center").value = String(center.id);
  message(status, `Центр «${center.name}» создан.`);
});

bindForm("account-add-user", async (data, status, form) => {
  const global = context.user?.role === "global_admin";
  if (!global && context.user?.role !== "center_admin") return;
  const centerId = global ? findCenter(data.get("center_id")).id : (context.center?.id ?? context.user.center_id);
  const email = formValue(data, "email");
  await accountRequest("users", {
    email, name: formValue(data, "name"), password: data.get("password"),
    role: global ? "center_admin" : "researcher", center_id: centerId,
  });
  form.reset();
  if (global) get("account-user-center").value = String(centerId);
  message(status, `Доступ для ${email} создан.`);
  await loadUsers();
});

async function start() {
  try {
    context = await accountContext();
    if (context.user && saveAfterLogin) {
      returnToSaving();
      return;
    }
    render();
    if (!context.user) return;
    loadSessions();
    if (["center_admin", "global_admin"].includes(context.user.role)) loadUsers();
  } catch (error) {
    report(error, get("account-status"));
  }
}

start();
