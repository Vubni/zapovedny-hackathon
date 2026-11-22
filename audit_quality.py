#!/usr/bin/env python3
"""Независимая сверка архива метеонаблюдений и расчётной базы."""

import argparse
import calendar
import cmath
import csv
import io
import json
import math
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from itertools import groupby
from pathlib import Path, PurePosixPath
from statistics import mean

ROOT = Path(__file__).resolve().parent
COMMON = ("t_air", "rh", "wind_speed", "wind_gust", "wind_dir")
PARAMS = {
    "ame60": (*COMMON, "soil_05", "soil_10", "soil_20"),
    "kr1": (*COMMON, "pressure_hpa"),
    "kr2": (*COMMON, "pressure_hpa"),
}
RANGES = {
    "t_air": (-50, 45), "rh": (0, 100), "wind_speed": (0, 60),
    "wind_gust": (0, 75), "wind_dir": (0, 360), "soil_05": (-40, 45),
    "soil_10": (-40, 45), "soil_20": (-40, 45), "pressure_hpa": (940, 1050),
}
PRESSURE_DAYS = {
    ("kr1", "2024-02-20"): "pressure_hours_february",
    ("kr1", "2024-04-01"): "pressure_valid_day",
    ("kr1", "2024-04-02"): "pressure_flagged_day",
    ("kr1", "2024-04-28"): "station_hours",
    ("kr1", "2024-04-29"): "pressure_hours_april",
    ("kr1", "2024-04-30"): "pressure_valid_varied_day",
    ("kr2", "2024-04-01"): "kr2_pressure_control",
}
DAY_FIELDS = (
    "n", "slots", "station_slots", "raw_n", "flagged", "missing",
    "conflicts", "valid", "mean", "min", "max", "sin_sum", "cos_sum",
)
MONTH_FIELDS = ("days", "valid_days", "longest_gap", "valid", "mean", "min", "max")
STATION_FIELDS = MONTH_FIELDS[:4]


@dataclass(frozen=True)
class Observation:
    line: int
    fields: dict
    flags: frozenset

    @property
    def timestamp(self):
        return self.fields["datetime"]


def read_csv(archive, member):
    return io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")


def source_evidence(examples, case, station, member, observations):
    examples.extend(
        {
            "case": case, "station": station, "archive_member": member,
            "csv_line": row.line, **row.fields,
        }
        for row in observations
    )


def slot(station, timestamp):
    return timestamp if station == "ame60" else timestamp[:13]


def angle(vector, n):
    if abs(vector) <= n * 1e-12:
        return None
    degrees = math.degrees(cmath.phase(vector)) % 360
    return 0.0 if degrees == 360 else degrees


def get_day(station, param, raw, clean, station_slots):
    kept = []
    flagged = missing = 0
    low, high = RANGES[param]
    for row in clean:
        value = row.fields.get(param)
        if param in row.flags:
            flagged += 1
        elif not value:
            missing += 1
        else:
            number = Decimal(value)
            if not low <= number <= high:
                raise ValueError(f"Вне диапазона без флага: {station} {row.timestamp} {param}={value}")
            kept.append((row.timestamp, number))
    n = len(kept)
    slots = len({slot(station, ts) for ts, _ in kept})
    valid = slots >= (72 if station == "ame60" else 18)
    values = [value for _, value in kept]
    vector = None
    avg = minimum = maximum = None
    if param == "wind_dir" and values:
        vector = sum(cmath.rect(1, math.radians(value)) for value in values)
        if valid:
            avg = angle(vector, n)
    elif valid:
        avg, minimum, maximum = mean(values), min(values), max(values)
    return dict(zip(
        DAY_FIELDS, (n, slots, station_slots, len(raw), flagged, missing,
                     len(raw) - len(clean), valid, avg, minimum, maximum,
                     vector.imag if vector is not None else None,
                     vector.real if vector is not None else None),
    ))


def get_coverage(validity):
    days_valid = sum(validity)
    gap = max((sum(1 for _ in days) for valid, days in groupby(validity) if not valid), default=0)
    return dict(zip(STATION_FIELDS, (len(validity), days_valid, gap,
                                    len(validity) - days_valid <= 10 and gap < 5)))


