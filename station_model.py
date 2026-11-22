"""Separate one-day temperature regressions for AME-60, Kr1 and Kr2."""

import argparse
import json
import math
from calendar import isleap
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from tempfile import TemporaryDirectory

from analytics import ARCHIVE, DATABASE, ROOT, STATIONS, database
from build_data import LIMITS, sha256

MODEL_FILE = ROOT / "results" / "model" / "temperature.json"
MANIFEST = DATABASE.with_name("build.json")
ONE_DAY = timedelta(days=1)
HOLDOUT_YEARS = {"ame60": 2021, "kr1": 2025, "kr2": 2025}
FEATURES = ("intercept", "today_c", "today_minus_yesterday_c", "annual_sin", "annual_cos")


def features(target, today, yesterday):
    year_days = 366 if isleap(target.year) else 365
    angle = math.tau * (target - date(target.year, 1, 1)).days / year_days
    return (1.0, today, today - yesterday, math.sin(angle), math.cos(angle))


def examples(by_day):
    for target, observed in by_day.items():
        source = target - ONE_DAY
        previous = source - ONE_DAY
        if source in by_day and previous in by_day:
            yield target, features(target, by_day[source], by_day[previous]), observed


def least_squares(rows):
    """Solve the small normal system with partial pivoting."""
    columns = list(zip(*(inputs + (actual,) for _, inputs, actual in rows)))
    size = len(FEATURES)
    system = [
        [math.fsum(a * b for a, b in zip(left, right)) for right in columns]
        for left in columns[:size]
    ]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(system[row][col]))
        system[col], system[pivot] = system[pivot], system[col]
        scale = system[col][col]
        if abs(scale) < 1e-10:
            raise ValueError("Недостаточно независимых признаков для обучения")
        system[col] = [value / scale for value in system[col]]
        for row in range(size):
            if row != col:
                multiplier = system[row][col]
                system[row] = [value - multiplier * base for value, base in zip(system[row], system[col])]
    return [row[-1] for row in system]


def estimate(coefficients, inputs):
    return math.fsum(weight * value for weight, value in zip(coefficients, inputs))


def period(rows):
    return {
        "days": len(rows),
        "first_target_day": rows[0][0].isoformat(),
        "last_target_day": rows[-1][0].isoformat(),
    }


def evaluate_station(station, by_day):
    test_year = HOLDOUT_YEARS[station]
    test_start, test_end = date(test_year, 1, 1), date(test_year, 12, 31)
    training, test = [], []
    for row in examples(by_day):
        target = row[0]
        if target < test_start:
            training.append(row)
        elif target <= test_end:
            test.append(row)
    if not training or not test:
        raise ValueError(f"Для {station} не хватает последовательных суток для обучения и теста")
    coefficients = least_squares(training)
    return {
        "id": station,
        "label": STATIONS[station][0],
        "coefficients": coefficients,
        "training": period(training),
        "test": {
            **period(test),
            "window_start": test_start.isoformat(),
            "window_end": test_end.isoformat(),
            "calendar_days": (test_end - test_start).days + 1,
            "model_mae_c": fmean(abs(estimate(coefficients, inputs) - actual) for _, inputs, actual in test),
            "baseline_mae_c": fmean(abs(inputs[1] - actual) for _, inputs, actual in test),
        },
        "archive": {
            "first_source_day": (test_start - ONE_DAY).isoformat(),
            "last_source_day": max(by_day).isoformat(),
            "default_source_day": (test[-1][0] - ONE_DAY).isoformat(),
        },
    }


