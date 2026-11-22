"""Сформировать отдельный отчёт из одного CSV или JSON со сроками наблюдений."""

import argparse
import csv
import io
import json
import sqlite3
from contextlib import closing
from datetime import date
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from analytics import PARAMS, bounds
from build_data import (
    SCHEMA, STATIONS, Observations, Station, daily_record, export_csv, monthly_record, read_csv,
    read_records, sha256, validate_headers, write_year,
)


REPORT_FORMAT = "weather-station-report-v1"
EXPORTS = {
    "daily.csv": ("daily", "station, param, day"),
    "monthly.csv": ("monthly", "station, param, month"),
    "station_coverage.csv": ("station_monthly", "station, month"),
}
CONTENT_FORMATS = {"text/csv": "csv", "application/json": "json"}
MAX_UPLOAD = 5 * 1024 * 1024


def import_passport(station_id):
    if isinstance(station_id, str) and station_id in STATIONS:
        return STATIONS[station_id], 96 if station_id == "ame60" else 24
    from station_catalog import catalog
    for passport in catalog():
        if passport["id"] == station_id:
            size = passport["slot_minutes"]
            station = Station(range(1, 10000), tuple(item["id"] for item in passport["params"]),
                              station_id, size, passport["minimum_slots"])
            expected = 96 if size == 1 and passport.get("profile") == "ame60" else 1440 // size
            return station, expected
    raise ValueError("Выберите станцию из списка")


class JSONObject(list):
    """Preserve JSON field names through validation, including repeated names."""


def json_records(file, source, station):
    try:
        data = json.load(file, object_pairs_hook=JSONObject, parse_constant=lambda _: object())
    except json.JSONDecodeError as error:
        raise ValueError(f"{source}:{error.lineno}:{error.colno}: некорректный JSON: {error.msg}") from None
    if type(data) is not list:
        raise ValueError(f"{source}: ожидается JSON-массив объектов наблюдений")
    for index, entry in enumerate(data, 1):
        location = f"{source}: элемент {index}"
        if not isinstance(entry, JSONObject):
            raise ValueError(f"{location}: ожидается объект наблюдения")
        validate_headers([name for name, _ in entry], location, station)
        row = {}
        for name, value in entry:
            if name in {"datetime", "qc_flag"}:
                if not isinstance(value, str):
                    raise ValueError(f"{location}: {name} должен быть строкой")
                row[name] = value
            elif value is None:
                row[name] = ""
            elif type(value) in (int, float, str):
                row[name] = str(value)
            else:
                raise ValueError(f"{location}: {name} должен быть числом, числовой строкой или null")
        yield location, row


def read_input_stream(file, source, source_format, station, year):
    try:
        if source_format == "csv":
            observations, counts = read_csv(file, source, station, year)
        else:
            observations, counts = read_records(json_records(file, source, station), station, year)
    except UnicodeDecodeError:
        raise ValueError(f"{source}: файл должен быть в кодировке UTF-8") from None
    if not counts["raw_rows"]:
        raise ValueError(f"{source}: нет строк наблюдений")
    return observations, counts


def read_input(source, station, year):
    suffix = source.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise ValueError(f"{source}: поддерживаются расширения .csv и .json")
    with source.open(encoding="utf-8-sig", newline="") as file:
        return read_input_stream(file, source, suffix[1:], station, year)


def preview_date(value, fallback, year):
    if not value:
        return date.fromisoformat(fallback)
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("Укажите даты в формате ГГГГ-ММ-ДД") from None
    if parsed.year != year:
        raise ValueError(f"Даты просмотра должны относиться к {year} году")
    return parsed


def preview_row(station_id, station, param, grain, members):
    start, end = members[0].day, members[-1].day
    row = {
        "period": start, "start": start, "end": end, "days": len(members),
        "n": sum(day.n for day in members), "flagged": sum(day.flagged for day in members),
        "missing": sum(day.missing for day in members), "conflicts": sum(day.conflicts for day in members),
        "station_valid_days": sum(day.station_slots >= station.minimum_slots for day in members),
        "slots": None, "station_slots": None,
    }
    if grain == "day":
        day = members[0]
        unit = "сроков" if station.slot_minutes == 1 else "часов"
        row.update(
            slots=day.slots,
            station_slots=day.station_slots,
            valid=bool(day.valid), valid_days=day.valid, longest_gap=1 - day.valid,
            mean=day.mean, min=day.minimum, max=day.maximum,
            reason=f"{day.slots} {unit} с пригодным значением при пороге {station.minimum_slots}. "
                   f"Покрытие станции — {day.station_slots}.",
        )
    else:
        month = start[:7]
        _, _, _, days, valid_days, gap, valid, mean, minimum, maximum = monthly_record(
            station_id, param, month, members
        )
        whole = (start, end) == bounds(start, "month")
        row.update(
            period=month, valid_days=valid_days, longest_gap=gap, valid=bool(valid and whole),
            mean=mean if whole else None, min=minimum if whole else None, max=maximum if whole else None,
            reason=(f"Неполных суток {days - valid_days} из {days}, подряд — до {gap}."
                    if whole else "Выбран фрагмент месяца. Для месячного расчёта укажите весь месяц."),
        )
    return row


