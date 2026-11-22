"""Build the local weather journal's clean daily and monthly aggregates."""

import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple, Optional
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = "meteo_clgz_hackathon"
SHARED = ("t_air", "rh", "wind_speed", "wind_gust", "wind_dir")
DIRECTION = "wind_dir"
LIMITS = {
    "t_air": (-50, 45),
    "rh": (0, 100),
    "wind_speed": (0, 60),
    "wind_gust": (0, 75),
    DIRECTION: (0, 360),
    "soil_05": (-40, 45),
    "soil_10": (-40, 45),
    "soil_20": (-40, 45),
    "pressure_hpa": (940, 1050),
}


@dataclass(frozen=True)
class Station:
    years: range
    params: tuple
    folder: str
    slot_minutes: int
    minimum_slots: int


STATIONS = {
    "ame60": Station(
        range(1992, 2022), SHARED + ("soil_05", "soil_10", "soil_20"), "ame60", 1, 72
    ),
    "kr1": Station(range(2022, 2026), SHARED + ("pressure_hpa",), "sokol_m1", 60, 18),
    "kr2": Station(range(2022, 2026), SHARED + ("pressure_hpa",), "sokol_m1", 60, 18),
}

SCHEMA = """
CREATE TABLE daily (
    station TEXT NOT NULL, param TEXT NOT NULL, day TEXT NOT NULL,
    n INTEGER NOT NULL, slots INTEGER NOT NULL, station_slots INTEGER NOT NULL,
    raw_n INTEGER NOT NULL, flagged INTEGER NOT NULL, missing INTEGER NOT NULL,
    conflicts INTEGER NOT NULL, valid INTEGER NOT NULL,
    mean REAL, min REAL, max REAL, sin_sum REAL, cos_sum REAL,
    PRIMARY KEY (station, param, day),
    CHECK (n + flagged + missing + conflicts = raw_n),
    CHECK (slots <= n AND slots <= station_slots)
) WITHOUT ROWID;

CREATE TABLE monthly (
    station TEXT NOT NULL, param TEXT NOT NULL, month TEXT NOT NULL,
    days INTEGER NOT NULL, valid_days INTEGER NOT NULL, longest_gap INTEGER NOT NULL,
    valid INTEGER NOT NULL, mean REAL, min REAL, max REAL,
    PRIMARY KEY (station, param, month)
) WITHOUT ROWID;

CREATE TABLE station_monthly (
    station TEXT NOT NULL, month TEXT NOT NULL,
    days INTEGER NOT NULL, valid_days INTEGER NOT NULL, longest_gap INTEGER NOT NULL,
    valid INTEGER NOT NULL,
    PRIMARY KEY (station, month)
) WITHOUT ROWID;
"""


@dataclass
class Values:
    samples: list = field(default_factory=list)
    slots: set = field(default_factory=set)
    flagged: int = 0
    missing: int = 0

    def add(self, slot, value):
        self.samples.append(value)
        self.slots.add(slot)


@dataclass
class Observations:
    raw_n: int = 0
    conflicts: int = 0
    slots: set = field(default_factory=set)
    values: dict = field(default_factory=lambda: defaultdict(Values))


class DailyRow(NamedTuple):
    station: str
    param: str
    day: str
    n: int
    slots: int
    station_slots: int
    raw_n: int
    flagged: int
    missing: int
    conflicts: int
    valid: int
    mean: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    sin_sum: Optional[float]
    cos_sum: Optional[float]


def source_member(station_id, year):
    station = STATIONS[station_id]
    return f"{ARCHIVE_ROOT}/data/{station.folder}/{station_id}_{year}.csv"


def archive_text(archive, member):
    return io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")


