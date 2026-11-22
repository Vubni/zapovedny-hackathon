// The archive navigation is shared by modern workspaces and detailed journal tools.
const paths = {
  chart: '<path d="M3 19h18M5 15l4-5 4 3 6-8"/><circle cx="19" cy="5" r="1.3"/>',
  cloud: '<path d="M6.5 18a4.5 4.5 0 0 1-.7-9 6.1 6.1 0 0 1 11.7-1.3A5.15 5.15 0 0 1 17 18H6.5Z"/>',
  file: '<path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10Zm0 0v7h7M8 14h8m-8 3h8"/>',
};
const links = [
  { path: "/", label: "Наблюдения", glyph: "chart" },
  { path: "/forecast", label: "Прогноз", glyph: "cloud" },
  { path: "/files", label: "Файлы и импорт", glyph: "file" },
];

function icon(key, size) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[key]}</svg>`;
}

for (const aside of document.querySelectorAll("[data-archive-nav]")) {
  const route = location.pathname.split(".")[0];
  const active = ["/forecast", "/model", "/precip"].includes(route) ? "/forecast"
    : ["/files", "/review", "/import"].includes(route) ? "/files" : "/";
  aside.setAttribute("aria-label", "Разделы Метеоархива");
  aside.innerHTML = `
    <a href="/" class="brand" aria-label="Метеоархив, на главную"><img class="reserve-logo" src="/clgz-logo.png" width="100" height="100" alt="Центрально-Лесной заповедник"><span>Метеоархив</span></a>

    <nav class="nav" aria-label="Основная навигация">${links.map(({path,label,glyph}) =>
      `<a href="${path}" ${path === active ? 'aria-current="page"' : ''} aria-label="${label}" title="${label}">${icon(glyph,18)}<span>${label}</span></a>`).join("")}</nav>
    <div class="side-footer">Урочище Красное<strong>Центрально-Лесной заповедник</strong></div>`;
}
