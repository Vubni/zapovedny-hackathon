// Shared navigation for the public archive workspaces.
const paths = {
  chart: '<path d="M3 19h18M5 15l4-5 4 3 6-8"/><circle cx="19" cy="5" r="1.3"/>',
  cloud: '<path d="M6.5 18a4.5 4.5 0 0 1-.7-9 6.1 6.1 0 0 1 11.7-1.3A5.15 5.15 0 0 1 17 18H6.5Z"/>',
  file: '<path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10Zm0 0v7h7M8 14h8m-8 3h8"/>',
  account: '<circle cx="12" cy="8" r="3.2"/><path d="M5 20v-2a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v2"/>',
  center: '<path d="M3 21h18M5 21V8l7-5 7 5v13M10 21v-5h4v5M9 10h.1m5.9 0h.1"/>',
  collapse: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M10 4v16m7-11-3 3 3 3"/>',
  expand: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M10 4v16m4-11 3 3-3 3"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
};
const links = [
  { path: "/", label: "Наблюдения", glyph: "chart" },
  { path: "/forecast", label: "Прогноз", glyph: "cloud" },
  { path: "/files", label: "Файлы и импорт", glyph: "file" },
  { path: "/account", label: "Личный кабинет", glyph: "account" },
];

const storageKey = "meteoarchive.navigation.v1";
const defaultWidth = 224;
const railWidth = 66;
const minWidth = 180;
const maxWidth = 360;
const keyboardStep = 16;
const mobileViewport = window.matchMedia("(max-width: 660px)");
const wideViewport = window.matchMedia("(min-width: 901px)");

