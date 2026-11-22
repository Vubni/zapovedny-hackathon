// The former audit URLs must keep working as links to the unified file workflow.
const assert = require("node:assert/strict");
const { request } = require("playwright");
const { withIsolatedMeteo, loginAccount } = require("./isolated-meteo.cjs");

async function assertRedirect(base, source, target) {
  const response = await fetch(base + source, { redirect: "manual" });
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), target);
  const opened = await fetch(base + target, { redirect: "manual" });
  assert.equal(opened.status, 200);
  assert.match(opened.headers.get("content-type"), /^text\/html/);
}

withIsolatedMeteo(async (base, { globalAdmin }) => {
  await assertRedirect(base, "/review", "/files");
  const api = await request.newContext();
  try {
    await loginAccount(api, base, globalAdmin);
    const query = new URLSearchParams({ filename: "compat-review.csv", station: "kr1" });
    const upload = await api.post(`${base}/api/files/upload?${query}`, {
      headers: { "Content-Type": "text/csv" },
      data: "datetime,t_air\n2031-02-10 12:00,5\n",
    });
    const file = await upload.json();
    assert.equal(upload.status(), 201, JSON.stringify(file));
    const link = new URLSearchParams({ file: file.id });
    await assertRedirect(base, `/review?${link}`, `/files?${link}`);
  } finally {
    await api.dispose();
  }
  console.log("PASS: ссылки на прежний аудит переходят к файлам и сохраняют ID исходника.");
}, { accounts: true }).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