def read_year(archive, member, station, year):
    days = defaultdict(Observations)
    timestamps = {}
    conflicts = 0
    raw_rows = 0
    with archive_text(archive, member) as file:
        reader = csv.DictReader(file)
        headers = reader.fieldnames or []
        if (
            len(set(headers)) != len(headers)
            or not {"datetime", "qc_flag"} <= set(headers)
            or set(headers) - {"datetime", "qc_flag", *station.params}
        ):
            raise ValueError(f"{member}: некорректные заголовки столбцов: {headers}")
        for row in reader:
            location = f"{member}:{reader.line_num}"
            if None in row or None in row.values():
                raise ValueError(f"{location}: число колонок расходится с заголовком")
            timestamp = row["datetime"]
            parsed = datetime.fromisoformat(timestamp)
            if parsed.year != year or parsed.isoformat(" ", timespec="minutes") != timestamp:
                raise ValueError(f"{location}: некорректный срок {timestamp!r}")
            day = days[timestamp[:10]]
            day.raw_n += 1
            raw_rows += 1

            flags = row["qc_flag"].split(";") if row["qc_flag"] else []
            conflict = "dup_conflict" in flags
            if timestamp in timestamps and not (conflict and timestamps[timestamp]):
                raise ValueError(f"{location}: повтор срока без dup_conflict")
            timestamps[timestamp] = conflict
            if conflict:
                day.conflicts += 1
                conflicts += 1
                continue

            flagged_params = set()
            for flag in flags:
                param, _, reason = flag.partition(":")
                if param not in station.params or reason not in {"range", "fault"}:
                    raise ValueError(f"{location}: неизвестный флаг {flag!r}")
                flagged_params.add(param)

            minute = parsed.hour * 60 + parsed.minute
            slot = minute // station.slot_minutes
            day.slots.add(slot)
            for param in station.params:
                values = day.values[param]
                if param in flagged_params:
                    values.flagged += 1
                    continue
                value = row.get(param, "").strip()
                if not value:
                    values.missing += 1
                    continue
                sample = float(value)
                lower, upper = LIMITS[param]
                if not lower <= sample <= upper:
                    raise ValueError(f"{location}: {param}={value} за диапазоном без флага")
                values.add(slot, sample)

    metadata = {"member": member, "raw_rows": raw_rows, "conflicts": conflicts}
    return days, metadata


def circular_sums(degrees):
    angles = [math.radians(angle) for angle in degrees]
    return math.fsum(map(math.sin, angles)), math.fsum(map(math.cos, angles))


def circular_angle(sin_sum, cos_sum, n):
    if math.hypot(sin_sum, cos_sum) <= n * 1e-12:
        return None
    angle = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
    return 0.0 if angle == 360 else angle


def daily_record(station_id, param, date, day, minimum_slots):
    values = day.values.get(param) or Values()
    n = len(values.samples)
    slots = len(values.slots)
    valid = int(slots >= minimum_slots)
    mean = minimum = maximum = sin_sum = cos_sum = None
    if param == DIRECTION and n:
        sin_sum, cos_sum = circular_sums(values.samples)
        if valid:
            mean = circular_angle(sin_sum, cos_sum, n)
    elif valid:
        mean = math.fsum(values.samples) / n
        minimum = min(values.samples)
        maximum = max(values.samples)
    return DailyRow(
        station_id, param, date, n, slots, len(day.slots), day.raw_n,
        values.flagged, values.missing, day.conflicts, valid, mean,
        minimum, maximum, sin_sum, cos_sum,
    )


def coverage(validity):
    gap = longest_gap = valid_days = 0
    for valid in validity:
        if valid:
            valid_days += 1
            gap = 0
        else:
            gap += 1
            longest_gap = max(gap, longest_gap)
    days = len(validity)
    valid = int(days - valid_days <= 10 and longest_gap < 5)
    return days, valid_days, longest_gap, valid


def monthly_record(station_id, param, month, daily_rows):
    days, valid_days, longest_gap, valid = coverage([row.valid for row in daily_rows])
    mean = minimum = maximum = None
    if valid:
        values = [row for row in daily_rows if row.mean is not None]
        means = [row.mean for row in values]
        if means and param == DIRECTION:
            mean = circular_angle(*circular_sums(means), len(means))
        elif means:
            mean = math.fsum(means) / len(means)
            minimum = min(row.minimum for row in values)
            maximum = max(row.maximum for row in values)
    return station_id, param, month, days, valid_days, longest_gap, valid, mean, minimum, maximum


def write_year(db, station_id, station, year, observations):
    empty_day = Observations()
    for month_number in range(1, 13):
        month = f"{year:04d}-{month_number:02d}"
        days = calendar.monthrange(year, month_number)[1]
        dates = [f"{month}-{day:02d}" for day in range(1, days + 1)]
        observed = [observations.get(date, empty_day) for date in dates]
        station_validity = [len(day.slots) >= station.minimum_slots for day in observed]
        db.execute("INSERT INTO station_monthly VALUES (?, ?, ?, ?, ?, ?)",
                   (station_id, month, *coverage(station_validity)))
        for param in station.params:
            rows = [
                daily_record(station_id, param, date, day, station.minimum_slots)
                for date, day in zip(dates, observed)
            ]
            db.executemany(
                "INSERT INTO daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            db.execute("INSERT INTO monthly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       monthly_record(station_id, param, month, rows))