function icon(key, size = 18) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[key]}</svg>`;
}

function clampWidth(value, maximum = maxWidth) {
  return Math.min(maximum, Math.max(minWidth, Math.round(value)));
}

function readPreferences() {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey));
    return {
      width: Number.isFinite(value?.width) ? clampWidth(value.width) : defaultWidth,
      desktopExpanded: typeof value?.desktopExpanded === "boolean" ? value.desktopExpanded : null,
      mobileExpanded: value?.mobileExpanded === true,
    };
  } catch {
    return { width: defaultWidth, desktopExpanded: null, mobileExpanded: false };
  }
}

function activeSection() {
  const route = location.pathname.replace(/\.html$/, "").replace(/\/$/, "");
  if (["/forecast", "/precip"].includes(route)) return "/forecast";
  if (["/files", "/review", "/import"].includes(route)) return "/files";
  if (route === "/account") return "/account";
  if (route === "/operator") return null;
  return "/";
}

function mountSidebar(sidebar) {
  const active = activeSection();
  sidebar.setAttribute("aria-label", "Разделы Метеоархива");
  sidebar.innerHTML = `
    <div class="archive-sidebar-head">
      <a href="/" class="brand archive-sidebar-brand" aria-label="Центрально-Лесной заповедник · Метеоархив">
        <img class="reserve-logo" src="/clgz-logo.png" width="80" height="80" alt="">
        <i class="archive-sidebar-center-icon" hidden>${icon("center", 25)}</i>
        <span class="archive-sidebar-name"><span>Центрально-Лесной</span> заповедник</span>
      </a>
      <button type="button" class="archive-sidebar-toggle" aria-controls="archive-sidebar-menu"></button>
    </div>
    <nav id="archive-sidebar-menu" class="nav archive-sidebar-links" aria-label="Основная навигация">${links.map(({ path, label, glyph }) =>
      `<a href="${path}" ${path === active ? 'aria-current="page"' : ''} aria-label="${label}" title="${label}">${icon(glyph)}<span>${label}</span></a>`).join("")}</nav>
    <div class="archive-sidebar-resize" role="separator" tabindex="0" aria-orientation="vertical"
      aria-controls="archive-sidebar-menu" aria-label="Ширина боковой панели" title="Изменить ширину боковой панели"></div>`;

  const root = document.documentElement;
  const menu = sidebar.querySelector("#archive-sidebar-menu");
  const toggle = sidebar.querySelector(".archive-sidebar-toggle");
  const separator = sidebar.querySelector(".archive-sidebar-resize");
  let preferences = readPreferences();
  let drag;
  let frame = 0;

  function viewportLimit() {
    return clampWidth(root.clientWidth - 460);
  }

  function render() {
    const mobile = mobileViewport.matches;
    const expanded = mobile ? preferences.mobileExpanded : (preferences.desktopExpanded ?? wideViewport.matches);
    const width = clampWidth(preferences.width, viewportLimit());
    const layout = mobile ? "mobile" : expanded ? "sidebar" : "rail";
    sidebar.dataset.layout = layout;
    sidebar.dataset.expanded = String(expanded);
    const buttonLabel = mobile
      ? (expanded ? "Закрыть меню" : "Открыть меню")
      : (expanded ? "Свернуть боковую панель" : "Развернуть боковую панель");
    if (toggle.getAttribute("aria-label") !== buttonLabel) {
      toggle.innerHTML = icon(mobile ? (expanded ? "close" : "menu") : (expanded ? "collapse" : "expand"), 20);
      toggle.setAttribute("aria-label", buttonLabel);
      toggle.title = buttonLabel;
    }
    toggle.setAttribute("aria-expanded", String(expanded));
    menu.hidden = mobile && !expanded;
    separator.hidden = layout !== "sidebar";
    separator.setAttribute("aria-valuemin", minWidth);
    separator.setAttribute("aria-valuemax", viewportLimit());
    separator.setAttribute("aria-valuenow", width);
    separator.setAttribute("aria-valuetext", `${width} пикселей`);
    root.style.setProperty("--archive-sidebar-width", `${expanded ? width : railWidth}px`);
  }

  function save() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(preferences));
    } catch {
      // The navigation still works when browser storage is disabled.
    }
  }

  function changeExpanded(expanded) {
    const key = mobileViewport.matches ? "mobileExpanded" : "desktopExpanded";
    preferences[key] = expanded;
    render();
    save();
  }

  function applyDrag(position) {
    preferences.width = clampWidth(drag.startWidth + position - drag.startX, viewportLimit());
    render();
  }

  function finishDrag(commit, position) {
    if (!drag) return;
    cancelAnimationFrame(frame);
    frame = 0;
    if (commit && position !== drag.startX) {
      applyDrag(position);
      save();
    } else {
      preferences.width = drag.previousWidth;
      render();
    }
    drag = undefined;
    root.removeAttribute("data-archive-resizing");
  }

  toggle.addEventListener("click", () => changeExpanded(toggle.getAttribute("aria-expanded") !== "true"));
  sidebar.addEventListener("keydown", event => {
    if (event.key === "Escape" && mobileViewport.matches && preferences.mobileExpanded) {
      event.preventDefault();
      toggle.focus();
      changeExpanded(false);
    }
  });
  separator.addEventListener("keydown", event => {
    let width = Number(separator.getAttribute("aria-valuenow"));
    if (event.key === "ArrowLeft") width -= keyboardStep;
    else if (event.key === "ArrowRight") width += keyboardStep;
    else if (event.key === "Home") width = minWidth;
    else if (event.key === "End") width = viewportLimit();
    else if (event.key === "Enter") {
      event.preventDefault();
      toggle.focus();
      changeExpanded(false);
      return;
    } else return;
    event.preventDefault();
    preferences.width = clampWidth(width, viewportLimit());
    render();
    save();
  });
  separator.addEventListener("pointerdown", event => {
    if (event.button !== 0 || drag) return;
    drag = {
      pointer: event.pointerId,
      startX: event.clientX,
      startWidth: sidebar.getBoundingClientRect().width,
      previousWidth: preferences.width,
      latest: event.clientX,
    };
    root.setAttribute("data-archive-resizing", "");
    separator.setPointerCapture(event.pointerId);
  });
  separator.addEventListener("pointermove", event => {
    if (drag?.pointer !== event.pointerId) return;
    drag.latest = event.clientX;
    if (!frame) {
      frame = requestAnimationFrame(() => {
        frame = 0;
        applyDrag(drag.latest);
      });
    }
  });
  separator.addEventListener("pointerup", event => {
    if (drag?.pointer === event.pointerId) finishDrag(true, event.clientX);
  });
  separator.addEventListener("lostpointercapture", event => {
    if (drag?.pointer === event.pointerId) finishDrag(false);
  });

  window.addEventListener("resize", render);
  window.addEventListener("storage", event => {
    if (event.key !== storageKey) return;
    preferences = readPreferences();
    render();
  });
  render();
  const brand = sidebar.querySelector(".archive-sidebar-brand");
  const logo = sidebar.querySelector(".reserve-logo");
  const centerIcon = sidebar.querySelector(".archive-sidebar-center-icon");
  const brandName = sidebar.querySelector(".archive-sidebar-name");
  const centralName = brandName.innerHTML;
  async function renderCenter(refresh = false) {
    try {
      const { accountContext } = await import("/account-client.js");
      const context = await accountContext({ refresh });
      const central = !context.center?.id || context.center.id === "central-forest";
      logo.hidden = !central;
      centerIcon.hidden = central;
      if (central) brandName.innerHTML = centralName;
      else brandName.textContent = context.center.name;
      brand.setAttribute("aria-label", `${brandName.textContent} · Метеоархив`);
    } catch {
      // Public sections remain usable if the account service is temporarily unavailable.
    }
  }
  renderCenter();
  window.addEventListener("storage", event => {
    if (event.key === "meteoarchive.account.revision") renderCenter(true);
  });
}

const sidebar = document.querySelector("[data-archive-nav]");
if (sidebar) mountSidebar(sidebar);

const saveTrigger = document.querySelector("main [data-save-observation-workspace]");
if (saveTrigger) {
  async function prepareSave(workspace) {
    if (saveTrigger.disabled) return;
    saveTrigger.disabled = true;
    try {
      const { openSaveSession } = await import("/account-actions.js");
      await openSaveSession({ workspace });
    } catch {
      const status = document.createElement("p");
      status.className = "archive-session-feedback";
      status.setAttribute("role", "status");
      status.textContent = "Не удалось открыть сохранение сессии.";
      document.body.append(status);
      setTimeout(() => status.remove(), 6500);
    } finally {
      saveTrigger.disabled = false;
    }
  }
  saveTrigger.addEventListener("click", () => prepareSave());
  const url = new URL(location.href);
  if (url.searchParams.get("save-session") === "1") {
    url.searchParams.delete("save-session");
    history.replaceState(history.state, "", url);
    prepareSave(url.search ? url.pathname + url.search : undefined);
  }
}
