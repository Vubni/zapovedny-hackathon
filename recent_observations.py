"""Daily table for the last five dates ending at an actual observation."""
import csv
import json
from collections import Counter
from contextlib import closing
from datetime import date
from io import StringIO

import analytics
import observation_store
from observations import calendar_days, load_daily, observed_slots, raw_days, station_params
from station_catalog import catalog


def _last_archive_day(station, parameter, before=None):
    if station not in analytics.STATIONS:
        return None
    condition = " AND day < ?" if before is not None else ""
    args = (station, parameter, before) if before is not None else (station, parameter)
    with closing(analytics.database()) as db:
        return db.execute(
            f"SELECT MAX(day) FROM daily WHERE station=? AND param=? AND n > 0{condition}", args,
        ).fetchone()[0]


def _last_observation_day(station, parameter):
    station_id = station["id"]
    archived = _last_archive_day(station_id, parameter)
    first, last = observation_store.date_bounds(station_id)
    if not last or (archived and archived > last):
        return archived
    preceding = archived if archived and archived < first else _last_archive_day(station_id, parameter, before=first)
    daily = load_daily(station, (parameter,), first, last)[parameter]
    observed = max((day for day, metrics in daily.items() if metrics.n), default=None)
    return max((day for day in (preceding, observed) if day), default=None)


def _day_row(day, rows, station, parameter):
    observed, estimated, methods = [], [], Counter()
    missing = 0
    for row in rows.values():
        if not row.get("synthetic") and row.get(parameter) is not None:
            observed.append(row[parameter])
            continue
        estimate = row.get("estimates", {}).get(parameter)
        if estimate and estimate.get("value") is not None:
            estimated.append(estimate["value"])
            methods[estimate.get("method") or "unknown"] += 1
        elif not row.get("synthetic"):
            missing += 1

    slots = observed_slots(rows, station, parameter)
    complete = slots >= station["minimum_slots"]
    observed_mean = analytics.mean_of(observed, parameter)
    show_extremes = complete and parameter != "wind_dir"
    status = "observed" if observed else "missing"
    if estimated:
        status = "mixed" if observed else "estimated"
    return {
        "day": day, "status": status, "observed_n": len(observed), "observed_slots": slots,
        "complete": complete, "mean": observed_mean if complete else None,
        "minimum": min(observed) if show_extremes else None,
        "maximum": max(observed) if show_extremes else None,
        "observed_term_mean": observed_mean, "estimated_n": len(estimated),
        "estimated_mean": analytics.mean_of(estimated, parameter), "estimates_by_method": dict(methods),
        "missing_n": missing,
    }


def latest_five(query):
    station_id, parameter = query.get("station", "kr1"), query.get("param", "t_air")
    station = next((item for item in catalog() if item["id"] == station_id), None)
    if station is None:
        raise ValueError("Неизвестная станция.")
    if parameter not in station_params(station):
        raise ValueError("Станция не измеряет выбранный параметр.")
    label, unit = analytics.PARAMS[parameter]
    end = _last_observation_day(station, parameter)
    start, rows = None, []
    if end:
        start = date.fromordinal(max(1, date.fromisoformat(end).toordinal() - 4)).isoformat()
        records = dict(raw_days(station, start, end, (parameter,), include_estimates=True))
        rows = [_day_row(day, records.get(day, {}), station, parameter) for day in calendar_days(start, end)]
    return {
        "station": station_id, "station_label": station["label"], "param": parameter,
        "label": label, "unit": unit, "start": start, "end": end,
        "minimum_slots": station["minimum_slots"], "rows": rows,
    }


def export_recent_csv(table):
    fields = (
        "day", "station", "param", "unit", "status", "observed_n", "observed_slots", "minimum_slots",
        "complete", "mean", "minimum", "maximum", "observed_term_mean", "estimated_n", "estimated_mean",
        "estimates_by_method", "missing_n",
    )
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    shared = {field: table[field] for field in ("station", "param", "unit", "minimum_slots")}
    for row in table["rows"]:
        writer.writerow({
            **row, **shared, "complete": int(row["complete"]),
            "estimates_by_method": json.dumps(row["estimates_by_method"], sort_keys=True, separators=(",", ":")),
        })
    name = f"{table['station']}_{table['param']}_latest-five.csv"
    return name, output.getvalue().encode("utf-8-sig")
