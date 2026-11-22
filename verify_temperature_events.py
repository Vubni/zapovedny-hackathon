"""Сверка переходов температуры с исходным ZIP и граничных условий расчёта."""

import argparse
import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen
from zipfile import ZipFile

from analytics import STATIONS, archive_rows
from server import Handler, ROOT, ready
from temperature_events import export_events, inspect_days, query_events
from verify_analysis import ARCHIVE, compare_fields, covered, equal, mean, read_values


def synthetic_cases():
    cases = [
        ("Смена в обе стороны", [-2, 1, -1], [(2, "warming", []), (3, "cooling", [])]),
        ("Несколько нулевых суток", [-1, 0, 0, 2], [(4, "warming", [2, 3])]),
        ("Касание нуля", [1, 0, 2], []),
        ("Начальные нули", [0, 0, 2], []),
        ("Заключительный ноль", [-1, 0], []),
        ("Непригодный день", [-1, "invalid", 1], []),
        ("Пропущенная строка", [1, "absent", -1], []),
        ("Ноль и разрыв", [1, 0, "invalid", -1], []),
        ("Непригодное значение", [-1, float("inf"), 1], []),
        ("Двоичное округление", [-1, 1e-12, 2], [(3, "warming", [2])]),
        ("Малое ненулевое среднее", [-1, 2e-12], [(2, "warming", [])]),
    ]
    start = date(2024, 1, 1)
    for label, values, expected in cases:
        days = [
            {"day": date(2024, 1, day).isoformat(), "mean": value if value != "invalid" else 2,
             "valid": value != "invalid"}
            for day, value in enumerate(values, 1) if value != "absent"
        ]
        counts, events = inspect_days("kr1", days, start, date(2024, 1, len(values)))
        actual = [(int(row["day"][-2:]), row["direction"], [int(day[-2:]) for day in row["zero_days"]])
                  for row in events]
        equal(label, actual, expected)
        equal(label + " / дни по календарю", counts["calendar_days"], len(values))
    return len(cases)


def archive_reference():
    """Compare successive nonzero dates in independently filtered ZIP readings."""
    by_station = {}
    with ZipFile(ARCHIVE) as archive:
        for station, (_, first, last, _) in STATIONS.items():
            daily = {}
            zeros = set()
            for year in range(first, last + 1):
                for day, observations in read_values(archive, station, year, ("t_air",))["t_air"].items():
                    if covered(observations, station):
                        values = [value for _, value in observations]
                        daily[day] = mean(values)
                        if sum(Decimal(str(value)) for value in values) == 0:
                            zeros.add(day)
            anchors = sorted(daily.keys() - zeros)
            events = []
            for previous_day, day in zip(anchors, anchors[1:]):
                left, right = daily[previous_day], daily[day]
                if (left < 0) == (right < 0):
                    continue
                middle = [previous_day + timedelta(days=offset)
                          for offset in range(1, (day - previous_day).days)]
                if any(item not in zeros for item in middle):
                    continue
                events.append({
                    "station": station, "previous_day": previous_day.isoformat(), "previous_mean_c": left,
                    "day": day.isoformat(), "mean_c": right, "direction": "cooling" if right < 0 else "warming",
                    "zero_days": [item.isoformat() for item in middle],
                })
            calendar_count = (date(last, 12, 31) - date(first, 1, 1)).days + 1
            by_station[station] = {
                "events": events, "calendar_days": calendar_count, "valid_days": len(daily),
                "invalid_days": calendar_count - len(daily), "zero_days": len(zeros),
                "valid_pairs": sum(day + timedelta(days=1) in daily for day in daily),
                "warming": sum(row["direction"] == "warming" for row in events),
                "cooling": sum(row["direction"] == "cooling" for row in events),
            }
    return by_station


def check_archive(result):
    expected = archive_reference()
    for summary in result["stations"]:
        station = summary["station"]
        reference = expected[station]
        observed = [row for row in result["events"] if row["station"] == station]
        equal(station + " / число переходов", len(observed), len(reference["events"]))
        for actual, row in zip(observed, reference["events"]):
            compare_fields(f"{station} / {row['day']}", actual, row)
        compare_fields(station + " / покрытие", summary,
                       {key: value for key, value in reference.items() if key != "events"})