def get_month(rows, param):
    result = get_coverage([row["valid"] for row in rows])
    avg = minimum = maximum = None
    if result["valid"]:
        kept = [row for row in rows if row["valid"]]
        if param == "wind_dir":
            vectors = [cmath.rect(1, math.radians(row["mean"])) for row in kept if row["mean"] is not None]
            avg = angle(sum(vectors), len(vectors))
        else:
            avg, minimum, maximum = (
                mean(row["mean"] for row in kept),
                min(row["min"] for row in kept),
                max(row["max"] for row in kept),
            )
    return {**result, "mean": avg, "min": minimum, "max": maximum}


def source_year(archive, member, station, year, counts, flags, examples):
    by_day = defaultdict(list)
    timestamps = defaultdict(list)
    sample_flags = {
        ("ame60", 2016, "soil_05:fault"): "soil_sensor_fault",
        ("ame60", 2021, "wind_dir:range"): "wind_2021_range",
        ("ame60", 2021, "wind_dir:fault"): "wind_2021_fault",
    }
    with read_csv(archive, member) as source:
        reader = csv.DictReader(source)
        columns = set(reader.fieldnames) - {"datetime", "qc_flag"}
        for line, fields in enumerate(reader, start=2):
            row_flags = fields["qc_flag"].split(";") if fields["qc_flag"] else ()
            flags.update((station, year, flag) for flag in row_flags)
            row = Observation(line, fields, frozenset(flag.split(":")[0] for flag in row_flags))
            timestamps[row.timestamp].append(row)
            by_day[row.timestamp[:10]].append(row)
            for flag in row_flags:
                example_key = sample_flags.get((station, year, flag))
                if example_key:
                    source_evidence(examples, example_key, station, member, [row])
                    del sample_flags[station, year, flag]
            if line == 2 and station == "ame60" and year == 1998:
                source_evidence(examples, "wind_1998_absent", station, member, [row])
    conflicts = [rows for rows in timestamps.values() if len(rows) > 1]
    flagged_times = {ts for ts, rows in timestamps.items() if any("dup_conflict" in row.flags for row in rows)}
    expected_times = {rows[0].timestamp for rows in conflicts}
    if flagged_times != expected_times or any(
        "dup_conflict" not in row.flags for rows in conflicts for row in rows
    ):
        raise ValueError(f"Несогласованность конфликтных сроков и флагов: {member}")
    for rows in conflicts:
        measurements = {tuple(row.fields[param] for param in sorted(columns)) for row in rows}
        if len(measurements) == 1:
            raise ValueError(f"Конфликтный срок без различий значений: {member} {rows[0].timestamp}")
    offgrid = [ts for ts in timestamps if station == "ame60" and int(ts[-2:]) % 15]
    if conflicts and not counts["conflict_times"]:
        source_evidence(examples, "conflicting_timestamp", station, member, conflicts[0])
    if station == "ame60" and year == 2018:
        source_evidence(examples, "ame60_offgrid", station, member, by_day["2018-05-11"])
    for (source_station, day), name in PRESSURE_DAYS.items():
        if source_station == station and day in by_day:
            source_evidence(examples, name, station, member, by_day[day])
    counts.update({
        "raw_rows": sum(map(len, by_day.values())),
        "conflict_times": len(conflicts),
        "conflict_rows": sum(map(len, conflicts)),
        "ame60_offgrid_times": len(offgrid),
    })
    return by_day, columns, len(offgrid)


