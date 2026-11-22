"""Server-side chart data for the meteorological archive and confirmed observations."""

import calendar
import csv
import io
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from itertools import groupby
from statistics import fmean
from zipfile import ZipFile

import analytics
import build_data
import observation_store
import station_catalog


@dataclass(frozen=True)
class Chart:
    title: str
    unit: str
    fields: tuple
    kind: str = "line"
    axis: str = "time"


CHARTS = {
    "air": Chart("Средняя температура воздуха", "°C", (("t_air", "mean"),)),
    "air_range": Chart("Диапазон температуры воздуха", "°C", (("t_air", "min"), ("t_air", "max"))),
    "air_anomaly": Chart("Отклонение температуры воздуха", "°C", (("t_air", "anomaly"),)),
    "air_trend": Chart("Тренд температуры воздуха", "°C", (("t_air", "trend"),)),
    "soil_05": Chart("Температура почвы · 5 см", "°C", (("soil_05", "mean"),)),
    "soil_10": Chart("Температура почвы · 10 см", "°C", (("soil_10", "mean"),)),
    "soil_20": Chart("Температура почвы · 20 см", "°C", (("soil_20", "mean"),)),
    "rh": Chart("Средняя относительная влажность", "%", (("rh", "mean"),)),
    "rh_range": Chart("Диапазон относительной влажности", "%", (("rh", "min"), ("rh", "max"))),
    "wind_speed": Chart("Средняя скорость ветра", "м/с", (("wind_speed", "mean"),)),
    "wind_gust": Chart("Максимальные порывы ветра", "м/с", (("wind_gust", "max"),)),
    "wind_combined": Chart("Скорость и порывы ветра", "м/с", (("wind_speed", "mean"), ("wind_gust", "max"))),
    "wind_rose": Chart("Роза ветров", "%", (("wind_dir", "frequency"),), "rose", "direction"),
    "wind_freq": Chart("Повторяемость направлений ветра", "%", (("wind_dir", "frequency"),), "rose", "direction"),
    "calm": Chart("Доля штиля", "%", (("wind_speed", "calm"),)),
    "pressure": Chart("Среднее атмосферное давление", "гПа", (("pressure_hpa", "mean"),)),
    "pressure_range": Chart("Диапазон атмосферного давления", "гПа",
                            (("pressure_hpa", "min"), ("pressure_hpa", "max"))),
    "temp_difference": Chart("Разница температур · Кр2 − Кр1", "°C", (("t_air", "difference"),)),
    "temp_diurnal": Chart("Средний суточный ход температуры", "°C", (("t_air", "mean"),), axis="hour"),
}
ROLES = {"min": "минимум", "max": "максимум", "trend": "тренд"}
GRAINS = {"day", "decade", "month", "year"}
MODES = {"modern", "togu", "semenov"}
MAX_POINTS = 200_000
MAX_INTERVALS = 12
PAIR = ("kr1", "kr2")
DIFFERENCE = {"id": "kr2-kr1", "label": "Кр2 − Кр1", "color": "#a74937"}
CALM_LIMIT = 0.5
DIRECTIONS = ("С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ",
              "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ")
SPEED_CLASSES = (
    (2, "0,5–<2 м/с"), (4, "2–<4 м/с"), (6, "4–<6 м/с"),
    (8, "6–<8 м/с"), (11, "8–<11 м/с"), (math.inf, "≥11 м/с"),
)


@dataclass(frozen=True)
class Period:
    start: str
    end: str
    first: str
    last: str

    @property
    def full(self):
        return (self.start, self.end) == (self.first, self.last)

    @property
    def length(self):
        return (date.fromisoformat(self.last) - date.fromisoformat(self.first)).days + 1


@dataclass(frozen=True)
class Daily:
    n: int
    mean: float = None
    minimum: float = None
    maximum: float = None

    @property
    def valid(self):
        return self.mean is not None


def parse_day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (TypeError, ValueError):
        pass
    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД")


