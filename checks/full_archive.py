"""Проверить полный 30-летний ряд через HTTP, CSV и независимый календарь."""
import argparse
import csv
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}/api"
    opener = build_opener(ProxyHandler({}))
    query = dict(station="ame60",param="t_air",grain="day",start="1992-01-01",end="2021-12-31")

    def get(route):
        start = perf_counter()
        with opener.open(base + route + "?" + urlencode(query), timeout=45) as response:
            assert response.status == 200
            body = response.read()
        return body, round(1000 * (perf_counter()-start), 2)

    first, last = map(date.fromisoformat, (query["start"],query["end"]))
    calendar = [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
    with (ROOT / "results/audit/parameter_months.csv").open() as src:
        independent = {r["month"]:int(r["valid_days"]) for r in csv.DictReader(src)
                       if r["station"]=="ame60" and r["param"]=="t_air"}
    timings = []
    digest = None
    for _ in range(3):
        body, ms = get("/series")
        signature = hashlib.sha256(body).hexdigest()
        assert digest is None or signature == digest
        digest = signature
        timings.append(ms)
    rows = json.loads(body)["rows"]
    assert [r["start"] for r in rows] == calendar
    valid_per_month = {month:0 for month in independent}
    for r in rows:
        valid_per_month[r["start"][:7]] += r["valid"]
    assert valid_per_month == independent
    export, csv_ms = get("/export")
    spreadsheet = list(csv.DictReader(io.StringIO(export.decode("utf-8-sig")),delimiter=";"))
    assert len(spreadsheet) == len(calendar)
    decimals_checked = 0
    for source, record in zip(rows, spreadsheet):
        assert source["period"] == record["Период"]
        assert record["Период принят"] == ("да" if source["valid"] else "нет")
        value = record["Среднее"]
        if source["mean"] is None:
            assert not value
        else:
            assert abs(source["mean"]-float(value.replace(",", "."))) < 1e-9
            decimals_checked += 1

    report = dict(query=query,calendar_days=len(calendar),verification_months=len(independent),
                  valid_daily_means_checked=decimals_checked,missing_daily_means=len(calendar)-decimals_checked,
                  json_bytes=len(body),json_sha256=digest,json_requests_ms=timings,json_median_ms=median(timings),
                  csv_bytes=len(export),csv_sha256=hashlib.sha256(export).hexdigest(),csv_request_ms=csv_ms,
                  note="Замеры чтения ответов локального сервера на этой машине. Время выполнения работы специалистом отдельно не измерялось.")
    dest = ROOT/"results"/"full-archive-http.json"
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__ == "__main__":
    main()