def recalculate(archive_path):
    daily, monthly, station_monthly = {}, {}, {}
    counts, flags = Counter(), Counter()
    examples, inventory, published = [], [], {}
    with zipfile.ZipFile(archive_path) as archive:
        members = {}
        for member in sorted(archive.namelist()):
            file = PurePosixPath(member)
            if file.name == "coverage_by_month.csv":
                with read_csv(archive, member) as source:
                    for row in csv.DictReader(source):
                        key = (row["station"].lower(), f'{row["year"]}-{int(row["month"]):02}')
                        published[key] = {
                            "days": int(row["days_in_month"]), "valid_days": int(row["valid_days"]),
                            "valid": row["month_valid"] == "True",
                        }
            if file.suffix != ".csv" or "data" not in file.parts:
                continue
            station, year_text = file.stem.split("_")
            members[station, int(year_text)] = member

        ranges = {"ame60": range(1992, 2022), "kr1": range(2022, 2026), "kr2": range(2022, 2026)}
        months_with_files = set()
        for station, years in ranges.items():
            for year in years:
                member = members.get((station, year))
                by_day, columns, offgrid = {}, set(), 0
                if member:
                    by_day, columns, offgrid = source_year(archive, member, station, year, counts, flags, examples)
                    months_with_files.update((station, f"{year}-{month:02}") for month in range(1, 13))
                inventory.append({
                    "station": station, "year": year, "file": member, "columns": sorted(columns),
                    "observations": sum(map(len, by_day.values())), "offgrid_times": offgrid,
                })
                for month in range(1, 13):
                    month_key = f"{year}-{month:02}"
                    coverage, parameter_days = [], defaultdict(list)
                    for day in range(1, calendar.monthrange(year, month)[1] + 1):
                        day_key = f"{month_key}-{day:02}"
                        raw = by_day.get(day_key, [])
                        clean = [row for row in raw if "dup_conflict" not in row.flags]
                        station_slots = len({slot(station, row.timestamp) for row in clean})
                        coverage.append(station_slots >= (72 if station == "ame60" else 18))
                        for param in PARAMS[station]:
                            result = get_day(station, param, raw, clean, station_slots)
                            daily[station, param, day_key] = result
                            parameter_days[param].append(result)
                    station_monthly[station, month_key] = get_coverage(coverage)
                    for param, days in parameter_days.items():
                        monthly[station, param, month_key] = get_month(days, param)

    if months_with_files != published.keys():
        raise ValueError("Список пересчитанных месяцев расходится с опубликованным")
    differences = [
        key for key, fields in published.items()
        if any(station_monthly[key][field] != value for field, value in fields.items())
    ]
    if differences or len(published) != 420:
        raise ValueError(f"Опубликованное покрытие: {len(published)} месяцев, расхождения: {differences}")
    return daily, monthly, station_monthly, inventory, examples, counts, flags


def diagnostic_examples(daily, monthly, station, examples):
    soil_day = next(row["datetime"][:10] for row in examples if row["case"] == "soil_sensor_fault")
    control = {
        "kr1_2024_pressure": {
            "raw_n": sum(row["raw_n"] for (s, p, d), row in daily.items()
                         if (s, p) == ("kr1", "pressure_hpa") and d.startswith("2024")),
            "flagged": sum(row["flagged"] for (s, p, d), row in daily.items()
                           if (s, p) == ("kr1", "pressure_hpa") and d.startswith("2024")),
            "valid_days": sum(row["valid_days"] for (s, p, m), row in monthly.items()
                              if (s, p) == ("kr1", "pressure_hpa") and m.startswith("2024")),
            "valid_months": sum(row["valid"] for (s, p, m), row in monthly.items()
                                if (s, p) == ("kr1", "pressure_hpa") and m.startswith("2024")),
        },
        "kr1_april_station": station["kr1", "2024-04"],
        "kr1_april_pressure": monthly["kr1", "pressure_hpa", "2024-04"],
        "kr1_april_air": monthly["kr1", "t_air", "2024-04"],
        "kr2_april_pressure": monthly["kr2", "pressure_hpa", "2024-04"],
        "kr1_may_pressure": monthly["kr1", "pressure_hpa", "2024-05"],
        "kr1_april_01_pressure": daily["kr1", "pressure_hpa", "2024-04-01"],
        "kr1_april_02_pressure": daily["kr1", "pressure_hpa", "2024-04-02"],
        "kr1_april_02_air": daily["kr1", "t_air", "2024-04-02"],
        "kr1_april_28_air": daily["kr1", "t_air", "2024-04-28"],
        "kr1_april_29_pressure": daily["kr1", "pressure_hpa", "2024-04-29"],
        "kr1_april_30_pressure": daily["kr1", "pressure_hpa", "2024-04-30"],
        "kr1_february_20_pressure": daily["kr1", "pressure_hpa", "2024-02-20"],
        "soil_fault_example": {
            "date": soil_day, "soil_05": daily["ame60", "soil_05", soil_day],
            "soil_10": daily["ame60", "soil_10", soil_day], "t_air": daily["ame60", "t_air", soil_day],
        },
        "ame60_november_1992_station": station["ame60", "1992-11"],
        "ame60_february_2017_soil": monthly["ame60", "soil_05", "2017-02"],
        "ame60_offgrid_day": daily["ame60", "t_air", "2018-05-11"],
    }
    return control


