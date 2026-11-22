"""Проверить запущенный локальный API и записать реальные примеры для команды."""
import argparse
import csv
import io
import json
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from replay_day import compare, replay

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "examples"


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    opener = build_opener(ProxyHandler({}))
    checks = []
    OUT.mkdir(parents=True, exist_ok=True)

    def request(path, params=None, status=200):
        target = path + ("?" + urlencode(params) if params else "")
        t0 = perf_counter()
        try:
            response = opener.open(base + target, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            assert response.status == status, (target, response.status, payload[:200])
            checks.append({"request":target,"status":status,"bytes":len(payload),
                           "elapsed_ms":round(1000*(perf_counter()-t0),1)})
            if response.headers.get_content_type() == "application/json":
                return json.loads(payload)
            return payload

    meta = request("/api/meta")
    assert [s["id"] for s in meta["stations"]] == ["ame60","kr1","kr2"]
    save("stations.json", meta)
    q = dict(station="kr1",param="pressure_hpa",start="2024-01-01",end="2024-12-31",grain="month")
    yearly = request("/api/series",q)
    s = yearly["stats"]
    assert (s["valid_days"],s["used_days"],s["valid_periods"],s["flagged"]) == (334,332,11,1362)
    april = yearly["rows"][3]
    assert (april["mean"],april["valid_days"],april["longest_gap"],april["flagged"]) == (None,2,28,1272)
    save("kr1-pressure-2024.json", yearly)

    april_query = dict(q,start="2024-04-01",end="2024-04-30")
    day_rows = request("/api/series",dict(april_query,grain="day"))
    assert len(day_rows["rows"]) == 30
    save("kr1-pressure-april-days.json",day_rows)
    assert request("/api/series",dict(april_query,param="t_air"))["rows"][0]["valid_days"] == 28
    assert request("/api/series",dict(april_query,station="kr2"))["rows"][0]["valid_days"] == 30
    for day, name, expected in [("01","valid",(38,19,True)),("29","incomplete",(31,15,False))]:
        raw = request("/api/raw",dict(station="kr1",param="pressure_hpa",day=f"2024-04-{day}",download=1))
        assert (raw["n"],raw["slots"],raw["valid"]) == expected
        compare(raw,replay(raw)[0])
        if day == "01":
            assert raw["signals"][0]["code"] == "constant_pressure" and raw["signals"][0]["n"] == 38
        save(f"kr1-pressure-april-{day}-{name}.json",raw)
    neighbor = request("/api/raw",dict(station="kr2",param="pressure_hpa",day="2024-04-01"))
    assert neighbor["signals"] == [] and len({r["value"] for r in neighbor["rows"]}) == 39
    save("kr2-pressure-april-01.json", neighbor)
    raw = request("/api/raw",dict(station="ame60",param="wind_dir",day="1993-04-02"))
    compare(raw,replay(raw)[0])
    assert sum("dup_conflict" in r["flags"] for r in raw["rows"]) == 2
    export = request("/api/export",q)
    rows = list(csv.DictReader(io.StringIO(export.decode("utf-8-sig")),delimiter=";"))
    assert len(rows)==12 and rows[3]["Среднее"]=="" and rows[3]["Флаги параметра"]=="1272"
    (OUT/"kr1-pressure-2024.csv").write_bytes(export)

    comp = request("/api/compare")
    assert len(comp["months"])==12 and len(comp["hours"])==24
    assert sum(r["pairs"] for r in comp["hours"])==8498
    save("temperature-comparison-2025.json",comp)
    for bad in [dict(q,station="../kr1"),dict(q,grain="year"),dict(q,param="soil_05"),dict(q,start="2024-02-30"),
                dict(q,start="2025-01-01"),dict(q,base_start="2021"),dict(q,base_end="bad")]:
        assert request("/api/series",bad,400)["error"]
    assert request("/missing",status=404)["error"]
    save("http-checks.json",{"passed_requests":len(checks),"requests":checks,
                             "note":"Время одного локального прогона, миллисекунды."})
    print(f"{len(checks)} запросов и содержательные проверки пройдены. Примеры в {OUT}")


if __name__ == "__main__":
    main()