def train():
    archive_hash, database_hash = sha256(ARCHIVE), sha256(DATABASE)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (archive_hash, database_hash) != (manifest["source_sha256"], manifest["database_sha256"]):
        raise ValueError("Контрольные суммы изменились. Обновите базу командой python3 build_data.py")
    with closing(database()) as connection:
        stations = []
        for station in HOLDOUT_YEARS:
            days = connection.execute(
                "SELECT day, mean FROM daily WHERE station=? AND param='t_air' "
                "AND valid=1 AND mean IS NOT NULL ORDER BY day",
                (station,),
            )
            by_day = {date.fromisoformat(day): mean for day, mean in days}
            stations.append(evaluate_station(station, by_day))
    return {
        "version": 1,
        "source": ARCHIVE.name,
        "source_sha256": archive_hash,
        "database_sha256": database_hash,
        "target": "next_calendar_day_mean_air_temperature",
        "unit": "°C",
        "method": "ordinary_least_squares",
        "features": FEATURES,
        "baseline": "last_observed_daily_mean",
        "stations": stations,
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
        value = float(text.replace(",", "."))
        if low <= value <= high:
            return value
    except (AttributeError, ValueError):
        pass
    raise ValueError(f"Введите среднесуточную температуру за {label} от {low} до {high} °C")


def archive_values(station, previous, source, target):
    with closing(database()) as connection:
        rows = connection.execute(
            "SELECT day, mean FROM daily WHERE station=? AND param='t_air' "
            "AND valid=1 AND mean IS NOT NULL AND day BETWEEN ? AND ?",
            (station, previous.isoformat(), target.isoformat()),
        )
        readings = dict(rows)
    missing = [day.isoformat() for day in (previous, source) if day.isoformat() not in readings]
    if missing:
        raise ValueError("Нет пригодной среднесуточной температуры за " + ", ".join(missing))
    return readings[source.isoformat()], readings[previous.isoformat()], readings.get(target.isoformat())


def predict(query):
    model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if model["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Версия архива изменилась. Обновите модель командой python3 station_model.py")
    station = query.get("station", "kr1")
    fit = next((entry for entry in model["stations"] if entry["id"] == station), None)
    if fit is None:
        raise ValueError("Неизвестная станция")
    mode = query.get("mode", "archive")
    if mode not in {"archive", "manual"}:
        raise ValueError("Выберите архив или ввод наблюдений")

    archive = fit["archive"]
    day_text = query.get("day", archive["default_source_day"] if mode == "archive" else None)
    source = input_day(day_text)
    if source < date.fromisoformat(archive["first_source_day"]):
        raise ValueError(f"Выберите исходную дату с {archive['first_source_day']}, когда обучение уже завершено")
    previous, target = source - ONE_DAY, source + ONE_DAY
    if mode == "archive":
        if source > date.fromisoformat(archive["last_source_day"]):
            raise ValueError(f"Последние пригодные сутки в архиве станции — {archive['last_source_day']}")
        today, yesterday, observed = archive_values(station, previous, source, target)
    else:
        moscow_day = datetime.now(timezone(timedelta(hours=3))).date()
        if source >= moscow_day:
            raise ValueError("Укажите завершившиеся сутки по московскому времени")
        today = input_temperature(query.get("today"), "выбранный день")
        yesterday = input_temperature(query.get("yesterday"), "предыдущий день")
        observed = None

    return {
        "station": station,
        "mode": mode,
        "source_day": source.isoformat(),
        "target_day": target.isoformat(),
        "inputs": [
            {"day": previous.isoformat(), "temperature_c": yesterday},
            {"day": source.isoformat(), "temperature_c": today},
        ],
        "prediction_c": estimate(fit["coefficients"], features(target, today, yesterday)),
        "baseline_c": today,
        "observed_c": observed,
        "stations": [
            {key: value for key, value in entry.items() if key != "coefficients"}
            for entry in model["stations"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Обучение и оценка суточной модели температуры")
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
    for station in model["stations"]:
        test = station["test"]
        print(f"{station['label']}: {test['days']} суток, MAE {test['model_mae_c']:.3f} °C, "
              f"база {test['baseline_mae_c']:.3f} °C")


if __name__ == "__main__":
    main()
