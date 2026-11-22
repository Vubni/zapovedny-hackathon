"""Reproduce the retrospective benchmark with python3 -m checks.gap_fill_archive."""

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from random import Random
from statistics import fmean
from zipfile import ZipFile

from analytics import ARCHIVE, ROOT
from build_data import LIMITS, archive_text, sha256, source_member
from gap_fill import station_estimates

CASE_FILES = {"ame60": (2021, (15,)), "kr1": (2025, (30, 60)), "kr2": (2025, (30, 60))}
LENGTHS = (1, 2, 4)
CAP = 200
SEED = 20261122
OUTPUT = ROOT / "results" / "review" / "gap-fill-archive.json"


def station_rows(archive, member):
    grouped = defaultdict(list)
    low, high = LIMITS["t_air"]
    with archive_text(archive, member) as stream:
        for row in csv.DictReader(stream):
            flags = row["qc_flag"].split(";")
            conflict = "dup_conflict" in flags
            temperature = None
            if row["t_air"].strip() and not conflict and not any(flag.startswith("t_air:") for flag in flags):
                value = float(row["t_air"])
                if low <= value <= high:
                    temperature = value
            grouped[row["datetime"]].append((conflict, temperature))

    rows = []
    for stamp, records in sorted(grouped.items()):
        conflict = len(records) != 1 or records[0][0]
        temperature = None if conflict else records[0][1]
        rows.append({"time": stamp, "value": temperature, "keep": temperature is not None,
                     "conflict": conflict})
    return rows


def sample_blocks(rows, step, size, station):
    times = [datetime.fromisoformat(row["time"]) for row in rows]
    interval = timedelta(minutes=step)
    regular = [right - left == interval and a["keep"] and b["keep"]
               for left, right, a, b in zip(times, times[1:], rows, rows[1:])]
    candidates = [start for start in range(1, len(rows) - size)
                  if all(regular[start-1:start+size])]
    shuffled = candidates.copy()
    Random(f"{SEED}-{station}-{step}-{size}").shuffle(shuffled)
    reserved, selected = set(), []
    for start in shuffled:
        block = range(start - 1, start + size + 1)
        if reserved.isdisjoint(block):
            reserved.update(block)
            selected.append(start)
            if len(selected) == CAP:
                break
    return len(candidates), sorted(selected)


def error_metrics(errors):
    if not errors:
        return None
    return {"mae_c": fmean(abs(error) for error in errors),
            "rmse_c": math.sqrt(fmean(error ** 2 for error in errors))}


def benchmark(rows, starts, size, representation):
    truth = {}
    for start in starts:
        left = rows[start - 1]["value"]
        right = rows[start + size]["value"]
        for offset in range(size):
            row = rows[start + offset]
            linear = left + (right - left) * (offset + 1) / (size + 1)
            truth[row["time"]] = (row["value"], linear, left)

    masked = []
    for row in rows:
        if row["time"] not in truth:
            masked.append(row)
        elif representation == "blank":
            masked.append(dict(row, keep=False, value=None))
    estimates, status = station_estimates(masked, "t_air")
    matched = sorted(truth.keys() & estimates.keys())
    calibration_count = 0
    for stamp in matched:
        provenance = estimates[stamp]["provenance"]
        left, right = (anchor["time"] for anchor in provenance["anchors"])
        training_end = provenance["training_end"]
        if not left < stamp < right or left in truth or right in truth or (training_end and training_end >= stamp):
            raise ValueError(f"Скрытый срок или поздняя калибровка попали в восстановление {stamp}")
        calibration_count += bool(training_end)
    covered_blocks = sum(
        all(rows[start + offset]["time"] in estimates for offset in range(size)) for start in starts
    )
    return {
        "model_step_minutes": status["step_minutes"],
        "hidden_points": len(truth),
        "estimated_points": len(matched),
        "fully_restored_blocks": covered_blocks,
        "points_with_past_calibration": calibration_count,
        "paired": {
            "model": error_metrics([estimates[stamp]["value"] - truth[stamp][0] for stamp in matched]),
            "linear": error_metrics([truth[stamp][1] - truth[stamp][0] for stamp in matched]),
            "last_value": error_metrics([truth[stamp][2] - truth[stamp][0] for stamp in matched]),
        },
        "all_hidden": {
            "linear": error_metrics([linear - actual for actual, linear, _ in truth.values()]),
            "last_value": error_metrics([left - actual for actual, _, left in truth.values()]),
        },
    }


def run():
    cases = []
    with ZipFile(ARCHIVE) as archive:
        for station, (year, steps) in CASE_FILES.items():
            member = source_member(station, year)
            rows = station_rows(archive, member)
            for step in steps:
                for size in LENGTHS:
                    eligible, starts = sample_blocks(rows, step, size, station)
                    cases.append({
                        "station": station, "source_member": member, "year": year,
                        "step_minutes": step, "consecutive_hidden_terms": size,
                        "anchor_span_minutes": (size + 1) * step,
                        "eligible_blocks": eligible, "tested_blocks": len(starts),
                        "first_hidden": rows[starts[0]]["time"] if starts else None,
                        "last_hidden": rows[starts[-1] + size - 1]["time"] if starts else None,
                        "blank_rows": benchmark(rows, starts, size, "blank"),
                        "removed_rows": benchmark(rows, starts, size, "removed"),
                    })
    return {"parameter": "t_air", "source_sha256": sha256(ARCHIVE), "seed": SEED,
            "max_nonoverlapping_blocks_per_case": CAP,
            "hidden_source": "observed_terms_without_original_temperature_quality_flags",
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description="Проверка восстановления искусственно скрытых архивных сроков")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    path = parser.parse_args().output
    report = run()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for case in report["cases"]:
        model = case["blank_rows"]
        print(f"{case['station']} {case['step_minutes']} мин × {case['consecutive_hidden_terms']} скрытых сроков"
              f" · {model['estimated_points']} из {model['hidden_points']}")


if __name__ == "__main__":
    main()
