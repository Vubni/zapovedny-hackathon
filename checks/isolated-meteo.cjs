const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const root = path.join(__dirname, "..");
const bootstrap = `
import json
import sys
from http.server import ThreadingHTTPServer
from server import Handler
from station_catalog import register_station

for station in json.loads(sys.argv[1]):
    register_station(station)
with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
    print(http.server_port, flush=True)
    http.serve_forever()
`;

function bootstrapAdmin(env) {
  assert.equal(env.METEO_DATABASE_URL, "", "Accounts fixtures require an isolated SQLite database.");
  assert.ok(env.METEO_OBSERVATION_DB);
  const admin = {
    email: `global-${randomBytes(6).toString("hex")}@example.test`,
    name: "Глобальный администратор для проверки",
    password: randomBytes(24).toString("hex"),
  };
  execFileSync(process.env.PYTHON || "python3", [
    "accounts.py", "bootstrap", "--email", admin.email, "--name", admin.name, "--password-stdin",
  ], { cwd: root, env, input: admin.password + "\n", encoding: "utf8", timeout: 15000 });
  return admin;
}

async function accountPost(api, base, route, data, status) {
  const response = await api.post(base + route, { data, headers: { Origin: base } });
  const body = await response.text();
  assert.equal(response.status(), status, `${route}: ${body}`);
  return JSON.parse(body);
}

function loginAccount(api, base, { email, password }) {
  return accountPost(api, base, "/api/account/login", { email, password }, 200);
}

async function createCenter(api, base, data) {
  return (await accountPost(api, base, "/api/account/centers", data, 201)).center;
}

async function createAccount(api, base, data) {
  const { user } = await accountPost(api, base, "/api/account/users", data, 201);
  return { ...user, password: data.password };
}

async function withIsolatedMeteo(check, { stations = [], accounts = false } = {}) {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "meteo-acceptance-"));
  const env = {
    ...process.env, METEO_DATABASE_URL: "", METEO_OBSERVATION_DB: path.join(temporary, "observations.sqlite3"),
    METEO_PUBLIC_ORIGIN: "", PYTHONDONTWRITEBYTECODE: "1",
  };
  let server;
  let lines;
  let stopped;
  let logs = "";
  try {
    const globalAdmin = accounts ? bootstrapAdmin(env) : null;
    server = spawn(process.env.PYTHON || "python3", ["-u", "-c", bootstrap, JSON.stringify(stations)], {
      cwd: root, env, stdio: ["ignore", "pipe", "pipe"],
    });
    lines = readline.createInterface({ input: server.stdout });
    stopped = once(server, "close");
    server.stderr.on("data", chunk => { logs = (logs + chunk).slice(-10000); });
    const port = await Promise.race([
      once(lines, "line", { signal: AbortSignal.timeout(10000) }).then(([line]) => line),
      stopped.then(([code]) => { throw new Error(`Тестовый сервер завершился с кодом ${code}`); }),
    ]);
    assert.match(port, /^\d+$/);
    return await check(`http://127.0.0.1:${port}`, { globalAdmin, temporary });
  } catch (error) {
    if (logs) console.error(logs.trim());
    throw error;
  } finally {
    if (server?.exitCode === null && server.signalCode === null) server.kill();
    if (stopped) await stopped.catch(() => {});
    lines?.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

module.exports = { withIsolatedMeteo, bootstrapAdmin, loginAccount, createCenter, createAccount };