def compare_db(path, specifications):
    if not path.is_file():
        raise FileNotFoundError(path)
    checked = {}
    errors = []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for table, keys, fields, calculated in specifications:
            source = connection.execute(f"SELECT {', '.join((*keys, *fields))} FROM {table}")
            present = set()
            difference_count = 0
            for row in source:
                key = row[:len(keys)]
                expected = calculated.get(key)
                values = row[len(keys):]
                if key in present:
                    differences = ["повторяющийся ключ"]
                elif expected is None:
                    differences = ["лишняя строка"]
                else:
                    differences = []
                    for field, actual in zip(fields, values):
                        wanted = expected[field]
                        if isinstance(wanted, (float, Decimal)) and actual is not None:
                            delta = abs(float(wanted) - actual)
                            if field == "mean" and len(key) == 3 and key[1] == "wind_dir":
                                delta = min(delta, abs(360 - delta))
                            same = delta <= 1e-8
                        else:
                            same = actual == wanted
                        if not same:
                            differences.append(f"{field}={actual!r}, ожидалось {wanted!r}")
                present.add(key)
                if differences:
                    difference_count += 1
                    if len(errors) < 20:
                        errors.append(f"{table} {key}: {', '.join(differences)}")
            missing = calculated.keys() - present
            if missing and len(errors) < 20:
                errors.append(f"{table}: отсутствует {len(missing)} строк, пример {min(missing)}")
            checked[table] = {
                "calculated_rows": len(calculated), "database_keys": len(present),
                "different_rows": difference_count, "missing_rows": len(missing),
            }
    return {"tables": checked, "errors": errors}


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def keyed_rows(data, fields):
    for key, row in sorted(data.items()):
        yield {**dict(zip(fields, key)), **row}


def control_days(daily, examples):
    exam_dates = {(row["station"], row["datetime"][:10]) for row in examples}
    return {
        (station, param, day): row for (station, param, day), row in daily.items()
        if (station, day) in exam_dates
        or station in ("kr1", "kr2") and param in ("pressure_hpa", "t_air") and day.startswith("2024-04")
        or (station, param) == ("kr1", "pressure_hpa") and day.startswith("2024-05")
    }


def integer(number):
    return f"{number:,}".replace(",", "\u202f")


def decimal(number, places):
    return f"{number:.{places}f}".replace(".", ",")


