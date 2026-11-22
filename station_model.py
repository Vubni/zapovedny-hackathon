"""Transferable one-day mean air temperature model and calendar backtests."""

import argparse
import json
import math
from bisect import bisect_right
from calendar import isleap
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from tempfile import TemporaryDirectory

from analytics import ARCHIVE, DATABASE, ROOT, database
from build_data import LIMITS, sha256
from observation_store import all_date_bounds
from observations import confirmed_day_groups, load_daily, station_params
from station_catalog import AREA, catalog

MODEL_FILE = ROOT / "results" / "model" / "temperature.json"
MANIFEST = DATABASE.with_name("build.json")
ONE_DAY = timedelta(days=1)
FEATURES = ("today_minus_yesterday_c", "hemisphere_adjusted_seasonal_drift")
NO_PAIR = "Для прогноза нужны двое последовательных суток с пригодной среднесуточной температурой."


def features(target, today, yesterday, latitude):
    year_days = 366 if isleap(target.year) else 365
    # Warming direction after the northern winter solstice. Mirror across the equator.
    phase = math.tau * ((target - date(target.year, 1, 1)).days + 10) / year_days
    scale = 0 if latitude is None else math.sin(math.radians(latitude)) / math.sin(math.radians(AREA[0]))
    seasonal_drift = min(1, max(-1, scale)) * math.sin(phase)
    return (today - yesterday, seasonal_drift)


def examples(by_day, latitude):
    for target, actual in sorted(by_day.items()):
        source = target - ONE_DAY
        previous = source - ONE_DAY
        if source in by_day and previous in by_day:
            today = by_day[source]
            yield target, features(target, today, by_day[previous], latitude), today, actual


def period(rows):
    dates = [row[0] for row in rows]
    return {"days": len(dates), "first_target_day": min(dates).isoformat(),
            "last_target_day": max(dates).isoformat()}


def fit_before(groups, cutoff, exclude=None):
    """Fit two residual coefficients with equal total weight for each donor station."""
    samples = {
        station: [row for row in rows if row[0] < cutoff]
        for station, rows in groups.items() if station != exclude
    }
    samples = {station: rows for station, rows in samples.items() if rows}
    if not samples:
        return None
    rows = [row for group in samples.values() for row in group]
    weighted = [(x, s, actual - today, 1 / len(group))
                for group in samples.values() for _, (x, s), today, actual in group]
    xx = math.fsum(w * x * x for x, _, _, w in weighted)
    xs = math.fsum(w * x * s for x, s, _, w in weighted)
    ss = math.fsum(w * s * s for _, s, _, w in weighted)
    xy = math.fsum(w * x * y for x, _, y, w in weighted)
    sy = math.fsum(w * s * y for _, s, y, w in weighted)
    determinant = xx * ss - xs * xs
    if determinant <= 1e-10:
        return None
    return {
        **period(rows),
        "available_from": cutoff.isoformat(),
        "stations": {station: len(group) for station, group in samples.items()},
        "coefficients": [(ss * xy - xs * sy) / determinant, (xx * sy - xs * xy) / determinant],
    }


def estimate(coefficients, inputs, today):
    return today + math.fsum(weight * value for weight, value in zip(coefficients, inputs))


def evaluate_station(station, groups, test_year, *, leave_out=False):
    start, end = date(test_year, 1, 1), date(test_year, 12, 31)
    test = [row for row in groups[station["id"]] if start <= row[0] <= end]
    fit = fit_before(groups, start, station["id"] if leave_out else None)
    meta = {"station": station["id"], "label": station["label"], "window_start": start.isoformat(),
            "window_end": end.isoformat(), "calendar_days": (end - start).days + 1}
    if fit is None:
        return {**meta, "days": 0, "eligible_target_days": len(test),
                "reason": "До начала теста отсутствуют обучающие данные других станций."}
    return {
        **meta, **period(test), "training": fit,
        "model_mae_c": fmean(abs(estimate(fit["coefficients"], inputs, today) - actual)
                             for _, inputs, today, actual in test),
        "baseline_mae_c": fmean(abs(today - actual) for _, _, today, actual in test),
    }


