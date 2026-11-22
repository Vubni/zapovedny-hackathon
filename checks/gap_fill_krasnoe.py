"""Compare the fixed external correction on hidden Krasnoe archive temperatures."""

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from analytics import ARCHIVE, ROOT
from build_data import LIMITS, sha256, source_member
from checks.gap_fill_archive import error_metrics, sample_blocks, station_rows
from external_fill import (
    MODELS, ModelData, _metadata, _parse_model, _power_products, apply_estimates, external_reference,
    reference_inputs,
)
from gap_fill import station_estimates, verify_estimates

POINT = (56.46431, 32.97029)
START, END = "2024-04-01", "2024-04-30"
SOURCE_FROM, SOURCE_THROUGH = date(2024, 3, 31), date(2024, 5, 1)
SOURCE_FOLDER = ROOT / "results" / "review" / "external"
RETRIEVAL = SOURCE_FOLDER / "krasnoe-april-2024-retrieval.json"
OUTPUT = ROOT / "results" / "review" / "gap-fill-krasnoe.json"
PARAMETER = "t_air"


def provider_responses():
    providers, files = [], []
    for retrieval in json.loads(RETRIEVAL.read_text(encoding="utf-8")):
        provider = MODELS[retrieval["id"]]
        path = SOURCE_FOLDER / f"{provider.id}-krasnoe-april-2024.json"
        if (sha256(path) != retrieval["sha256"] or tuple(retrieval["requested_point"]) != POINT
                or retrieval["url"] != provider.url(SOURCE_FROM, SOURCE_THROUGH, POINT)):
            raise ValueError(f"Не совпал источник исторической проверки {path.name}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        coordinates, values = _parse_model(provider, raw, SOURCE_FROM, SOURCE_THROUGH, POINT, PARAMETER)
        metadata = _metadata(provider, SOURCE_FROM, SOURCE_THROUGH, False, POINT, PARAMETER)
        metadata.update(coordinates=coordinates, retrieved_at=retrieval["retrieved_utc"], status="available")
        if provider.id == "merra2":
            metadata["model"] = " / ".join(_power_products(raw["header"]))
        files.append(dict(retrieval, filename=path.name, returned_grid=coordinates, model=metadata["model"]))
        providers.append(ModelData(metadata, values))
    if {source.metadata["id"] for source in providers} != set(MODELS):
        raise ValueError("Для проверки нужны оба независимых источника.")
    return providers, files


def hidden_sample(rows, station):
    _, starts = sample_blocks(rows, 30, 1, station)
    truth = {rows[index]["time"]: rows[index]["value"] for index in starts}
    masked = [dict(row, keep=False, value=None) if row["time"] in truth else row for row in rows]
    return masked, truth


def reanalysis_references(rows, target_stamps, providers, timezone_name, subhour):
    report = {"param": PARAMETER, "start": START, "end": END, "rows": reference_inputs(rows, subhour, True)}
    with patch("external_fill._load_sources", return_value=providers):
        report = apply_estimates(report, {"fill": "1", "timezone": timezone_name, "subhour": subhour,
                                          "latitude": POINT[0], "longitude": POINT[1]})
    references, history_refs = {}, {}
    for row in report["rows"]:
        reference = external_reference(row, LIMITS[PARAMETER])
        if reference is None:
            continue
        if row.get("calibration_sample"):
            if row["time"] in target_stamps:
                raise ValueError("Скрытый срок попал в калибровочную выборку")
            history_refs[row["time"]] = reference
        elif row["time"] in target_stamps:
            references[row["time"]] = reference
    return references, history_refs


def compare_values(truth, estimates, adjusted, verifications, matched):
    changed = [stamp for stamp in matched if verifications[stamp]["decision"] == "adjusted"]
    delta_abs_errors = [abs(adjusted[stamp]["value"] - truth[stamp])
                        - abs(estimates[stamp]["value"] - truth[stamp]) for stamp in changed]
    return {
        "corrected_points": len(changed),
        "decision_counts": dict(Counter(verifications[stamp]["decision"] for stamp in matched)),
        "max_adjustment_c": max((abs(verifications[stamp]["delta"]) for stamp in changed), default=0),
        "with_correction": error_metrics([adjusted[stamp]["value"] - truth[stamp] for stamp in matched]),
        "corrected_point_outcomes": {
            "closer": sum(delta < -1e-10 for delta in delta_abs_errors),
            "farther": sum(delta > 1e-10 for delta in delta_abs_errors),
            "equal": sum(abs(delta) <= 1e-10 for delta in delta_abs_errors),
        },
    }


def compare(masked, truth, estimates, providers, timezone_name, subhour):
    from gap_fill import verification_history

    references, past_references = reanalysis_references(masked, truth, providers, timezone_name, subhour)
    history = verification_history(masked, past_references, PARAMETER, timezone=timezone_name)
    matched = sorted(truth.keys() & estimates.keys())
    estimates = {stamp: estimates[stamp] for stamp in matched}
    ungated, original_checks = verify_estimates(estimates, references, PARAMETER, validated=True)
    gated, gated_checks = verify_estimates(estimates, references, PARAMETER, history=history)
    proofs = [verification["calibration"] for verification in gated_checks.values()
              if verification["calibration"] is not None]

    comparable = sorted(estimates.keys() & references.keys())
    if comparable:
        probe = comparable[len(comparable) // 2]
        prefix_rows = [row for row in masked if row["time"] < probe]
        _, prefix_references = reanalysis_references(prefix_rows, set(), providers, timezone_name, subhour)
        prefix = verification_history(prefix_rows, prefix_references, PARAMETER, timezone=timezone_name)
        prefix_result, prefix_checks = verify_estimates(
            {probe: estimates[probe]}, {probe: references[probe]}, PARAMETER, history=prefix)
        if prefix_result[probe] != gated[probe] or prefix_checks[probe] != gated_checks[probe]:
            raise ValueError(f"Проверка до {probe} зависит от наблюдений после целевого срока")

    return {
        "timezone_hypothesis": timezone_name, "subhour_mode": subhour,
        "hidden_points": len(truth), "local_estimates": len(matched),
        "available_external_references": sum(item["usable"] for item in references.values()),
        "local": error_metrics([estimates[stamp]["value"] - truth[stamp] for stamp in matched]),
        "ungated": compare_values(truth, estimates, ungated, original_checks, matched),
        "prior_validation": compare_values(truth, estimates, gated, gated_checks, matched),
        "validation_reference_count": len(past_references),
        "historical_comparable_errors": len(history),
        "candidates_with_minimum_previous_pairs": sum(
            proof["sample_count"] >= proof["minimum_sample_count"] for proof in proofs
        ),
        "prefix_check_hidden_timestamp": probe if comparable else None,
        "external_reference_only": error_metrics([item["value"] - truth[stamp]
                                                   for stamp, item in references.items() if item["usable"]]),
    }


def run():
    providers, files = provider_responses()
    results = []
    with ZipFile(ARCHIVE) as archive:
        for station in ("kr1", "kr2"):
            member = source_member(station, 2024)
            rows = [row for row in station_rows(archive, member) if START <= row["time"][:10] <= END]
            masked, truth = hidden_sample(rows, station)
            estimates, status = station_estimates(masked, PARAMETER)
            results.append({"station": station, "source_member": member,
                            "model_step_minutes": status["step_minutes"],
                            "first_hidden": min(truth), "last_hidden": max(truth),
                            "scenarios": [compare(masked, truth, estimates, providers, zone, subhour)
                                          for zone in ("UTC", "Europe/Moscow")
                                          for subhour in ("skip", "interpolate")]})
    return {"parameter": PARAMETER, "from": START, "through": END, "archive_sha256": sha256(ARCHIVE),
            "input_archive_timezone_confirmed": False, "sources": files, "stations": results}


def main():
    parser = argparse.ArgumentParser(description="Проверка мягкой внешней поправки на скрытых сроках в Красном")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    destination = parser.parse_args().output
    destination.write_text(json.dumps(run(), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