def write_report(path, snapshot, examples):
    c = snapshot["controls"]
    fact = snapshot["archive_counts"]
    apr = c["kr1_april_pressure"]
    air = c["kr1_april_air"]
    station = c["kr1_april_station"]
    kr2 = c["kr2_april_pressure"]
    good = c["kr1_april_01_pressure"]
    varied = c["kr1_april_30_pressure"]
    may = c["kr1_may_pressure"]
    irregular_april = c["kr1_april_29_pressure"]
    irregular_february = c["kr1_february_20_pressure"]
    soil = next(row for row in examples if row["case"] == "soil_sensor_fault")
    offgrid = [row for row in examples if row["case"] == "ame60_offgrid"]
    duplicates = [row for row in examples if row["case"] == "conflicting_timestamp"]
    fault = next(row for row in examples if row["case"] == "wind_2021_fault")
    period = c["kr1_2024_pressure"]
    gap = c["ame60_november_1992_station"]
    total_gap = c["ame60_february_2017_soil"]
    db = snapshot["database_comparison"]
    report = [
        "# Независимая проверка метеоархива",
        "",
        "## Покрытие и правила",
        "",
        f"Пересчёт по {fact['observation_files']} CSV внутри ZIP обработал "
        f"{integer(fact['raw_rows'])} исходных строк. Все 420 месяцев из `coverage_by_month.csv` совпали "
        "по числу календарных и валидных дней и признаку валидности. В выдачу также включены "
        "36 пустых месяцев АМЕ-60 за 2000–2002 годы.",
        "",
        "На уровне станции сутки АМЕ-60 проходят при 72 уникальных записанных сроках из 96. "
        "Для Кр1 и Кр2 нужны хотя бы 18 разных часов из 24. На уровне параметра порог применяется "
        "повторно к значениям после исключения пустых ячеек, флагов этого параметра и конфликтных сроков. "
        "Для среднего за сутки знаменатель — число пригодных измерений (`n`), "
        "включая по два и более измерений в одном часе. Для среднего за месяц знаменатель — число "
        "валидных суток, каждое со своим средним и равным весом. Минимум и максимум берутся "
        "из пригодных измерений этих суток. Углы ветра усредняются по единичным векторам.",
        "",
        "Месяц проходит при обоих условиях. Суммарно не больше 10 невалидных суток и самая длинная "
        f"серия короче 5 суток. Ноябрь 1992 года АМЕ-60 имеет {gap['valid_days']} из {gap['days']} "
        f"валидных суток и серию из {gap['longest_gap']} суток, поэтому месяц исключён. "
        f"В феврале 2017 года у почвы на 5 см {total_gap['valid_days']} из {total_gap['days']} "
        f"валидных суток. Самая длинная серия пропусков длится {total_gap['longest_gap']} суток, "
        f"но суммарно пропущено {total_gap['days'] - total_gap['valid_days']} суток, и месяц исключён.",
        "",
        "## Давление на Кр1 и Кр2 в 2024 году",
        "",
        "| Станция и показатель | Месяц | Валидные сутки | Максимальный пропуск | Месяц валиден |",
        "|---|---|---:|---:|---|",
    ]
    for label, month, row in (
        ("Кр1 · покрытие станции", "апрель", station),
        ("Кр1 · давление", "апрель", apr),
        ("Кр1 · температура воздуха", "апрель", air),
        ("Кр2 · давление", "апрель", kr2),
        ("Кр1 · давление", "май", may),
    ):
        valid = "да" if row["valid"] else "нет"
        report.append(
            f"| {label} | {month} | {row['valid_days']} из {row['days']} | {row['longest_gap']} | {valid} |"
        )
    report.extend([
        "",
        f"- За год у Кр1 {integer(period['raw_n'])} строк и {integer(period['flagged'])} флага давления. "
        f"По давлению валидны {period['valid_days']} суток и {period['valid_months']} месяцев. "
        "Апрель — единственный исключённый месяц давления.",
        f"- 1 апреля Кр1 давление проходит. Среднее {decimal(good['mean'] * good['n'], 2)} / {good['n']} "
        f"= {decimal(good['mean'], 2)} гПа. Из {good['raw_n']} строк дня {good['flagged']} исключены по флагу, "
        f"{good['n']} пригодных значений занимают {good['slots']} часов из 24. "
        f"Минимум и максимум равны {decimal(good['min'], 2)} гПа. 30 апреля среднее "
        f"{decimal(varied['mean'] * varied['n'], 2)} / {varied['n']} = {decimal(varied['mean'], 3)} гПа, "
        f"измерения за {varied['slots']} часа.",
        f"- 2 апреля Кр1 все {c['kr1_april_02_pressure']['flagged']} значений давления помечены. "
        f"На тех же строках температура воздуха проходит с {c['kr1_april_02_air']['n']} "
        f"значениями за {c['kr1_april_02_air']['slots']} часа.",
        f"- 29 апреля Кр1 у давления {irregular_april['n']} пригодное измерение за "
        f"{irregular_april['slots']} часов. 20 февраля аналогично {irregular_february['n']} "
        f"измерений за {irregular_february['slots']} часов. Оба дня исключены. 28 апреля у самой "
        f"станции Кр1 {c['kr1_april_28_air']['raw_n']} строк за "
        f"{c['kr1_april_28_air']['station_slots']} часов, поэтому сутки исключены и на уровне станции.",
        f"- В мае Кр1 среднее давление по {may['valid_days']} суточным средним равно "
        f"{decimal(may['mean'], 4)} гПа. Экстремумы {decimal(may['min'], 2)} и "
        f"{decimal(may['max'], 2)} гПа. Среднее за апрель у Кр2 равно "
        f"{decimal(kr2['mean'], 4)} гПа по {kr2['valid_days']} суточным средним.",
        "",
        "## Контроль исходных строк",
        "",
        f"- Конфликты найдены в {fact['conflict_times']} сроках, исключены все "
        f"{fact['conflict_rows']} строки. Например, в `{PurePosixPath(duplicates[0]['archive_member']).name}` "
        f"срок {duplicates[0]['datetime']} встречается в строках "
        + " и ".join(str(row["csv_line"]) for row in duplicates)
        + ". Обе строки помечены `dup_conflict`.",
        f"- У АМЕ-60 {integer(fact['ame60_offgrid_times'])} уникальных сроков вне четвертьчасовой сетки, "
        f"из них {integer(fact['ame60_2018_offgrid_times'])} в 2018 году. В файле `ame60_2018.csv` "
        f"строки {offgrid[0]['csv_line']}–{offgrid[-1]['csv_line']} за 11 мая дают "
        f"{c['ame60_offgrid_day']['station_slots']} сроков от 00:12 до 23:57 с исходными минутами. "
        "Сутки валидны.",
        f"- В 2016 году `soil_05:fault` отмечено {integer(snapshot['known_flags']['soil_2016_fault'])} строк. "
        f"На сроке {soil['datetime']} в строке {soil['csv_line']} значение `soil_05` "
        f"{decimal(Decimal(soil['soil_05']), 1)} °C исключено. "
        f"Температура воздуха {decimal(Decimal(soil['t_air']), 1)} °C и температура почвы "
        f"на глубине 10 см {decimal(Decimal(soil['soil_10']), 1)} °C сохранены.",
        "- В 1998 году в CSV АМЕ-60 отсутствует столбец направления ветра. В 2021 году столбец есть. "
        f"В нём {integer(snapshot['known_flags']['wind_2021_range'])} флагов диапазона и "
        f"{integer(snapshot['known_flags']['wind_2021_fault'])} флага сбоя. "
        f"Даже направление {fault['wind_dir']}° на сроке {fault['datetime']} помечено флагом сбоя. "
        "В обоих годах у направления 0 пригодных измерений.",
        "",
        "## Сверка с расчётной базой",
        "",
    ])
    if db is None:
        report.append("Пересчёт по ZIP выполнен. Для сравнения запустите команду с `--verify`.")
    else:
        report.extend([
            "| Таблица SQLite | Пересчитано строк | Строк с расхождениями | Строк отсутствует |",
            "|---|---:|---:|---:|",
        ])
        for table, row in db["tables"].items():
            report.append(
                f"| `{table}` | {row['calculated_rows']} | {row['different_rows']} | {row['missing_rows']} |"
            )
        report.extend(["", "Числовые агрегаты сверены с абсолютным допуском 10⁻⁸."])
        if db["errors"]:
            report.extend(["", *("- " + error for error in db["errors"])])
    report.extend([
        "",
        "Из корня проекта выполните `python3 audit_quality.py --verify results/weather.sqlite`.",
        "",
        "- [Покрытие по станциям](station_months.csv) и [покрытие по параметрам](parameter_months.csv).",
        "- [Суточные расчёты контрольных дней](control_days.csv) и [строки исходного архива](source_examples.csv).",
        "- [Сводка проверки](audit.json), [перечень файлов, столбцов и флагов](archive_inventory.csv).",
    ])
    path.write_text("\n".join(report) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "data/meteo_hack_dataset.zip")
    parser.add_argument("--out", type=Path, default=ROOT / "results/audit")
    parser.add_argument("--verify", type=Path, help="Сопоставить пересчёт с SQLite")
    args = parser.parse_args()

    daily, monthly, station_monthly, inventory, examples, counts, flags = recalculate(args.archive)
    counts["observation_files"] = sum(row["file"] is not None for row in inventory)
    counts["ame60_2018_offgrid_times"] = next(
        row["offgrid_times"] for row in inventory if (row["station"], row["year"]) == ("ame60", 2018)
    )
    controls = diagnostic_examples(daily, monthly, station_monthly, examples)
    known_flags = {
        "soil_2016_fault": flags["ame60", 2016, "soil_05:fault"],
        "wind_2021_range": flags["ame60", 2021, "wind_dir:range"],
        "wind_2021_fault": flags["ame60", 2021, "wind_dir:fault"],
    }
    wind_valid_measurements = {
        str(year): sum(row["n"] for (s, p, d), row in daily.items()
                       if (s, p) == ("ame60", "wind_dir") and d.startswith(str(year)))
        for year in (1998, 2021)
    }
    official_kr1 = {"raw_n": 17247, "flagged": 1362, "valid_days": 334, "valid_months": 11}
    if (controls["kr1_2024_pressure"] != official_kr1
            or counts["conflict_times"] != 36 or counts["conflict_rows"] != 72
            or counts["ame60_offgrid_times"] != 5022 or counts["ame60_2018_offgrid_times"] != 4909
            or any(wind_valid_measurements.values())):
        raise ValueError("Пересчёт расходится с контрольными значениями паспорта архива")

    db = None
    if args.verify:
        db = compare_db(args.verify, [
            ("station_monthly", ("station", "month"), STATION_FIELDS, station_monthly),
            ("monthly", ("station", "param", "month"), MONTH_FIELDS, monthly),
            ("daily", ("station", "param", "day"), DAY_FIELDS, daily),
        ])
    counts["published_station_months_matched"] = 420
    digest = sha256(args.archive.read_bytes()).hexdigest()
    snapshot = {
        "archive_file": args.archive.name, "archive_sha256": digest, "archive_counts": dict(counts),
        "known_flags": known_flags, "ame60_wind_dir_eligible_observations": wind_valid_measurements,
        "controls": controls, "database_comparison": db,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "audit.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=float) + "\n", encoding="utf-8",
    )
    write_csv(args.out / "station_months.csv", ("station", "month", *STATION_FIELDS),
              keyed_rows(station_monthly, ("station", "month")))
    write_csv(args.out / "parameter_months.csv", ("station", "param", "month", *MONTH_FIELDS),
              keyed_rows(monthly, ("station", "param", "month")))
    write_csv(args.out / "control_days.csv", ("station", "param", "day", *DAY_FIELDS),
              keyed_rows(control_days(daily, examples), ("station", "param", "day")))
    write_csv(args.out / "source_examples.csv",
              ("case", "station", "archive_member", "csv_line", "datetime", *RANGES, "qc_flag"), examples)
    flag_counts = defaultdict(list)
    for (station, year, flag), n in sorted(flags.items()):
        flag_counts[station, year].append(f"{flag}={n}")
    for row in inventory:
        row["columns"] = " ".join(row["columns"])
        row["flags"] = " ".join(flag_counts[row["station"], row["year"]])
    write_csv(args.out / "archive_inventory.csv",
              ("station", "year", "file", "observations", "offgrid_times", "columns", "flags"), inventory)
    write_report(args.out / "ОТЧЁТ.md", snapshot, examples)

    print("420 из 420 опубликованных месяцев совпали.")
    if db:
        for table, row in db["tables"].items():
            print(f"{table}: {row['calculated_rows']} сверено, {row['different_rows']} расхождений, "
                  f"{row['missing_rows']} отсутствует.")
        if db["errors"]:
            print("\n".join(db["errors"]))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
