// The former audit URLs must keep working as links to the unified file workflow.
const assert = require("node:assert/strict");
const { withIsolatedMeteo } = require("./isolated-meteo.cjs");

async function assertRedirect(base, source, target) {
  const response = await fetch(base + source, { redirect: "manual" });
  assert.equal(response.status, 303);
  assert.equal(response.headers.get("location"), target);
  const opened = await fetch(base + target, { redirect: "manual" });
  assert.equal(opened.status, 200);
  assert.match(opened.headers.get("content-type"), /^text\/html/);
}

withIsolatedMeteo(async base => {
  await assertRedirect(base, "/review", "/files");
  const query = new URLSearchParams({ filename: "compat-review.csv", station: "kr1" });
  const upload = await fetch(`${base}/api/files/upload?${query}`, {
    method: "POST",
    headers: { "Content-Type": "text/csv" },
    body: "datetime,t_air\n2031-02-10 12:00,5\n",
  });
  const file = await upload.json();
  assert.equal(upload.status, 201, JSON.stringify(file));
  const link = new URLSearchParams({ file: file.id });
  await assertRedirect(base, `/review?${link}`, `/files?${link}`);
  console.log("PASS: ссылки на прежний аудит переходят к файлам и сохраняют ID исходника.");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
