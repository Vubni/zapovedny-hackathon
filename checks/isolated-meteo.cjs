const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const bootstrap = `
import json
import sys
from checks.test_observation_workflow import SERVER
from station_catalog import register_station

for station in json.loads(sys.argv[1]):
    register_station(station)
exec(SERVER)
`;

async function withIsolatedMeteo(check, { stations = [] } = {}) {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "meteo-acceptance-"));
  const server = spawn(process.env.PYTHON || "python3", [
    "-u", "-c", bootstrap, JSON.stringify(stations),
  ], {
    cwd: path.join(__dirname, ".."),
    env: {
      ...process.env, METEO_DATABASE_URL: "", METEO_OBSERVATION_DB: path.join(temporary, "observations.sqlite3"),
      YANDEX_MAPS_BROWSER_KEY: "", PYTHONDONTWRITEBYTECODE: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const lines = readline.createInterface({ input: server.stdout });
  const stopped = once(server, "close");
  let logs = "";
  server.stderr.on("data", chunk => { logs = (logs + chunk).slice(-10000); });
  try {
    const port = await Promise.race([
      once(lines, "line", { signal: AbortSignal.timeout(10000) }).then(([line]) => line),
      stopped.then(([code]) => { throw new Error(`Тестовый сервер завершился с кодом ${code}`); }),
    ]);
    assert.match(port, /^\d+$/);
    return await check(`http://127.0.0.1:${port}`);
  } catch (error) {
    if (logs) console.error(logs.trim());
    throw error;
  } finally {
    if (server.exitCode === null && server.signalCode === null) server.kill();
    await stopped.catch(() => {});
    lines.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

module.exports = { withIsolatedMeteo };