def check_windows():
    april = query_events({"start": "2024-04-01", "end": "2024-04-30"})
    equal("Апрель / переходы", [(row["station"], row["day"], row["direction"]) for row in april["events"]], [
        ("kr1", "2024-04-04", "cooling"), ("kr2", "2024-04-04", "cooling"),
        ("kr1", "2024-04-05", "warming"), ("kr2", "2024-04-06", "warming"),
    ])
    equal("АМЕ-60 / период вне архива", april["stations"][0]["calendar_days"], 0)
    equal("АМЕ-60 / начало периода вне архива", april["stations"][0]["start"], None)
    am = {"station": "ame60", "start": "2014-12-12", "end": "2014-12-14"}
    across_zero = query_events(am)
    equal("АМЕ-60 / переход через нулевые сутки", across_zero["events"][0]["zero_days"], ["2014-12-13"])
    equal("АМЕ-60 / граница включена", across_zero["events"][0]["day"], am["end"])
    zero_samples = [Decimal(row["t_air"]) for _, row in archive_rows("ame60", 2014)
                    if row["datetime"].startswith("2014-12-13")]
    equal("АМЕ-60 / исходных измерений 13 декабря", len(zero_samples), 96)
    equal("АМЕ-60 / сумма десятичных значений за 13 декабря", sum(zero_samples), Decimal("0"))
    equal("АМЕ-60 / левая граница", query_events({**am, "start": "2014-12-13"})["events"], [])
    equal("АМЕ-60 / правая граница", query_events({**am, "end": "2014-12-13"})["events"], [])
    equal("АМЕ-60 / один день", query_events({**am, "start": am["end"]})["events"], [])
    zeros_before_gap = query_events({"station": "kr2", "start": "2023-01-17", "end": "2023-01-30"})
    summary, = zeros_before_gap["stations"]
    equal("Кр2 / пять нулевых дней", summary["zero_days"], 5)
    equal("Кр2 / непригодные сутки", summary["invalid_days"], 8)
    equal("Кр2 / нули перед разрывом", zeros_before_gap["events"], [])
    return {
        "april_2024": april,
        "ame60_across_zero": across_zero,
        "ame60_zero_sample_count": len(zero_samples),
        "kr2_zeros_before_gap": zeros_before_gap,
    }


def check_http(values):
    class QuietHandler(Handler):
        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def get(path):
            return urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)

        try:
            for resource in ("/events", "/events.html", "/events.js"):
                with get(resource) as response:
                    equal(resource, response.status, 200)
            with get("/api/events?start=2024-04-01&end=2024-04-30") as response:
                equal("HTTP / JSON", json.load(response), values["april_2024"])
            with get("/api/events/export?start=2024-04-01&end=2024-04-30") as response:
                exported = response.read()
                equal("HTTP / CSV", exported, export_events(values["april_2024"]))
                equal("HTTP / CSV Content-Type", response.headers.get_content_type(), "text/csv")
                equal("HTTP / CSV Content-Disposition", response.headers["Content-Disposition"],
                      'attachment; filename="temperature_crossings_all_2024-04-01_2024-04-30.csv"')
                rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig")), delimiter=";"))
                for row, event in zip(rows, values["april_2024"]["events"], strict=True):
                    equal("HTTP / CSV станция", row["Станция"], event["station"])
                    equal("HTTP / CSV дата", row["Дата перехода"], event["day"])
                    equal("HTTP / CSV среднее", float(row["Среднее после перехода (°C)"].replace(",", ".")),
                          event["mean_c"])
            for path in (
                "/api/events?station=unknown", "/api/events?start=2024-02-30",
                "/api/events?start=", "/api/events?start=2024-1-01",
                "/api/events?start=2025-01-01&end=2024-12-31",
            ):
                try:
                    with get(path) as response:
                        equal(path, response.status, 400)
                except HTTPError as error:
                    with error:
                        equal(path, error.code, 400)
            for day in ("2014-12-12", "2014-12-13", "2014-12-14"):
                with get(f"/api/raw?station=ame60&param=t_air&day={day}") as response:
                    raw = json.load(response)
                    equal("HTTP / исходный день", raw["day"], day)
                    equal("HTTP / пригодность исходного дня", raw["valid"], True)
                    equal("HTTP / исходных сроков", raw["n"], 96)
        finally:
            server.shutdown()
            worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-results", action="store_true", help="Обновить CSV и JSON в results/research")
    args = parser.parse_args()
    ready()
    n_synthetic = synthetic_cases()
    result = query_events({})
    check_archive(result)
    examples = check_windows()
    check_http(examples)
    summary = {key: value for key, value in result.items() if key != "events"}
    summary.update(total_events=len(result["events"]), examples=examples)
    if args.write_results:
        output = ROOT / "results" / "research"
        output.mkdir(parents=True, exist_ok=True)
        (output / "temperature-crossings.csv").write_bytes(export_events(result))
        (output / "temperature-crossings-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Проверены {n_synthetic} синтетических сценариев, HTTP и {summary['total_events']} переходов из архива.")
    for station in result["stations"]:
        print(f"{station['label']}: {station['events']} переходов "
              f"({station['warming']} в плюс, {station['cooling']} в минус), "
              f"валидных суток {station['valid_days']} из {station['calendar_days']}.")


if __name__ == "__main__":
    main()
