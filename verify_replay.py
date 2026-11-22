"""Проверить суточные выгрузки и отказ при подмене строк сверкой с ZIP."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import analytics


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "data" / "meteo_hack_dataset.zip"
REPORT = ROOT / "results" / "replay-source-checks.json"


def exported_day(station, param, day):
    return analytics.raw_day({"station": station, "param": param, "day": day})


def verify():
    browser_export = ROOT / "results" / "examples" / "kr1-pressure-april-01-valid.json"
    examples = {
        "http_export_kr1_pressure": json.loads(browser_export.read_text(encoding="utf-8")),
        "incomplete_kr1_pressure": exported_day("kr1", "pressure_hpa", "2024-04-29"),
        "ame60_wind_with_conflicts": exported_day("ame60", "wind_dir", "1993-04-02"),
        "empty_values_in_existing_parameter_column": exported_day("ame60", "wind_speed", "2011-07-30"),
        "archived_csv_without_parameter_column": exported_day("ame60", "wind_dir", "1998-07-01"),
        "empty_day_in_existing_csv": exported_day("kr2", "t_air", "2023-12-31"),
        "absent_year_csv": exported_day("ame60", "t_air", "2000-01-01"),
    }
    pressure = examples["http_export_kr1_pressure"]
    conflicts = examples["ame60_wind_with_conflicts"]
    flagged = next(index for index, row in enumerate(pressure["rows"]) if "pressure_hpa:range" in row["flags"])
    conflict = next(index for index, row in enumerate(conflicts["rows"]) if "dup_conflict" in row["flags"])

    with TemporaryDirectory() as directory:
        fixture = Path(directory) / "day.json"

        def run(data, use_archive):
            fixture.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "replay_day.py"), str(fixture)]
            if use_archive:
                command.extend(["--archive", str(ARCHIVE)])
            return subprocess.run(command, capture_output=True, text=True)

        genuine = {}
        for name, data in examples.items():
            archive_result = run(data, use_archive=True)
            assert archive_result.returncode == 0, (name, archive_result.stderr)
            assert "ZIP" in archive_result.stdout, (name, archive_result.stdout)
            genuine[name] = {
                "day": data["day"], "station": data["station"], "param": data["param"],
                "source": data["source"], "rows": len(data["rows"]),
                "null_values": sum(row["value"] is None for row in data["rows"]),
                "archive_exit": archive_result.returncode,
            }

        tampering = {}

        def changed(name, data, mutate, diagnostic):
            fixture_data = deepcopy(data)
            mutate(fixture_data)
            arithmetic = run(fixture_data, use_archive=False)
            assert arithmetic.returncode == 0 and "Режим проверки по строкам JSON" in arithmetic.stdout, (
                name, arithmetic.stderr,
            )
            archive_result = run(fixture_data, use_archive=True)
            assert archive_result.returncode == 1 and diagnostic in archive_result.stderr, (
                name, archive_result.stderr,
            )
            tampering[name] = {
                "arithmetic_exit": arithmetic.returncode, "archive_exit": archive_result.returncode,
                "archive_error": archive_result.stderr.strip(),
            }

        changed("omitted_conflict_row", conflicts, lambda data: data["rows"].pop(conflict), "Число строк")
        changed("added_conflict_row", conflicts, lambda data: data["rows"].insert(conflict, data["rows"][conflict]),
                "Число строк")
        changed("reordered_rows", pressure, lambda data: data["rows"].reverse(), "Поле line")
        changed("changed_excluded_value_to_null", pressure,
                lambda data: data["rows"][flagged].update(value=None), "Поле value")
        changed("changed_value_below_summary_tolerance", pressure,
                lambda data: data["rows"][0].update(value=pressure["rows"][0]["value"] + 1e-10), "Поле value")
        changed("changed_other_parameter_flag", pressure,
                lambda data: data["rows"][0].update(flags="wind_dir:range"), "Поле flags")
        changed("changed_physical_csv_line", pressure,
                lambda data: data["rows"][0].update(line=pressure["rows"][0]["line"] - 1), "Поле line")
        changed("changed_timestamp_within_hour", pressure,
                lambda data: data["rows"][0].update(time="2024-04-01 00:03"), "Поле time")
        changed("substituted_existing_csv_member", pressure,
                lambda data: data.update(source=data["source"].replace("kr1_2024", "kr2_2024")), "Поле source")
        changed("removed_source_from_empty_existing_day", examples["empty_day_in_existing_csv"],
                lambda data: data.update(source=None), "Поле source")
        changed("invented_source_for_absent_year", examples["absent_year_csv"],
                lambda data: data.update(source="meteo_clgz_hackathon/data/ame60/ame60_2000.csv"), "отсутствует CSV")

        invalid = deepcopy(pressure)
        invalid["rows"][0]["value"] = True
        for use_archive in (False, True):
            rejected = run(invalid, use_archive)
            assert rejected.returncode == 1 and "конечное число" in rejected.stderr, rejected.stderr

    evidence = {
        "command": "python3 -B verify_replay.py",
        "archive": str(ARCHIVE.relative_to(ROOT)),
        "genuine_exports": genuine,
        "deliberate_tampering_detected_by_archive": tampering,
        "deliberate_boolean_as_number": {"arithmetic_exit": 1, "archive_exit": 1},
    }
    REPORT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Сверены {len(genuine)} исходных выгрузок и {len(tampering)} случаев намеренной подмены. "
          f"Логический тип вместо числа отклонён в обоих режимах. Отчёт {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    verify()