def verify_archive_coverage(db, archive, source_metadata):
    query = "SELECT station, month, days, valid_days, valid FROM station_monthly"
    derived = {(s, m): (days, n, valid) for s, m, days, n, valid in db.execute(query)}
    expected = {}
    with archive_text(archive, f"{ARCHIVE_ROOT}/coverage_by_month.csv") as file:
        for row in csv.DictReader(file):
            station = row["station"].lower()
            month = f'{int(row["year"]):04d}-{int(row["month"]):02d}'
            key = (station, month)
            if key in expected or row["month_valid"] not in {"True", "False"}:
                raise ValueError(f"Некорректная строка контрольного покрытия: {row}")
            expected[key] = (
                int(row["days_in_month"]), int(row["valid_days"]),
                int(row["month_valid"] == "True"),
            )
    source_months = {
        (station, f"{year}-{month:02d}")
        for station, years in source_metadata.items()
        for year in years
        for month in range(1, 13)
    }
    if source_months != expected.keys():
        raise ValueError("Годы в контрольном покрытии и файлах архива расходятся")
    for key, reference in expected.items():
        if derived.get(key) != reference:
            raise ValueError(f"Покрытие {key}: в расчёте {derived.get(key)}, в архиве {reference}")
    for key in derived.keys() - expected.keys():
        days, n, valid = derived[key]
        if not days or n or valid:
            raise ValueError(f"Год без исходных данных имеет покрытие: {key}")
    return len(expected)


def verify_kr1_pressure(db):
    station_days, station_valid_days, station_months = db.execute("""
        SELECT sum(days), sum(valid_days), sum(valid)
        FROM station_monthly WHERE station = 'kr1' AND month LIKE '2024-%'
    """).fetchone()
    valid_days, flagged, raw_rows = db.execute("""
        SELECT sum(valid), sum(flagged), sum(raw_n)
        FROM daily WHERE station = 'kr1' AND param = 'pressure_hpa' AND day LIKE '2024-%'
    """).fetchone()
    valid_months, = db.execute("""
        SELECT sum(valid)
        FROM monthly WHERE station = 'kr1' AND param = 'pressure_hpa' AND month LIKE '2024-%'
    """).fetchone()
    april_days, april_gap, april_valid = db.execute("""
        SELECT valid_days, longest_gap, valid
        FROM monthly WHERE station = 'kr1' AND param = 'pressure_hpa' AND month = '2024-04'
    """).fetchone()
    actual = (station_days, station_valid_days, station_months, valid_days,
              flagged, raw_rows, valid_months, april_valid)
    if actual != (366, 363, 12, 334, 1362, 17247, 11, 0):
        raise ValueError(f"Контрольный расчёт давления Кр1 за 2024 расходится с паспортом: {actual}")
    return {
        "raw_rows": raw_rows,
        "station_valid_days": station_valid_days,
        "station_valid_months": station_months,
        "valid_days": valid_days,
        "valid_months": valid_months,
        "flagged": flagged,
        "april": {"days": 30, "valid_days": april_days, "longest_gap": april_gap, "valid": april_valid},
    }


def export_csv(db, path, query):
    cursor = db.execute(query)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([column[0] for column in cursor.description])
        writer.writerows(cursor)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for part in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def build(archive_path, output):
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": archive_path.name,
        "source_sha256": sha256(archive_path),
        "sources": {station_id: {} for station_id in STATIONS},
    }
    with TemporaryDirectory(prefix=".weather-build-", dir=output) as temporary:
        stage = Path(temporary)
        with ZipFile(archive_path) as archive, closing(sqlite3.connect(stage / "weather.sqlite")) as db:
            members = set(archive.namelist())
            db.executescript(SCHEMA)
            with db:
                for station_id, station in STATIONS.items():
                    for year in station.years:
                        member = source_member(station_id, year)
                        observations = {}
                        if member in members:
                            observations, metadata = read_year(archive, member, station, year)
                            summary["sources"][station_id][year] = metadata
                        write_year(db, station_id, station, year, observations)
            summary["official_coverage_rows_verified"] = verify_archive_coverage(
                db, archive, summary["sources"]
            )
            summary["kr1_pressure_2024"] = verify_kr1_pressure(db)
            integrity, = db.execute("PRAGMA integrity_check").fetchone()
            if integrity != "ok":
                raise ValueError(f"Не прошла проверка SQLite: {integrity}")
            summary["rows"] = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("daily", "monthly", "station_monthly")
            }
            export_csv(db, stage / "station_coverage.csv",
                       "SELECT * FROM station_monthly ORDER BY station, month")
            export_csv(db, stage / "parameter_coverage.csv",
                       "SELECT * FROM monthly ORDER BY station, param, month")
        summary["database_sha256"] = sha256(stage / "weather.sqlite")
        (stage / "build.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for name in ("station_coverage.csv", "parameter_coverage.csv", "build.json", "weather.sqlite"):
            (stage / name).replace(output / name)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "meteo_hack_dataset.zip")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    summary = build(args.archive, args.output_dir)
    print(f"Проверено строк покрытия из архива: {summary['official_coverage_rows_verified']}.")
    print(f"Результаты сохранены в {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
