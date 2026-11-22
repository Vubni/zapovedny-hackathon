"""Проверка суточного JSON метеожурнала средствами стандартной библиотеки."""

import argparse
from collections import Counter
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import sys


STATIONS = {"ame60": "АМЕ-60", "kr1": "Кр1", "kr2": "Кр2"}
PARAMETERS = {
    "t_air": "Температура воздуха, °C",
    "rh": "Относительная влажность, %",
    "wind_speed": "Скорость ветра, м/с",
    "wind_gust": "Порыв ветра, м/с",
    "wind_dir": "Направление ветра, °",
    "soil_05": "Температура почвы на 5 см, °C",
    "soil_10": "Температура почвы на 10 см, °C",
    "soil_20": "Температура почвы на 20 см, °C",
    "pressure_hpa": "Давление, гПа",
}
SOIL_PARAMS = {"soil_05", "soil_10", "soil_20"}
DAY_FORMAT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
ROW_FIELDS = {"time", "value", "flags", "status", "line"}
SUMMARY_FIELDS = {"slots", "station_slots", "threshold", "valid", "mean", "n"}
FILE_FIELDS = {"station", "param", "day", "source", "rows"} | SUMMARY_FIELDS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number_or_null(value):
    try:
        return value is None or (type(value) in (int, float) and math.isfinite(value))
    except OverflowError:
        return False


def mean_value(values, param):
    if param != "wind_dir":
        return math.fsum(values) / len(values)
    radians = [math.radians(value) for value in values]
    x = math.fsum(math.cos(angle) for angle in radians)
    y = math.fsum(math.sin(angle) for angle in radians)
    if math.hypot(x, y) <= 1e-12 * len(values):
        return None
    angle = math.degrees(math.atan2(y, x)) % 360
    return 0.0 if angle == 360 else angle


def replay(data):
    require(isinstance(data, dict) and FILE_FIELDS <= data.keys(), "Нет обязательных полей экспорта")
    station, param, day, source, rows = (data[field] for field in ("station", "param", "day", "source", "rows"))
    require(isinstance(station, str) and station in STATIONS, "Неизвестная станция")
    absent = {"pressure_hpa"} if station == "ame60" else SOIL_PARAMS
    available = PARAMETERS.keys() - absent
    require(isinstance(param, str) and param in available, "Параметр недоступен на этой станции")
    require(isinstance(day, str) and re.fullmatch(DAY_FORMAT, day), "Дата должна быть в формате ГГГГ-ММ-ДД")
    date.fromisoformat(day)
    require(source is None or isinstance(source, str), "Поле source должно содержать имя файла или null")
    require(isinstance(rows, list), "Поле rows должно содержать список наблюдений")

    conflicts_by_time = {}
    station_slots, parameter_slots = set(), set()
    known_flags = {f"{key}:{reason}" for key in available for reason in ("range", "fault")}
    known_flags.add("dup_conflict")
    parameter_flags = {f"{param}:range", f"{param}:fault"}
    values = []
    flags_count = Counter()
    missing = 0
    for index, row in enumerate(rows, start=1):
        label = f"Запись {index}"
        require(isinstance(row, dict) and ROW_FIELDS <= row.keys(), f"{label} без обязательных полей")
        time, value, flag_text = row["time"], row["value"], row["flags"]
        require(isinstance(time, str) and re.fullmatch(DAY_FORMAT + r" [0-9]{2}:[0-9]{2}", time),
                f"{label} содержит неверный формат срока")
        try:
            timestamp = datetime.strptime(time, "%Y-%m-%d %H:%M")
        except ValueError as error:
            raise ValueError(f"{label} содержит неверный срок {time}") from error
        require(timestamp.date().isoformat() == day, f"{label} содержит срок за другую дату {time}")
        require(number_or_null(value), f"{label} должна содержать конечное число или null")
        require(isinstance(row["status"], str) and type(row["line"]) is int and row["line"] > 0,
                f"{label} содержит неверный статус или номер строки")
        require(isinstance(flag_text, str), f"{label} содержит неверный список флагов")
        flags = set(flag_text.split(";")) if flag_text else set()
        require(flags <= known_flags, f"{label} содержит неизвестный флаг")
        flags_count.update(flags)
        conflict = "dup_conflict" in flags
        require(time not in conflicts_by_time or (conflict and conflicts_by_time[time]),
                f"Повторный срок без dup_conflict: {time}")
        conflicts_by_time[time] = conflict
        if conflict:
            continue
        slot = time if station == "ame60" else timestamp.hour
        station_slots.add(slot)
        if flags & parameter_flags:
            continue
        if value is None:
            missing += 1
            continue
        parameter_slots.add(slot)
        values.append(value)

    threshold = 72 if station == "ame60" else 18
    valid = len(parameter_slots) >= threshold
    result = {
        "slots": len(parameter_slots),
        "station_slots": len(station_slots),
        "threshold": threshold,
        "valid": valid,
        "mean": mean_value(values, param) if valid else None,
        "n": len(values),
    }
    return result, flags_count, missing


def compare(data, result):
    for key, expected in result.items():
        actual = data[key]
        if key == "mean":
            matches = number_or_null(actual) and (
                actual is expected if actual is None or expected is None
                else math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9)
            )
        else:
            matches = type(actual) is type(expected) and actual == expected
        require(matches, f"Итог {key} расходится с расчётом. В JSON {actual!r}, по строкам {expected!r}")


def report(data, result, flags, missing):
    station, param = data["station"], data["param"]
    total, unit = (96, "сроков") if station == "ame60" else (24, "ч")
    mean = result["mean"]
    coverage = f"из {total} {unit}"
    print(f"{data['day']} · {STATIONS[station]} · {PARAMETERS[param]}")
    print(f"Источник {data['source'] or '—'}")
    rows = [
        ("Строк в JSON", len(data["rows"])),
        ("Покрытие станции", f"{result['station_slots']} {coverage}"),
        ("Покрытие параметра", f"{result['slots']} {coverage}"),
        ("Порог покрытия", f"{result['threshold']} {unit}"),
        ("Сутки по параметру", "валидны" if result["valid"] else "невалидны"),
        ("Пропуски параметра", missing),
        ("Флаги в исходных строках", ", ".join(f"{flag} × {count}" for flag, count in sorted(flags.items())) or "—"),
        ("Принято наблюдений (n)", result["n"]),
        ("Среднее за сутки", f"{mean:.6g}" if mean is not None else "—"),
    ]
    width = max(len(label) for label, _ in rows)
    print(f"{'Показатель':<{width}} | Значение")
    print(f"{'-' * width}-+---------")
    for label, value in rows:
        print(f"{label:<{width}} | {value}")
    print("Итоги JSON совпадают с пересчётом.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path, help="суточный экспорт JSON метеожурнала")
    args = parser.parse_args()
    try:
        with args.json_file.open(encoding="utf-8-sig") as stream:
            data = json.load(stream)
        result, flags, missing = replay(data)
        compare(data, result)
        report(data, result, flags, missing)
    except (OSError, ValueError, OverflowError) as error:
        print(f"Ошибка проверки JSON. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