def archive_range(days, *, latest=False):
    pairs = [day for day in sorted(days) if date.min < day < date.max and day - ONE_DAY in days]
    if not pairs:
        return None
    with_observation = [day for day in pairs if day + ONE_DAY in days]
    return {
        "first_source_day": pairs[0].isoformat(),
        "last_source_day": max(days).isoformat(),
        "default_source_day": (pairs[-1] if latest or not with_observation else with_observation[-1]).isoformat(),
    }


def train():
    archive_hash, database_hash = sha256(ARCHIVE), sha256(DATABASE)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (archive_hash, database_hash) != (manifest["source_sha256"], manifest["database_sha256"]):
        raise ValueError("Контрольные суммы изменились. Обновите базу командой python3 build_data.py")
    stations = [station for station in catalog() if station["archive"] and "t_air" in station_params(station)]
    groups, ranges = {}, {}
    with closing(database()) as connection:
        for station in stations:
            key = station["id"]
            records = connection.execute(
                "SELECT day, mean FROM daily WHERE station=? AND param='t_air' "
                "AND valid=1 AND mean IS NOT NULL ORDER BY day", (key,),
            )
            readings = {date.fromisoformat(day): mean for day, mean in records}
            groups[key] = list(examples(readings, station["latitude"]))
            ranges[key] = archive_range(readings)
    years = sorted({row[0].year + 1 for rows in groups.values() for row in rows})
    fits = [fit for year in years if (fit := fit_before(groups, date(year, 1, 1))) is not None]
    return {
        "version": 2,
        "source": ARCHIVE.name,
        "source_sha256": archive_hash,
        "database_sha256": database_hash,
        "target": "next_calendar_day_mean_air_temperature",
        "unit": "°C",
        "method": "station_balanced_residual_least_squares",
        "features": FEATURES,
        "baseline": "last_observed_daily_mean",
        "archive_ranges": ranges,
        "fits": fits,
        "tests": [evaluate_station(station, groups, groups[station["id"]][-1][0].year)
                  for station in stations],
        "transfer_tests": [evaluate_station(station, groups, groups[station["id"]][-1][0].year, leave_out=True)
                           for station in stations],
    }


def active_model():
    model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if model["version"] != 2 or model["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Обновите модель для текущего архива командой python3 station_model.py")
    return model


def latest_civil_day():
    # UTC+14 is the most advanced civil clock. Users supply completed local days.
    return datetime.now(timezone(timedelta(hours=14))).date()


def station_range(station, model, published):
    if station["id"] not in published:
        return model["archive_ranges"].get(station["id"])
    start, end = station["start"], station["end"]
    end = min(end, (latest_civil_day() - ONE_DAY).isoformat())
    if start > end:
        return None
    readings = load_daily(station, ("t_air",), start, end)["t_air"]
    days = {date.fromisoformat(day) for day, reading in readings.items() if reading.valid}
    return archive_range(days, latest=True)


def model_catalog():
    model = active_model()
    published = all_date_bounds()
    stations = [{
        "id": station["id"], "label": station["label"],
        "archive": station_range(station, model, published),
        "has_observations": bool(station["start"]),
        "supports_temperature": "t_air" in station_params(station),
    } for station in catalog()]
    return {
        "stations": stations,
        "default_station": next((station["id"] for station in stations if station["id"] == "kr1"),
                                stations[0]["id"]),
        "training": {key: value for key, value in model["fits"][-1].items() if key != "coefficients"},
        "tests": model["tests"],
        "transfer_tests": model["transfer_tests"],
    }


def input_day(text):
    try:
        value = date.fromisoformat(text)
        if value.isoformat() == text and date.min < value < date.max:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД")