def calendar_days(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [date.fromordinal(day).isoformat() for day in range(first.toordinal(), last.toordinal() + 1)]


def period_budget(start, end, grain, series_count):
    interval_budget(((start, end),), grain, series_count)


def period_count(start, end, grain):
    month_count = (end.year - start.year) * 12 + end.month - start.month + 1
    counts = {"day": (end - start).days + 1, "decade": month_count * 3,
              "month": month_count, "year": end.year - start.year + 1}
    return counts[grain]


def interval_budget(intervals, grain, series_count):
    # This protects the JSON response without discarding individual points or altering the date filter.
    if sum(period_count(start, end, grain) for start, end in intervals) * series_count > MAX_POINTS:
        raise ValueError("Слишком много точек для одного графика. Выберите более крупную группировку или меньше станций.")


def periods(start, end, grain):
    first = start
    while first <= end:
        if grain == "year":
            lo, hi = first[:4] + "-01-01", first[:4] + "-12-31"
        else:
            lo, hi = analytics.bounds(first, "dekad" if grain == "decade" else grain)
        last = min(hi, end)
        yield Period(lo, hi, first, last)
        if last == end:
            break
        first = (date.fromisoformat(last) + timedelta(days=1)).isoformat()


def station_params(station):
    return {param["id"] for param in station["params"]}


def query_dates(query):
    first = parse_day(query.get("start", "2025-01-01"))
    last = parse_day(query.get("end", "2025-12-31"))
    if first > last:
        raise ValueError("Начало периода должно предшествовать его окончанию")
    return first, last


def query_intervals(query):
    """Return distinct non-adjacent inclusive ranges, ordered by start date."""
    if "intervals" not in query:
        return (query_dates(query),)
    raw = query["intervals"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Укажите интервалы в формате ГГГГ-ММ-ДД:ГГГГ-ММ-ДД через запятую")
    parts = raw.split(",", MAX_INTERVALS)
    if len(parts) > MAX_INTERVALS:
        raise ValueError(f"Можно выбрать до {MAX_INTERVALS} интервалов")
    ranges = []
    for part in parts:
        dates = part.strip().split(":")
        if len(dates) != 2:
            raise ValueError("Укажите интервалы в формате ГГГГ-ММ-ДД:ГГГГ-ММ-ДД через запятую")
        first, last = map(parse_day, dates)
        if first > last:
            raise ValueError("Начало периода должно предшествовать его окончанию")
        ranges.append((first, last))

    merged = []
    for first, last in sorted(ranges):
        if merged and (first - merged[-1][1]).days <= 1:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return tuple(merged)


def interval_details(first, last):
    start, end = first.isoformat(), last.isoformat()
    label = f"{first.day:02}.{first.month:02}.{first.year:04}"
    if first != last:
        label += f" — {last.day:02}.{last.month:02}.{last.year:04}"
    return {
        "id": f"{start}:{end}", "start": start, "end": end, "days": (last - first).days + 1,
        "label": label,
    }


def query_stations(query):
    station_ids = list(dict.fromkeys(name.strip() for name in query.get("stations", "").split(",") if name.strip()))
    if not station_ids:
        return []
    catalog = {station["id"]: station for station in station_catalog.catalog()}
    if set(station_ids) - catalog.keys():
        raise ValueError("Неизвестная станция")
    return [catalog[station] for station in station_ids]


def archive_rows(archive, station, year, start, end):
    member = build_data.source_member(station, year)
    try:
        stream = archive.open(member)
    except KeyError:
        return
    with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        for row in csv.DictReader(text):
            day = row["datetime"][:10]
            if day > end:
                break
            if day >= start:
                yield row


def clean_archive(row, params):
    flags = row["qc_flag"].split(";")
    if "dup_conflict" in flags:
        return None
    excluded = {flag.partition(":")[0] for flag in flags}
    values = {}
    for param in params:
        text = row.get(param, "").strip()
        if text and param not in excluded:
            values[param] = float(text)
    return values


def clean_archive_group(group, params, replacements):
    output = {}
    for row in group:
        timestamp = row["datetime"]
        values = clean_archive(row, params)
        if values is not None:
            output[timestamp] = values
    for timestamp, replacement in replacements.items():
        values = output.get(timestamp, {"synthetic": replacement.get("synthetic", False)})
        for param in replacement.get("declared", ()):
            values.pop(param, None)
        values.update((param, replacement[param]) for param in params if param in replacement)
        if replacement.get("estimates"):
            values["estimates"] = replacement["estimates"]
        output[timestamp] = values
    return output


def confirmed_day_groups(station, start, end, params, include_estimates, count_all_slots):
    def relevant(row):
        if (include_estimates and params.intersection(row.get("estimates", ()))):
            return True
        return not row.get("synthetic") and (count_all_slots or params.intersection(row.get("declared", ())))

    rows = filter(relevant, observation_store.confirmed_rows(station, start, end))
    for day, group in groupby(rows, lambda row: row["datetime"][:10]):
        yield day, {row["datetime"]: row for row in group}


def raw_days(station, start, end, params, imports_only=False, include_estimates=False, count_all_slots=False):
    """Yield one merged day at a time with confirmed measurements and explicit cell deletions."""
    station_id = station["id"]
    imported = iter(confirmed_day_groups(station_id, start, end, set(params), include_estimates, count_all_slots))
    updated = next(imported, None)
    has_archive = station_id in build_data.STATIONS
    if not has_archive:
        if updated is not None:
            yield updated
            yield from imported
        return
    if imports_only and updated is None:
        return

    def source_groups(archive, year):
        records = archive_rows(archive, station_id, year, start, end)
        return groupby(records, lambda row: row["datetime"][:10])

    first_year, last_year = int(start[:4]), int(end[:4])
    with ZipFile(analytics.ARCHIVE) as archive:
        for year in range(first_year, last_year + 1):
            if year not in build_data.STATIONS[station_id].years:
                while updated and int(updated[0][:4]) == year:
                    yield updated
                    updated = next(imported, None)
                continue
            if imports_only and (updated is None or int(updated[0][:4]) != year):
                continue

            for day, group in source_groups(archive, year):
                while updated and updated[0] < day:
                    yield updated
                    updated = next(imported, None)
                replacements = {}
                if updated and updated[0] == day:
                    replacements = updated[1]
                    updated = next(imported, None)
                elif imports_only:
                    if not updated or int(updated[0][:4]) != year:
                        break
                    continue

                yield day, clean_archive_group(group, params, replacements)

            while updated and int(updated[0][:4]) == year:
                yield updated
                updated = next(imported, None)


def summarize(values, station):
    if len({slot(time, station) for time, _ in values}) < station["minimum_slots"]:
        return Daily(len(values))
    numbers = [value for _, value in values]
    return Daily(len(numbers), fmean(numbers), min(numbers), max(numbers))


def measurement(rows, station, param):
    return summarize([(time, row[param]) for time, row in rows.items()
                      if row.get(param) is not None and not row.get("synthetic")], station)


def working_measurement(rows, station, param):
    values, methods = [], Counter()
    for time, row in rows.items():
        measured = row.get(param)
        if measured is not None and not row.get("synthetic"):
            values.append((time, measured))
            continue
        estimate = row.get("estimates", {}).get(param)
        if estimate and estimate.get("enters_working_graphs") and estimate.get("value") is not None:
            values.append((time, estimate["value"]))
            methods[estimate["method"]] += 1
    return (summarize(values, station), methods) if methods else None


def slot(timestamp, station):
    # АМЕ-60 coverage is defined by distinct original timestamps.
    minutes = 1 if station["id"] == "ame60" else station["slot_minutes"]
    return (int(timestamp[11:13]) * 60 + int(timestamp[14:16])) // minutes


def observed_slots(rows, station, param=None):
    return len({
        slot(timestamp, station) for timestamp, row in rows.items()
        if not row.get("synthetic") and (param is None or row.get(param) is not None)
    })


def load_daily(station, params, start, end, station_valid=None, working=None):
    allowed = tuple(param for param in params if param in station_params(station))
    daily = {param: {} for param in params}
    if not allowed:
        return daily
    if station["id"] in build_data.STATIONS:
        with closing(analytics.database()) as connection:
            for param in allowed:
                rows = connection.execute(
                    "SELECT day, n, mean, min, max, station_slots FROM daily "
                    "WHERE station=? AND param=? AND day BETWEEN ? AND ?",
                    (station["id"], param, start, end),
                )
                for day, n, mean, minimum, maximum, station_slots in rows:
                    if n:
                        daily[param][day] = Daily(n, mean, minimum, maximum)
                    if station_valid is not None and station_slots >= station["minimum_slots"]:
                        station_valid.add(day)
    for day, rows in raw_days(station, start, end, allowed, imports_only=True,
                              include_estimates=working is not None, count_all_slots=station_valid is not None):
        for param in allowed:
            daily[param][day] = measurement(rows, station, param)
            if working is not None:
                estimate = working_measurement(rows, station, param)
                if estimate:
                    working[param][day] = estimate
        if station_valid is not None:
            if observed_slots(rows, station) >= station["minimum_slots"]:
                station_valid.add(day)
            else:
                station_valid.discard(day)
    return daily


def group_periods(intervals, valid, valid_months, grain):
    ordered = sorted(valid)
    for period in intervals:
        good = ordered[bisect_left(ordered, period.first):bisect_right(ordered, period.last)]
        if grain == "year":
            admitted = [day for day in good if day[:7] in valid_months]
        elif grain == "month":
            admitted = good if period.start[:7] in valid_months else []
        else:
            admitted = good if period.full else []
        yield period, good, admitted


def valid_months(start, end, valid):
    output = set()
    for month in {day[:7] for day in valid}:
        lo, hi = analytics.bounds(month + "-01", "month")
        if start <= lo and hi <= end and build_data.coverage([day in valid for day in calendar_days(lo, hi)])[-1]:
            output.add(month)
    return output


def overview_slots(station, start, end, param=None):
    """Read archived daily slot counts and refresh only dates touched by published rows."""
    station_id = station["id"]
    archive = build_data.STATIONS.get(station_id)
    station_counts, parameter_counts = {}, {}
    if archive:
        archive_param = param or archive.params[0]
        with closing(analytics.database()) as connection:
            rows = connection.execute(
                "SELECT day, station_slots, slots FROM daily WHERE station=? AND param=? AND day BETWEEN ? AND ?",
                (station_id, archive_param, start, end),
            )
            for day, station_slots, parameter_slots in rows:
                station_counts[day] = station_slots
                if param:
                    parameter_counts[day] = parameter_slots
    parameters = (param,) if param else ()
    for day, rows in raw_days(station, start, end, parameters, imports_only=True, count_all_slots=True):
        station_counts[day] = observed_slots(rows, station)
        if param:
            parameter_counts[day] = observed_slots(rows, station, param)
    return station_counts, parameter_counts


def overview_periods(windows, series_count):
    grouped = {}
    for first, last in windows:
        for part in periods(first, last, "month"):
            item = grouped.setdefault(part.start, {
                "id": part.start, "start": part.first, "end": part.last, "total_days": 0, "intervals": [],
            })
            item["end"] = part.last
            item["total_days"] += part.length
            item["intervals"].append({"start": part.first, "end": part.last})
            if len(grouped) * series_count > MAX_POINTS:
                raise ValueError("Слишком много ячеек покрытия. Сократите периоды или число станций.")
    return list(grouped.values())


def coverage_cells(months, counts, minimum_slots, supported=True):
    items = []
    for month in months:
        observed_days = 0
        validity = []
        for period in month["intervals"]:
            for day in calendar_days(period["start"], period["end"]):
                count = counts.get(day, 0)
                observed_days += bool(count)
                validity.append(count >= minimum_slots)
        total, valid_days, longest_gap, admitted = build_data.coverage(validity)
        year, number = int(month["id"][:4]), int(month["id"][5:7])
        complete = total == calendar.monthrange(year, number)[1]
        if not supported:
            status = "unsupported"
        elif not observed_days:
            status = "absent"
        elif valid_days == total:
            status = "full"
        elif complete and not admitted:
            status = "excluded"
        else:
            status = "gaps"
        items.append({
            "period": month["id"], "valid_days": valid_days, "observed_days": observed_days,
            "total_days": total, "ratio": valid_days / total if supported else None, "status": status,
            "month_valid": bool(admitted) if complete and supported else None,
            "longest_gap": longest_gap if complete and supported else None,
        })
    return items


COVERAGE_LABELS = {"pressure_hpa": "Давление", "rh": "Влажность"}


def coverage_overview(query):
    """Summarize the selected calendar interval by station and coverage measure."""
    windows = query_intervals(query)
    selected_windows = [(first.isoformat(), last.isoformat()) for first, last in windows]
    start, end = selected_windows[0][0], selected_windows[-1][1]
    chart = CHARTS.get(query.get("chart"))
    chart_parameters = list(dict.fromkeys(param for param, _ in chart.fields)) if chart else []
    requested = query.get("parameter", chart_parameters[0] if chart_parameters else "all")
    if requested != "all" and requested not in analytics.PARAMS:
        raise ValueError("Выберите доступный параметр покрытия")

    stations = query_stations(query)
    available = {param for station in stations for param in station_params(station)}
    parameters = [{"id": "all", "label": "Общее покрытие"}]
    shown = available | set(chart_parameters) | {requested}
    parameters.extend({"id": param, "label": COVERAGE_LABELS.get(param, label)}
                      for param, (label, _) in analytics.PARAMS.items() if param in shown)
    result = {"start": start, "end": end, "grain": "month", "parameter": requested,
              "chart_parameters": chart_parameters,
              "parameters": parameters, "periods": [], "rows": [],
              "intervals": [{"start": lo, "end": hi} for lo, hi in selected_windows]}
    if not stations:
        return result

    result["periods"] = overview_periods(selected_windows, len(stations) * (1 if requested == "all" else 2))
    selected_days = sum(item["total_days"] for item in result["periods"])
    for station in stations:
        supported = requested == "all" or requested in station_params(station)
        param = requested if requested != "all" and supported else None
        station_counts, parameter_counts = {}, {}
        for first, last in selected_windows:
            overall, specific = overview_slots(station, first, last, param)
            station_counts.update(overall)
            parameter_counts.update(specific)
        minimum_slots = station["minimum_slots"]
        station_cells = coverage_cells(result["periods"], station_counts, minimum_slots)
        cells = station_cells if requested == "all" else coverage_cells(
            result["periods"], parameter_counts, minimum_slots, supported,
        )
        result["rows"].append({
            "station": station["id"], "label": station["label"], "color": station.get("color"),
            "minimum_slots": minimum_slots, "slot_minutes": station["slot_minutes"],
            "supported": supported, "valid_days": sum(cell["valid_days"] for cell in cells),
            "observed_days": sum(cell["observed_days"] for cell in cells),
            "total_days": selected_days, "cells": cells, "station_cells": station_cells,
        })
    return result


def coverage(station, start, end, daily, allowed, param, *, valid=None):
    if valid is None:
        valid = {day for day, row in daily.items() if row.valid}
    months = valid_months(start, end, valid)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    total_days = (last - first).days + 1
    total_months = (last.year - first.year) * 12 + last.month - first.month + 1
    if not allowed:
        reason = "Станция не измеряет этот параметр."
    elif station.get("start") and station.get("end") and (end < station["start"] or start > station["end"]):
        reason = "В выбранном диапазоне нет наблюдений станции."
    elif not any(row.n for row in daily.values()):
        reason = "В выбранном диапазоне нет пригодных измерений параметра."
    elif not valid:
        reason = "Ни в одних сутках не набралось достаточно пригодных сроков."
    else:
        reason = "Месяц учитывается при наличии не более 10 неполных суток и не более 4 подряд."
    if station["id"] == "kr1" and param == "pressure_hpa" and start <= "2024-04-01" and end >= "2024-04-30" and "2024-04" not in months:
        reason += " Апрель 2024 не прошёл проверку покрытия давления."
    return {
        "station": station["id"], "label": station["label"], "parameter": param,
        "parameter_label": analytics.PARAMS.get(param, (param,))[0],
        "valid_days": len(valid), "total_days": total_days,
        "valid_months": len(months), "total_months": total_months, "reason": reason,
    }, months


def point(period, value, good, used, **extra):
    return {
        "x": period.start, "x_end": period.end, "y": value, "valid_days": len(good),
        "used_days": len(used), "total_days": period.length, **extra,
    }


def make_series(station, param, role, points, unit, *, label=None, parameter_label=None, **extra):
    series = {
        "id": f"{station['id']}:{param}:{role}", "label": label or station["label"],
        "station": station["id"], "parameter": param,
        "parameter_label": parameter_label or analytics.PARAMS.get(param, (param,))[0],
        "role": role, "unit": unit, "points": points, **extra,
    }
    if station.get("color"):
        series["color"] = station["color"]
    return series


def selected_baseline(query, start, end):
    if not query.get("baseline"):
        return start, end
    baseline = query["baseline"].split("-")
    if len(baseline) == 2 and all(len(year) == 4 and year.isdecimal() for year in baseline):
        first, last = baseline[0] + "-01-01", baseline[1] + "-12-31"
        if parse_day(first) <= parse_day(last):
            return first, last
    raise ValueError("Период сравнения указывается как ГГГГ-ГГГГ")


def baseline_values(station, daily, chart_start, chart_end, start, end, grain):
    if (chart_start, chart_end) != (start, end):
        daily = load_daily(station, ("t_air",), start, end)["t_air"]
    valid = {day for day, row in daily.items() if row.valid}
    if grain in {"month", "year"}:
        months = valid_months(start, end, valid)
        used = {day for day in valid if day[:7] in months}
    elif grain == "decade":
        used = {day for day in valid if start <= analytics.bounds(day, "dekad")[0]
                and analytics.bounds(day, "dekad")[1] <= end}
    else:
        used = valid
    return (fmean(daily[day].mean for day in sorted(used)) if used else None,
            len({day[:4] for day in used}))


def trend_values(daily, valid, months):
    points = []
    for month in sorted(months):
        lo, hi = analytics.bounds(month + "-01", "month")
        points.append({"month": month, "valid": True,
                       "mean": fmean(daily[day].mean for day in calendar_days(lo, hi) if day in valid)})
    slope, count = analytics.seasonal_slope(points)
    if slope is None:
        return None, None, None, 0, 0
    comparable = defaultdict(list)
    for row in points:
        comparable[row["month"][5:]].append(row)
    used = [row for group in comparable.values() if len(group) >= 3 for row in group]
    center_x = fmean(month_coordinate(row["month"] + "-01") for row in used)
    center_y = fmean(row["mean"] for row in used)
    return slope, center_x, center_y, count, len({row["month"][:4] for row in used})


def month_coordinate(day):
    parsed = date.fromisoformat(day)
    begin = date(parsed.year, 1, 1).toordinal()
    length = 366 if calendar.isleap(parsed.year) else 365
    return parsed.year + (parsed.toordinal() - begin) / length


def scalar_series(chart_id, chart, station, series_data, intervals, start, end, grain, baseline):
    output, details, notes = [], [], []
    available = station_params(station)
    for param, role in chart.fields:
        daily = series_data[param]
        valid = {day for day, row in daily.items() if row.valid}
        report, months = coverage(station, start, end, daily, param in available, param, valid=valid)
        details.append(report)
        additional = {}
        if role == "anomaly":
            reference, years = baseline_values(station, daily, start, end, *baseline, grain)
            additional.update(baseline=reference, years=years)
            notes.append(f"{station['label']} · число лет в среднем за выбранный период — {years}.")
        if role == "trend":
            slope, center_x, center_y, count, years = trend_values(daily, valid, months)
            additional.update(slope=slope, trend_months=count, years=years, slope_unit="°C/год")
            if slope is None and valid:
                notes.append(f"{station['label']}: для тренда нужны сопоставимые месяцы не менее чем за три года.")
        points = []
        for period, good, used in group_periods(intervals, valid, months, grain):
            value = None
            if used:
                if role == "min":
                    value = min(daily[day].minimum for day in used)
                elif role == "max":
                    value = max(daily[day].maximum for day in used)
                elif role == "trend" and slope is not None:
                    coordinate = fmean(month_coordinate(day) for day in used)
                    value = center_y + slope * (coordinate - center_x)
                elif role != "trend":
                    value = fmean(daily[day].mean for day in used)
                    if role == "anomaly":
                        value = value - reference if reference is not None else None
            extra = {"baseline": reference, "years": years} if role == "anomaly" else {}
            points.append(point(period, value, good, used, **extra))
        label = station["label"]
        param_label = analytics.PARAMS[param][0]
        if chart_id == "wind_combined":
            label += f" · {param_label.lower()}"
        elif len(chart.fields) > 1:
            label += f" · {ROLES[role]}"
            param_label += f" · {ROLES[role]}"
        output.append(make_series(station, param, role, points, chart.unit,
                                  label=label, parameter_label=param_label, **additional))
    return output, list({detail["parameter"]: detail for detail in details}.values()), notes


def estimated_series(chart, station, daily, working, intervals, start, end, grain):
    output = []
    for param, role in chart.fields:
        estimates = working[param]
        if not estimates:
            continue
        used_daily = daily[param] | {day: row for day, (row, _) in estimates.items()}
        valid = {day for day, row in used_daily.items() if row.valid}
        months = valid_months(start, end, valid)
        points = []
        for period, good, used in group_periods(intervals, valid, months, grain):
            methods = Counter()
            for day in used:
                if day in estimates:
                    methods.update(estimates[day][1])
            if not methods:
                value = None
            elif role == "min":
                value = min(used_daily[day].minimum for day in used)
            elif role == "max":
                value = max(used_daily[day].maximum for day in used)
            else:
                value = fmean(used_daily[day].mean for day in used)
            points.append({
                "x": period.start, "x_end": period.end, "y": value, "used_days": len(used) if methods else 0,
                "estimated_n": sum(methods.values()), "estimates_by_method": dict(methods),
            })
        if any(item["y"] is not None for item in points):
            parameter_label = analytics.PARAMS[param][0]
            if len(chart.fields) > 1 and role in ROLES:
                parameter_label += f" · {ROLES[role]}"
            suffix = " · с учётом оценок"
            label = f"{station['label']} · {parameter_label.lower()}" if len(chart.fields) > 1 else station["label"]
            output.append(make_series(
                station, param, "estimate", points, chart.unit, label=label + suffix,
                parameter_label=parameter_label + suffix, source_role=role,
            ))
            output[-1]["id"] += f":{role}"
    return output


def station_statistics(station, param, daily, days):
    valid = [(day, row) for day, row in sorted(daily.items()) if row.valid]
    min_day, minimum = min(valid, key=lambda item: item[1].minimum, default=(None, None))
    max_day, maximum = max(valid, key=lambda item: item[1].maximum, default=(None, None))
    label, unit = analytics.PARAMS[param]
    return {
        "station": station["id"], "label": station["label"], "parameter": param,
        "parameter_label": label, "unit": unit, "valid_days": len(valid), "total_days": days,
        "n": sum(row.n for _, row in valid), "mean": fmean(row.mean for _, row in valid) if valid else None,
        "min": minimum.minimum if minimum else None, "max": maximum.maximum if maximum else None,
        "min_day": min_day, "max_day": max_day,
    }


def chart_data(query):
    """Return independent chart series for each normalized inclusive interval."""
    chart_id, grain, mode = (query.get("chart", "air"), query.get("grain", "month"),
                             query.get("mode", "modern"))
    if chart_id not in CHARTS or grain not in GRAINS or mode not in MODES:
        raise ValueError("Выберите доступный график, группировку и режим отображения")
    ranges = query_intervals(query)
    stations = query_stations(query)
    chart = CHARTS[chart_id]
    working_supported = chart.axis == "time" and all(role in {"mean", "min", "max"} for _, role in chart.fields)
    if stations and chart.axis == "time":
        series_count = len(stations) * len(chart.fields)
        series_count += series_count * working_supported + (chart_id == "temp_difference")
        interval_budget(ranges, grain, series_count)

    sections = (_chart_interval(query, chart_id, grain, mode, stations, first, last, working_supported)
                for first, last in ranges)
    if "intervals" not in query:
        return next(sections)
    details = [interval_details(first, last) for first, last in ranges]
    if len(ranges) == 1 or not stations:
        return dict(next(sections), start=details[0]["start"], end=details[-1]["end"], intervals=details)
    return _combine_intervals(sections, details)


def _combine_intervals(sections, details):
    combined = None
    for result, detail in zip(sections, details):
        if combined is None:
            combined = dict(result, start=details[0]["start"], end=details[-1]["end"], intervals=details,
                            series=[], coverage=[], statistics=[], notes=[])
            if "calm_by_station" in combined:
                del combined["calm_by_station"]
                combined["calm_by_interval"] = {}
        interval = {"interval": detail["id"], "interval_start": detail["start"],
                    "interval_end": detail["end"], "interval_label": detail["label"]}
        for series in result["series"]:
            series.update(interval)
            series["base_id"] = series["id"]
            series["id"] += f"@{detail['id']}"
            series["label"] += f" · {detail['label']}"
            if result["axis"] == "time":
                for point in series["points"]:
                    point["x"] = max(point["x"], detail["start"])
                    point["x_end"] = min(point["x_end"], detail["end"])
        combined["series"].extend(result["series"])
        combined["coverage"].extend(dict(item, **interval) for item in result["coverage"])
        combined["statistics"].extend(dict(item, **interval) for item in result["statistics"])
        combined["notes"].extend(result["notes"])
        if "calm_by_station" in result:
            combined["calm_by_interval"][detail["id"]] = result["calm_by_station"]
    combined["notes"] = list(dict.fromkeys(combined["notes"]))
    return combined


def _chart_interval(query, chart_id, grain, mode, stations, first, last, working_supported):
    start, end = first.isoformat(), last.isoformat()
    chart = CHARTS[chart_id]
    result = {"title": chart.title, "unit": chart.unit, "kind": chart.kind, "axis": chart.axis,
              "series": [], "coverage": [], "statistics": [], "notes": [], "start": start, "end": end,
              "grain": grain, "mode": mode, "chart": chart_id, "coverage_stations": [station["id"] for station in stations]}
    if not stations:
        result["notes"].append("Выберите хотя бы одну станцию")
        return result
    intervals = tuple(periods(start, end, grain)) if chart.axis == "time" else ()
    if grain in {"decade", "month"} and any(not period.full for period in intervals):
        result["notes"].append("На границах диапазона неполные календарные периоды показаны пропусками.")
    if chart_id in {"wind_rose", "wind_freq", "calm"}:
        return wind_data(result, stations, intervals, chart_id, start, end, grain, mode)
    if chart_id == "temp_diurnal":
        return diurnal_data(result, stations, start, end)

    parameters = tuple(dict.fromkeys(param for param, _ in chart.fields))
    station_valid = {station["id"]: set() for station in stations} if "pressure_hpa" in parameters else {}
    working = ({station["id"]: {param: {} for param in parameters} for station in stations}
               if working_supported else {})
    daily = {station["id"]: load_daily(station, parameters, start, end, station_valid.get(station["id"]),
                                       working.get(station["id"]))
             for station in stations}
    baseline = selected_baseline(query, start, end) if chart_id == "air_anomaly" else (start, end)
    scalar = CHARTS["air"] if chart_id == "temp_difference" else chart
    for station in stations:
        station_daily = daily[station["id"]]
        if chart_id not in {"air_anomaly", "air_trend"}:
            result["statistics"].extend(station_statistics(station, param, station_daily[param], (last - first).days + 1)
                                        for param in parameters)
        series, details, notes = scalar_series(chart_id, scalar, station, station_daily,
                                               intervals, start, end, grain, baseline)
        if "pressure_hpa" in station_params(station) and station["id"] in station_valid:
            overall = station_valid[station["id"]]
            for detail in details:
                detail["station_valid_days"] = len(overall)
                detail["station_valid_months"] = len(valid_months(start, end, overall))
                detail["reason"] += f" Покрытие станции по всем срокам: {len(overall)} из {detail['total_days']} суток."
        result["series"].extend(series)
        if station["id"] in working:
            result["series"].extend(estimated_series(scalar, station, daily[station["id"]],
                                                     working[station["id"]], intervals, start, end, grain))
        result["coverage"].extend(details)
        result["notes"].extend(notes)
    if chart_id == "temp_difference":
        add_difference(result, daily, intervals, start, end, grain)
    if grain == "year":
        result["notes"].append("Годовые значения рассчитаны по пригодным суткам полных календарных месяцев.")
    if chart_id == "air_anomaly":
        result["notes"].append(f"Отклонение от среднего за выбранный период {baseline[0]} — {baseline[1]}.")
    if any(series["role"] == "estimate" for series in result["series"]):
        result["notes"].append("Ряды с оценками показывают рабочие значения. Покрытие рассчитано по измерениям.")
    return result


def add_difference(result, daily, intervals, start, end, grain):
    if not set(PAIR) <= daily.keys():
        result["notes"].append("Для разницы выберите Кр1 и Кр2.")
        return
    kr1, kr2 = (daily[station]["t_air"] for station in PAIR)
    common = {day for day, row in kr1.items() if row.valid and day in kr2 and kr2[day].valid}
    paired = {day: Daily(1, kr2[day].mean - kr1[day].mean) for day in common}
    report, months = coverage(DIFFERENCE, start, end, paired, True, "t_air", valid=common)
    report["parameter_label"] = "Разница температур"
    report["reason"] = (
        "Учтены общие пригодные сутки. Для парного месяца допустимо до 10 неполных суток "
        "и не более 4 подряд." if common else "У Кр1 и Кр2 нет общих пригодных суток."
    )
    points = []
    for period, good, used in group_periods(intervals, common, months, grain):
        if grain == "decade" and len(used) * 4 < period.length * 3:
            used = []
        first = fmean(kr1[day].mean for day in used) if used else None
        second = fmean(kr2[day].mean for day in used) if used else None
        points.append(point(period, second - first if used else None, good, used, kr1=first, kr2=second))
    result["coverage"].append(report)
    result["series"].append(make_series(
        DIFFERENCE, "t_air", "difference", points, "°C", parameter_label="Разница температур",
    ))
    result["notes"].append("Кр2 − Кр1 рассчитана по общим пригодным суткам. Станционные ряды показаны по их собственному покрытию.")
    if grain == "decade":
        result["notes"].append("Для парной декады требуются общие пригодные наблюдения не менее чем за 75% суток.")


def hourly_days(station, start, end, daily):
    if "t_air" not in station_params(station):
        return
    for day, rows in raw_days(station, start, end, ("t_air",)):
        hours = defaultdict(list)
        slots = set()
        for timestamp, row in rows.items():
            value = row.get("t_air")
            if value is not None:
                hours[timestamp[11:13]].append(value)
                slots.add(slot(timestamp, station))
        valid = len(slots) >= station["minimum_slots"]
        daily[day] = Daily(sum(map(len, hours.values())), 0.0 if valid else None)
        if valid:
            yield day, {hour: fmean(values) for hour, values in hours.items()}


class HourlyMean:
    def __init__(self):
        self.sums = defaultdict(float)
        self.counts = defaultdict(int)

    def add(self, values, hours=None):
        for hour in values if hours is None else hours:
            self.sums[hour] += values[hour]
            self.counts[hour] += 1

    def points(self):
        points = []
        for index in range(24):
            hour = f"{index:02}"
            count = self.counts[hour]
            points.append({"x": hour, "y": self.sums[hour] / count if count else None, "n": count})
        return points


def paired_hours(first, second):
    solo = HourlyMean(), HourlyMean()
    paired = HourlyMean(), HourlyMean()
    joined = False
    streams = (first, second)
    left, right = (next(stream, None) for stream in streams)
    while left or right:
        if right is None or (left and left[0] < right[0]):
            solo[0].add(left[1])
            left = next(streams[0], None)
        elif left is None or right[0] < left[0]:
            solo[1].add(right[1])
            right = next(streams[1], None)
        else:
            hours = left[1].keys() & right[1].keys()
            joined = joined or bool(hours)
            for index, (_, values) in enumerate((left, right)):
                solo[index].add(values)
                paired[index].add(values, hours)
            left, right = (next(stream, None) for stream in streams)
    return (paired if joined else solo), joined


def diurnal_data(result, stations, start, end):
    daily = {station["id"]: {} for station in stations}
    streams = {station["id"]: hourly_days(station, start, end, daily[station["id"]]) for station in stations}
    profiles = {}
    if set(PAIR) <= streams.keys():
        pair, joined = paired_hours(*(streams[key] for key in PAIR))
        profiles.update(zip(PAIR, pair))
        if joined:
            result["notes"].append("Для Кр1 и Кр2 использованы общие календарные дни и часы.")
        elif any(profile.counts for profile in pair):
            result["notes"].append("Общих пригодных часов у Кр1 и Кр2 нет. Показаны доступные профили станций.")
    for station in stations:
        key = station["id"]
        if key not in profiles:
            profile = HourlyMean()
            for _, hours in streams[key]:
                profile.add(hours)
            profiles[key] = profile
        report, _ = coverage(station, start, end, daily[key], "t_air" in station_params(station), "t_air")
        result["coverage"].append(report)
        result["series"].append(make_series(station, "t_air", "diurnal", profiles[key].points(), "°C"))
    result["notes"].append("Часы сгруппированы по исходным отметкам времени. Часовой пояс архива не подтверждён.")
    return result


@dataclass(frozen=True)
class WindDay:
    speed_slots: int
    direction_slots: int
    joint_slots: int
    speed_n: int
    direction_n: int
    joint_n: int
    calm_n: int
    directions: tuple
    speed_classes: tuple
    sector_sums: tuple


def wind_day(rows, station):
    speed_slots, direction_slots, joint_slots = set(), set(), set()
    speed_n = direction_n = joint_n = calm = 0
    directions = [0] * len(DIRECTIONS)
    classes = [[0] * len(DIRECTIONS) for _ in SPEED_CLASSES]
    sums = [0.0] * len(DIRECTIONS)
    for timestamp, row in rows.items():
        speed, direction = row.get("wind_speed"), row.get("wind_dir")
        if speed is None and direction is None:
            continue
        term = slot(timestamp, station)
        if speed is not None:
            speed_slots.add(term)
            speed_n += 1
            calm += speed < CALM_LIMIT
        if direction is None:
            continue
        direction_slots.add(term)
        direction_n += 1
        if speed is not None:
            joint_slots.add(term)
            joint_n += 1
            if speed < CALM_LIMIT:
                continue
        sector = int((direction % 360 + 11.25) // 22.5) % len(DIRECTIONS)
        directions[sector] += 1
        if speed is not None:
            band = next(index for index, (limit, _) in enumerate(SPEED_CLASSES) if speed < limit)
            classes[band][sector] += 1
            sums[sector] += speed
    return WindDay(len(speed_slots), len(direction_slots), len(joint_slots), speed_n, direction_n, joint_n,
                   calm, tuple(directions), tuple(map(tuple, classes)), tuple(sums))


@lru_cache(maxsize=40)
def archived_wind(path, revision, station_id, slot_minutes, year):
    """Cache yearly daily counts, keeping individual observations out of the cache."""
    station = {"id": station_id, "slot_minutes": slot_minutes}
    params = ("wind_dir", "wind_speed")
    with ZipFile(path) as archive:
        records = archive_rows(archive, station_id, year, f"{year:04}-01-01", f"{year:04}-12-31")
        return {
            day: wind_day(clean_archive_group(group, params, {}), station)
            for day, group in groupby(records, lambda row: row["datetime"][:10])
        }


def wind_days(station, start, end):
    summary = {}
    station_id = station["id"]
    if station_id in build_data.STATIONS:
        path = analytics.ARCHIVE
        metadata = path.stat()
        revision = metadata.st_ino, metadata.st_mtime_ns, metadata.st_size
        first_year, last_year = int(start[:4]), int(end[:4])
        for year in build_data.STATIONS[station_id].years:
            if first_year <= year <= last_year:
                data = archived_wind(path, revision, station_id, station["slot_minutes"], year)
                summary.update((day, row) for day, row in data.items() if start <= day <= end)
    for day, rows in raw_days(station, start, end, ("wind_dir", "wind_speed"), imports_only=True):
        summary[day] = wind_day(rows, station)
    return summary


def wind_coverage(station, summary, start, end, flavor):
    param = "wind_speed" if flavor == "speed" else "wind_dir"
    available = station_params(station)
    allowed = param in available and (flavor != "joint" or "wind_speed" in available)
    daily = {
        day: Daily(getattr(row, f"{flavor}_n"),
                   0.0 if getattr(row, f"{flavor}_slots") >= station["minimum_slots"] else None)
        for day, row in summary.items() if allowed
    }
    report, months = coverage(station, start, end, daily, allowed, param)
    if flavor == "joint":
        report["parameter_label"] = "Направление и скорость ветра"
        if allowed and not report["valid_days"]:
            report["reason"] += " Для розы по скорости нужны оба измерения в одни сроки."
    return report, months, {day for day, row in daily.items() if row.valid}


def rose_points(counts, numerator, *, average=False):
    total = sum(counts)
    return [{"x": sector, "y": (
        (numerator[i] / counts[i] if counts[i] else None) if average
        else (100 * numerator[i] / total if total else None)
    ), "n": counts[i] if average else numerator[i]} for i, sector in enumerate(DIRECTIONS)]


def add_rose(result, station, summary, start, end, chart_id, mode):
    direction, _, directional = wind_coverage(station, summary, start, end, "direction")
    jointly, _, paired = wind_coverage(station, summary, start, end, "joint")
    use_speed = chart_id == "wind_rose" and mode != "semenov"
    use_frequency = chart_id == "wind_freq" or mode != "modern"
    result["coverage"].append(jointly if use_speed and not use_frequency else direction)
    if chart_id == "wind_rose" and mode == "togu":
        result["coverage"].append(jointly)

    if use_frequency:
        counts = tuple(sum(summary[day].directions[i] for day in directional) for i in range(len(DIRECTIONS)))
        result["series"].append(make_series(
            station, "wind_dir", "frequency", rose_points(counts, counts), "%",
            label=f"{station['label']} · повторяемость" if mode == "togu" and use_speed else station["label"],
            parameter_label="Повторяемость направлений",
        ))
    if use_speed:
        by_class = [
            tuple(sum(summary[day].speed_classes[band][i] for day in paired) for i in range(len(DIRECTIONS)))
            for band in range(len(SPEED_CLASSES))
        ]
        counts = tuple(map(sum, zip(*by_class)))
        if mode == "togu":
            sums = [math.fsum(summary[day].sector_sums[i] for day in paired) for i in range(len(DIRECTIONS))]
            result["series"].append(make_series(
                station, "wind_speed", "speed", rose_points(counts, sums, average=True), "м/с",
                label=f"{station['label']} · средняя скорость", parameter_label="Средняя скорость по румбам",
            ))
        else:
            for band, (_, label) in enumerate(SPEED_CLASSES):
                item = make_series(station, "wind_dir", "speed_class", rose_points(counts, by_class[band]), "%",
                                   label=f"{station['label']} · {label}", speed_class=label,
                                   parameter_label="Повторяемость по классу скорости")
                item["id"] += f":{band}"
                result["series"].append(item)
    usable = (directional if use_frequency else paired)
    wind = [summary[day] for day in usable if summary[day].speed_slots >= station["minimum_slots"]]
    total = sum(row.speed_n for row in wind)
    result["calm_by_station"][station["id"]] = 100 * sum(row.calm_n for row in wind) / total if total else None


def wind_data(result, stations, intervals, chart_id, start, end, grain, mode):
    result["notes"].append("Штиль — скорость менее 0,5 м/с. Направления показаны без наблюдений в штиль.")
    if chart_id == "wind_rose" and mode == "semenov":
        result["notes"].append("Роза по Семёнову представлена предварительно по повторяемости направлений.")
    if chart_id != "calm":
        result["calm_by_station"] = {}
    for station in stations:
        summary = wind_days(station, start, end)
        if chart_id != "calm":
            add_rose(result, station, summary, start, end, chart_id, mode)
            if station["id"] == "ame60":
                if start <= "1998-12-31" and end >= "1998-01-01":
                    result["notes"].append("АМЕ-60, 1998 год: направление ветра в исходном архиве отсутствует.")
                if start <= "2021-12-31" and end >= "2021-01-01":
                    result["notes"].append("АМЕ-60, 2021 год: направление ветра в исходном архиве исключено по флагам качества.")
            continue
        report, months, valid = wind_coverage(station, summary, start, end, "speed")
        result["coverage"].append(report)
        points = []
        for period, good, used in group_periods(intervals, valid, months, grain):
            observations = sum(summary[day].speed_n for day in used)
            calm = sum(summary[day].calm_n for day in used)
            value = 100 * calm / observations if observations else None
            points.append(point(period, value, good, used, n=observations, calm=calm))
        result["series"].append(make_series(station, "wind_speed", "calm", points, "%",
                                             parameter_label="Доля штиля"))
    return result
