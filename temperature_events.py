"""Пересечения 0 °C по пригодным среднесуточным температурам воздуха."""

import csv
import io
from contextlib import closing
from datetime import date, timedelta
from math import isfinite

from analytics import STATIONS, database


# Устраняет остаток двоичного округления, когда сумма исходных десятичных
# температур математически равна нулю (АМЕ-60, 2014-12-13).
ZERO_TOLERANCE_C = 1e-12
PARAMETER = "t_air"
ONE_DAY = timedelta(days=1)


def temperature_sign(mean):
    if mean < -ZERO_TOLERANCE_C:
        return -1
    return int(mean > ZERO_TOLERANCE_C)


def select_day(text):
    try:
        value = date.fromisoformat(text)
        if value.isoformat() == text:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД")


def inspect_days(station, days, start, end):
    """Walk calendar-ordered daily records already limited to this selection."""
    counts = dict(
        calendar_days=max(0, (end - start).days + 1), valid_days=0, zero_days=0,
        valid_pairs=0, warming=0, cooling=0,
    )
    previous_valid = None
    previous_nonzero = None
    zeros = []
    events = []
    for row in days:
        day = date.fromisoformat(row["day"])
        mean = row["mean"]
        if not (row["valid"] and mean is not None and isfinite(mean)):
            previous_valid = None
            previous_nonzero = None
            zeros = []
            continue
        counts["valid_days"] += 1
        if previous_valid is not None and day - previous_valid == ONE_DAY:
            counts["valid_pairs"] += 1
        else:
            previous_nonzero = None
            zeros = []
        previous_valid = day
        sign = temperature_sign(mean)
        if not sign:
            counts["zero_days"] += 1
            if previous_nonzero is not None:
                zeros.append(row["day"])
            continue
        if previous_nonzero is not None:
            previous_day, previous_mean, previous_sign = previous_nonzero
            if sign != previous_sign:
                direction = "warming" if sign > 0 else "cooling"
                counts[direction] += 1
                events.append({
                    "station": station, "direction": direction, "previous_day": previous_day,
                    "previous_mean_c": previous_mean, "day": row["day"], "mean_c": mean,
                    "zero_days": zeros,
                })
        previous_nonzero = row["day"], mean, sign
        zeros = []
    return counts, events


def query_events(query):
    station = query.get("station", "all")
    if station != "all" and station not in STATIONS:
        raise ValueError("Неизвестная станция")
    selected = STATIONS if station == "all" else (station,)
    archives = {
        key: (date(STATIONS[key][1], 1, 1), date(STATIONS[key][2], 12, 31)) for key in selected
    }
    start = select_day(query["start"]) if "start" in query else min(lo for lo, _ in archives.values())
    end = select_day(query["end"]) if "end" in query else max(hi for _, hi in archives.values())
    if start > end:
        raise ValueError("Дата начала должна быть раньше даты окончания или совпадать с ней")

    summaries, events = [], []
    with closing(database()) as connection:
        for key, (archive_start, archive_end) in archives.items():
            lo, hi = max(start, archive_start), min(end, archive_end)
            overlap = lo <= hi
            days = connection.execute(
                """SELECT day, mean, valid FROM daily
                   WHERE station=? AND param=? AND day BETWEEN ? AND ? ORDER BY day""",
                (key, PARAMETER, lo.isoformat(), hi.isoformat()),
            ) if overlap else ()
            counts, matches = inspect_days(key, days, lo, hi)
            summaries.append({
                "station": key, "label": STATIONS[key][0],
                "archive_start": archive_start.isoformat(), "archive_end": archive_end.isoformat(),
                "start": lo.isoformat() if overlap else None, "end": hi.isoformat() if overlap else None,
                **counts, "invalid_days": counts["calendar_days"] - counts["valid_days"],
                "events": len(matches),
            })
            events.extend(matches)
    events.sort(key=lambda event: (event["day"], event["station"]))
    return {
        "station": station, "param": PARAMETER, "unit": "°C",
        "start": start.isoformat(), "end": end.isoformat(), "zero_tolerance_c": ZERO_TOLERANCE_C,
        "stations": summaries, "events": events,
    }


def export_events(result):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Станция", "Направление", "Дата до перехода", "Среднее до перехода (°C)",
        "Дата перехода", "Среднее после перехода (°C)", "Даты нулевых суток",
    ])
    direction_labels = {"warming": "Из минуса в плюс", "cooling": "Из плюса в минус"}
    for event in result["events"]:
        writer.writerow([
            event["station"], direction_labels[event["direction"]],
            event["previous_day"], str(event["previous_mean_c"]).replace(".", ","),
            event["day"], str(event["mean_c"]).replace(".", ","),
            " ".join(event["zero_days"]),
        ])
    return ("\ufeff" + output.getvalue()).encode("utf-8")