def input_temperature(text, label):
    low, high = LIMITS["t_air"]
    try:
        value = float(str(text).replace(",", "."))
        if low <= value <= high:
            return value
    except ValueError:
        pass
    raise ValueError(f"Введите среднесуточную температуру за {label} от {low} до {high} °C")


def station_readings(station, previous, source, target, civil_day):
    start = previous.isoformat()
    end = (target if target < civil_day else source).isoformat()
    readings = load_daily(station, ("t_air",), start, end)["t_air"]
    missing = [day.isoformat() for day in (previous, source)
               if day.isoformat() not in readings or not readings[day.isoformat()].valid]
    if missing:
        raise ValueError(NO_PAIR + " Нет пригодной температуры за " + ", ".join(missing))
    confirmed = {day for day, _ in confirmed_day_groups(station["id"], start, source.isoformat(),
                                                       {"t_air"}, False, False)}
    inputs = [{"day": day, "temperature_c": readings[day].mean,
               "origin": "confirmed" if day in confirmed else "archive"}
              for day in (start, source.isoformat())]
    observed = readings.get(target.isoformat())
    return inputs, observed.mean if observed else None


def predict(query):
    model = active_model()
    station_id = query.get("station", "kr1")
    station = next((entry for entry in catalog() if entry["id"] == station_id), None)
    if station is None:
        raise ValueError("Неизвестная станция")
    mode = query.get("mode", "archive")
    if mode not in {"archive", "manual"}:
        raise ValueError("Выберите наблюдения станции или ручной ввод")

    day_text = query.get("day")
    if day_text is None and mode == "archive":
        archive = station_range(station, model, all_date_bounds())
        if archive is None:
            raise ValueError(NO_PAIR)
        day_text = archive["default_source_day"]
    source = input_day(day_text)
    civil_day = latest_civil_day()
    if source >= civil_day:
        raise ValueError("Укажите завершившиеся сутки по календарю станции")
    previous, target = source - ONE_DAY, source + ONE_DAY
    if mode == "archive":
        inputs, observed = station_readings(station, previous, source, target, civil_day)
    else:
        inputs = [
            {"day": previous.isoformat(), "temperature_c": input_temperature(query.get("yesterday"),
                                                                           "предыдущий день"), "origin": "manual"},
            {"day": source.isoformat(), "temperature_c": input_temperature(query.get("today"),
                                                                           "выбранный день"), "origin": "manual"},
        ]
        observed = None
    yesterday, today = (item["temperature_c"] for item in inputs)
    index = bisect_right([fit["available_from"] for fit in model["fits"]], target.isoformat()) - 1
    fit = model["fits"][index] if index >= 0 else None
    prediction = estimate(fit["coefficients"], features(target, today, yesterday, station["latitude"]), today) if fit else today
    return {
        "station": station_id, "mode": mode, "source_day": source.isoformat(), "target_day": target.isoformat(),
        "inputs": inputs, "prediction_c": prediction, "baseline_c": today, "observed_c": observed,
        "fit": {"kind": "transfer" if fit else "baseline",
                "training_through": fit["last_target_day"] if fit else None},
    }


def main():
    parser = argparse.ArgumentParser(description="Обучение и проверка переносимой суточной модели температуры")
    parser.add_argument("--output", type=Path, default=MODEL_FILE)
    options = parser.parse_args()
    if not DATABASE.is_file():
        parser.error("Сначала соберите базу командой python3 build_data.py")
    model = train()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=options.output.parent) as temporary:
        staged = Path(temporary) / options.output.name
        staged.write_text(json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")
        staged.replace(options.output)
    for key, section in (("tests", "По времени"), ("transfer_tests", "Новая станция")):
        for test in model[key]:
            summary = (f'{test["days"]} суток, MAE {test["model_mae_c"]:.3f} °C, '
                       f'база {test["baseline_mae_c"]:.3f} °C') if test["days"] else test["reason"]
            print(f'{section} · {test["label"]} · {summary}')


if __name__ == "__main__":
    main()