def preview_csv(station_id, param, unit, rows):
    columns = {
        "period": "Период", "start": "Начало", "end": "Конец", "valid": "Период принят",
        "mean": "Среднее", "min": "Минимум", "max": "Максимум",
        "days": "Дней в выборке", "valid_days": "Пригодных суток параметра",
        "station_valid_days": "Пригодных суток станции", "longest_gap": "Дней подряд без покрытия",
        "slots": "Пригодных сроков или часов параметра", "station_slots": "Сроков или часов станции",
        "n": "Пригодных записей", "flagged": "Записей с флагом параметра",
        "missing": "Пустых значений", "conflicts": "Конфликтных строк", "reason": "Разбор покрытия",
    }
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Станция", "Параметр", "Единица", *columns.values()])
    for row in rows:
        writer.writerow([station_id, param, unit, *(
            ("да" if value else "нет") if isinstance(value, bool)
            else format(value, ".12g").replace(".", ",") if isinstance(value, float)
            else value
            for value in (row[key] for key in columns)
        )])
    return "\ufeff" + buffer.getvalue()


def preview_import(body, source_format, query):
    station_id = query.get("station")
    station, expected_slots = import_passport(station_id)
    param = query.get("param")
    if param not in station.params:
        raise ValueError("Станция не измеряет выбранный параметр")
    try:
        year = int(query.get("year", ""))
        if not 1 <= year <= 9999:
            raise ValueError
    except ValueError:
        raise ValueError("Укажите календарный год от 1 до 9999") from None
    grain = query.get("grain", "day")
    if grain not in {"day", "month"}:
        raise ValueError("Выберите шаг по дням или по месяцам")
    with io.TextIOWrapper(io.BytesIO(body), encoding="utf-8-sig", newline="") as file:
        observations, counts = read_input_stream(
            file, f"Загруженный {source_format.upper()}", source_format, station, year
        )
    source_start, source_end = min(observations), max(observations)
    start = preview_date(query.get("start"), source_start, year)
    end = preview_date(query.get("end"), source_end, year)
    if start > end:
        raise ValueError("Дата начала позже даты окончания")
    empty = Observations()
    dates = (date.fromordinal(i).isoformat() for i in range(start.toordinal(), end.toordinal() + 1))
    daily = (
        daily_record(station_id, param, when, observations.get(when, empty), station.minimum_slots)
        for when in dates
    )
    groups = groupby(daily, lambda day: day.day if grain == "day" else day.day[:7])
    rows = [preview_row(station_id, station, param, grain, list(group)) for _, group in groups]
    label, unit = PARAMS[param]
    return {
        "station": station_id, "year": year, "param": param, "label": label, "unit": unit, "grain": grain,
        "start": start.isoformat(), "end": end.isoformat(),
        "source_start": source_start, "source_end": source_end, **counts,
        "minimum_slots": station.minimum_slots, "expected_slots": expected_slots,
        "rows": rows, "valid_periods": sum(row["valid"] for row in rows),
        "valid_days": sum(row["valid_days"] for row in rows),
        "flagged": sum(row["flagged"] for row in rows),
        "filename": f"{station_id}_{param}_{start}_{end}_{grain}_import.csv",
        "csv": preview_csv(station_id, param, unit, rows),
    }


def existing_report(path):
    files = {*EXPORTS, "manifest.json"}
    if path.is_symlink() or not path.is_dir() or {child.name for child in path.iterdir()} != files:
        return False
    if any((path / name).is_symlink() or not (path / name).is_file() for name in files):
        return False
    try:
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return isinstance(data, dict) and data.get("format") == REPORT_FORMAT


def publish(report, output, replace):
    lock = output.with_name(f".{output.name}.station-import.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f"{output}: папка занята другим импортом ({lock.name})") from None
    try:
        exists = output.exists() or output.is_symlink()
        if exists and not replace:
            raise ValueError(f"{output}: папка уже существует. Для замены отчёта добавьте --replace")
        if exists and not existing_report(output):
            raise ValueError(f"{output}: замена доступна только для полной папки отчёта этого импортера")
        previous = report.with_name("previous")
        try:
            if exists:
                output.rename(previous)
            report.rename(output)
        except BaseException:
            if previous.exists() and not output.exists():
                previous.rename(output)
            raise
    finally:
        lock.rmdir()


def import_station(source, output, station_id, year, replace=False):
    station, _ = import_passport(station_id)
    if not 1 <= year <= 9999:
        raise ValueError("Укажите календарный год от 1 до 9999")

    source_hash = sha256(source)
    observations, counts = read_input(source, station, year)
    if sha256(source) != source_hash:
        raise ValueError(f"{source}: файл изменился во время чтения")

    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": REPORT_FORMAT,
        "source": source.name,
        "source_sha256": source_hash,
        "station": station_id,
        "year": year,
        "slot_minutes": station.slot_minutes,
        "minimum_slots": station.minimum_slots,
        **counts,
        "files": {},
    }
    with TemporaryDirectory(prefix=".station-import-", dir=output.parent) as stage:
        report = Path(stage) / "report"
        report.mkdir()
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            with db:
                write_year(db, station_id, station, year, observations)
            for filename, (table, order) in EXPORTS.items():
                path = report / filename
                export_csv(db, path, f"SELECT * FROM {table} ORDER BY {order}")
                manifest["files"][filename] = {
                    "rows": db.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                    "sha256": sha256(path),
                }
        (report / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        publish(report, output, replace)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", required=True, help="код станции из каталога")
    parser.add_argument("--year", required=True, type=int, help="календарный год всех наблюдений")
    parser.add_argument("--input", required=True, type=Path, help="исходный CSV или JSON")
    parser.add_argument("--output-dir", required=True, type=Path, help="отдельная папка отчёта")
    parser.add_argument("--replace", action="store_true", help="заменить существующий отчёт этого импортера")
    args = parser.parse_args()
    try:
        manifest = import_station(args.input, args.output_dir, args.station, args.year, args.replace)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Ошибка импорта. {error}\n")
    print(f"Отчёт сохранён в {args.output_dir.absolute()}. Прочитано строк: {manifest['raw_rows']}.")


if __name__ == "__main__":
    main()
