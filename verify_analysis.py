"""Verify the journal's derived statistics directly against the archive."""

import calendar
import cmath
import csv
import io
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import analytics


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "data" / "meteo_hack_dataset.zip"
REPORT = ROOT / "results" / "analysis-checks.json"


def equal(label, actual, expected):
    if isinstance(expected, float):
        match = actual is not None and math.isclose(actual, expected, abs_tol=1e-10, rel_tol=1e-12)
    else:
        match = actual == expected
    if not match:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def compare_fields(label, actual, expected):
    for key, value in expected.items():
        equal(f"{label} / {key}", actual[key], value)


def calendar_days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def month_days(year, month):
    return calendar_days(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))


def read_values(archive, station, year, parameters):
    suffix = f"/{station}_{year}.csv"
    result = {param: defaultdict(list) for param in parameters}
    names = [name for name in archive.namelist() if name.endswith(suffix)]
    if not names:
        return result
    equal(suffix, len(names), 1)
    with io.TextIOWrapper(archive.open(names[0]), encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for record in reader:
            flags = record["qc_flag"].split(";")
            if "dup_conflict" in flags:
                continue
            stamp = datetime.fromisoformat(record["datetime"])
            flagged = {flag.split(":")[0] for flag in flags}
            for param in parameters:
                value = record.get(param)
                if value and param not in flagged:
                    result[param][stamp.date()].append((stamp, float(value)))
    return result


def mean(values):
    return math.fsum(values) / len(values)


def covered(samples, station):
    if station == "ame60":
        return len({stamp.time() for stamp, _ in samples}) >= 72
    return len({stamp.hour for stamp, _ in samples}) >= 18


def scalar_days(observations, station):
    daily = {}
    for day, samples in observations.items():
        if covered(samples, station):
            numbers = [value for _, value in samples]
            daily[day] = {"mean": mean(numbers), "min": min(numbers), "max": max(numbers)}
    return daily


def periods(days, available):
    missing = [day for day in days if day not in available]
    current = longest = 0
    previous = None
    for day in missing:
        current = current + 1 if previous and (day - previous).days == 1 else 1
        longest = max(current, longest)
        previous = day
    return len(days) - len(missing), longest, len(missing) <= 10 and longest < 5


def hourly_means(samples):
    hours = defaultdict(list)
    for stamp, value in samples:
        hours[stamp.hour].append(value)
    return {hour: mean(values) for hour, values in hours.items()}


def paired_temperatures(left, right):
    return {
        "pairs": len(left),
        "kr1": mean(left) if left else None,
        "kr2": mean(right) if right else None,
        "delta": mean([a - b for a, b in zip(left, right)]) if left else None,
    }


def verify_comparison(raw):
    daily = {station: scalar_days(values["t_air"], station) for station, values in raw.items()}
    days = sorted(day for day in daily["kr1"] if day in daily["kr2"])
    month_rows = []
    for month in range(1, 13):
        dates = [day for day in days if day.month == month]
        _, gap, valid = periods(month_days(2025, month), set(dates))
        temperatures = [
            [daily[station][day]["mean"] for day in dates] if valid else []
            for station in ("kr1", "kr2")
        ]
        month_rows.append({
            "month": f"2025-{month:02d}", **paired_temperatures(*temperatures),
            "pairs": len(dates), "longest_gap": gap,
        })

    shared_hours = defaultdict(list)
    for day in days:
        left, right = (hourly_means(raw[station]["t_air"][day]) for station in ("kr1", "kr2"))
        for hour in range(24):
            if hour in left and hour in right:
                shared_hours[hour].append((left[hour], right[hour]))
    hour_rows = []
    for hour in range(24):
        pairs = shared_hours[hour]
        left, right = zip(*pairs) if pairs else ([], [])
        hour_rows.append({"hour": hour, **paired_temperatures(left, right)})

    result = analytics.comparison()
    equal("comparison year", result["year"], 2025)
    equal("comparison month rows", len(result["months"]), 12)
    equal("comparison hour rows", len(result["hours"]), 24)
    for got, expected in zip(result["months"], month_rows):
        compare_fields(expected["month"], got,
                       {key: value for key, value in expected.items() if key != "longest_gap"})
    for got, expected in zip(result["hours"], hour_rows):
        compare_fields(f"comparison hour {expected['hour']}", got, expected)
    return {"common_days": len(days), "common_hours": sum(row["pairs"] for row in hour_rows),
            "months": month_rows, "hours": hour_rows}


def source_monthly(daily):
    monthly = {}
    for year in range(1992, 2022):
        for month in range(1, 13):
            dates = month_days(year, month)
            _, _, valid = periods(dates, daily)
            if valid:
                values = [daily[day]["mean"] for day in dates if day in daily]
                monthly[year, month] = mean(values)
    return monthly


def independent_slope(strata):
    # Within each season, sum every pair's difference products and squared
    # year differences. Dividing by group size gives centered sums for OLS.
    numerator = denominator = 0.0
    count = 0
    for points in strata.values():
        n = len(points)
        if n < 3:
            continue
        pairs = list(combinations(points, 2))
        numerator += math.fsum((a - b) * (x - y) for (a, x), (b, y) in pairs) / n
        denominator += math.fsum((a - b) ** 2 for (a, _), (b, _) in pairs) / n
        count += n
    return (numerator / denominator if denominator else None), count


def verify_temperature_analysis(daily):
    start, end = date(1993, 2, 7), date(2020, 9, 20)
    query = {"station": "ame60", "param": "t_air", "grain": "month",
             "start": start.isoformat(), "end": end.isoformat(), "base_start": 1992, "base_end": 2006}
    got = analytics.analyze(query)
    by_month = {r["period"]: r for r in got["rows"]}
    source = source_monthly(daily)
    strata = defaultdict(list)
    for (year, month), value in source.items():
        dates = month_days(year, month)
        if start <= dates[0] and dates[-1] <= end:
            strata[month].append((year, value))
    slope, count = independent_slope(strata)
    compare_fields("AME-60 slope", got["stats"], {"trend": slope, "trend_n": count, "trend_unit": "°C/год"})

    reference = []
    for year, month in ((2010, 1), (2010, 6), (2016, 9)):
        reference_years = [y for y in range(1992, 2007) if (y, month) in source]
        baseline = mean([source[y, month] for y in reference_years])
        value = source[year, month]
        expected = {"mean": value, "baseline": baseline, "anomaly": value - baseline,
                    "years": len(reference_years)}
        label = f"{year}-{month:02d}"
        compare_fields(label, by_month[label], expected)
        reference.append({"month": label, "reference_years": reference_years, **expected})
    return {"query": query, "slope_c_per_year": slope, "valid_months_in_slope": count,
            "strata": {m: len(points) for m, points in sorted(strata.items())}, "references": reference}


def verify_periods(grain, intervals, daily, raw):
    result = analytics.analyze({
        "station": "ame60", "param": "t_air", "grain": grain,
        "start": intervals[0][0], "end": intervals[-1][1],
    })
    equal(f"{grain} periods returned", len(result["rows"]), len(intervals))
    expected_rows = []
    used_days = 0
    for row, (start, end, whole) in zip(result["rows"], intervals):
        dates = calendar_days(date.fromisoformat(start), date.fromisoformat(end))
        accepted = [day for day in dates if day in daily]
        n, gap, passes_month = periods(dates, daily)
        valid = whole and bool(accepted) and (grain != "month" or passes_month)
        expected = {
            "start": start, "end": end, "days": len(dates), "valid_days": n,
            "valid": valid, "longest_gap": gap,
            "n": sum(raw.get(day, 0) for day in dates),
            "mean": mean([daily[day]["mean"] for day in accepted]) if valid else None,
            "min": min(daily[day]["min"] for day in accepted) if valid else None,
            "max": max(daily[day]["max"] for day in accepted) if valid else None,
        }
        if not whole:
            equal(f"{grain} clip reason {start}", "фрагмент" in row["reason"], True)
        if valid:
            used_days += n
        compare_fields(f"{grain} / {start}", row, expected)
        expected_rows.append(expected)
    equal(f"{grain} used days", result["stats"]["used_days"], used_days)
    return {"rows": expected_rows, "used_days": used_days}


def verify_all_invalid(archive):
    # The outage is confirmed directly from the absent station-year member.
    outage = read_values(archive, "ame60", 2001, ("t_air",))
    equal("absence of 2001 member observations", len(outage["t_air"]), 0)
    result = analytics.analyze({
        "station": "ame60", "param": "t_air", "grain": "month",
        "start": "2001-02-01", "end": "2001-03-31",
    })
    equal("outage month count", len(result["rows"]), 2)
    empty = {"mean": None, "min": None, "max": None, "n": 0, "valid_days": 0, "flagged": 0}
    for row, month in zip(result["rows"], ("2001-02", "2001-03")):
        days = len(month_days(2001, int(month[-2:])))
        compare_fields(f"outage {month}", row, {**empty, "period": month, "days": days,
                       "longest_gap": days, "valid": False, "anomaly": None, "conflicts": 0, "missing": 0})
    expected = {**empty, "days": 59, "used_days": 0, "valid_periods": 0, "periods": 2,
                "trend": None, "trend_n": 0}
    compare_fields("outage totals", result["stats"], expected)
    return {"start": result["start"], "end": result["end"], **expected}


def angle(values):
    vector = sum(cmath.rect(1, math.radians(value)) for value in values)
    return math.degrees(cmath.phase(vector)) % 360


def verify_direction(raw):
    candidates = []
    for station, values in raw.items():
        for day, samples in values["wind_dir"].items():
            readings = [value for _, value in samples]
            if covered(samples, station) and min(readings) < 30 and max(readings) > 330:
                expected = angle(readings)
                candidates.append((min(expected, 360 - expected), station, day, expected, readings))
    _, station, day, expected, readings = min(candidates)
    stamp = day.isoformat()
    result = analytics.analyze({
        "station": station, "param": "wind_dir", "grain": "day", "start": stamp, "end": stamp,
    })
    compare_fields("observed direction around north", result["rows"][0], {
        "mean": expected, "min": None, "max": None, "n": len(readings), "valid": True,
    })
    compare_fields("wind trend withheld", result["stats"], {"trend": None, "trend_n": 0})

    north = analytics.mean_of([359, 1], "wind_dir")
    equal("synthetic direction wrap at North / canonical degrees", north, 0.0)
    equal("synthetic opposite direction / undefined mean", analytics.mean_of([0, 180], "wind_dir"), None)
    delta = analytics.difference(2, 358, "wind_dir")
    equal("short circular anomaly", delta, 4.0)
    equal("reverse circular anomaly", analytics.difference(358, 2, "wind_dir"), -4.0)
    return {"observed": {"station": station, "day": stamp, "n": len(readings),
                         "min_reading": min(readings), "max_reading": max(readings),
                         "arithmetic_mean": mean(readings), "circular_mean": expected},
            "north_fixture": {"degrees": [359, 1], "mean": north},
            "opposite_fixture": {"degrees": [0, 180], "mean": None},
            "anomaly_fixture": {"degrees": 2, "reference": 358, "delta": delta}}


def main():
    with ZipFile(ARCHIVE) as archive:
        modern = {station: read_values(archive, station, 2025, ("t_air", "wind_dir"))
                  for station in ("kr1", "kr2")}
        daily, raw_2016 = {}, {}
        for year in range(1992, 2022):
            observations = read_values(archive, "ame60", year, ("t_air",))["t_air"]
            daily.update(scalar_days(observations, "ame60"))
            if year == 2016:
                raw_2016 = {day: len(samples) for day, samples in observations.items()}
        results = {
            "archive": ARCHIVE.name,
            "comparison_2025": verify_comparison(modern),
            "temperature_ame60": verify_temperature_analysis(daily),
            "clipped_dekads": verify_periods("dekad", [
                ("2016-02-05", "2016-02-10", False), ("2016-02-11", "2016-02-20", True),
                ("2016-02-21", "2016-02-29", True), ("2016-03-01", "2016-03-10", True),
                ("2016-03-11", "2016-03-20", True), ("2016-03-21", "2016-03-24", False),
            ], daily, raw_2016),
            "clipped_months": verify_periods("month", [
                ("2016-01-08", "2016-01-31", False), ("2016-02-01", "2016-02-29", True),
                ("2016-03-01", "2016-03-24", False),
            ], daily, raw_2016),
            "all_invalid": verify_all_invalid(archive),
            "wind_direction": verify_direction(modern),
        }
    with TemporaryDirectory(prefix=".analysis-checks-", dir=REPORT.parent) as temporary:
        staged = Path(temporary) / REPORT.name
        staged.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(REPORT)
    print(f"Проверены 12 месяцев, 24 часа, тренд, 3 отклонения, границы периодов и ветер. {REPORT}")


if __name__ == "__main__":
    main()
