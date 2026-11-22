"""Fill options and retrospective gap estimates from a station's own observations."""

from bisect import bisect_left
from collections import Counter
from datetime import date, datetime
from math import sqrt
from statistics import fmean, median

from analytics import PARAMS
from build_data import DIRECTION, LIMITS
from external_fill import _to_utc, time_zone


_MODES = {(False, False): "none", (True, False): "external", (False, True): "model", (True, True): "both"}
_LEGACY = {name: methods for methods, name in _MODES.items()} | {"0": (False, False), "1": (True, False)}
_CALIBRATION_WINDOW = 192
# (maximum source spread, maximum deviation from the station, absolute correction limit).
_VERIFICATION_LIMITS = {"t_air": (2.0, 2.0, 0.5), "rh": (8.0, 8.0, 2.0), "pressure_hpa": (2.5, 2.5, 0.625)}
_EXTERNAL_WEIGHT = 0.25
_MIN_VERIFICATION_PAIRS = 20
_VERIFICATION_WINDOW = 96
VERIFICATION_LABELS = {
    "adjusted": "Применена ограниченная поправка",
    "already_equal": "Локальная и внешняя оценки совпадают",
    "no_local_estimate": "Нет оценки по данным станции",
    "external_unavailable": "Нет внешних данных на сопоставимый срок",
    "fewer_than_two_sources": "Доступно менее двух источников",
    "providers_disagree": "Внешние источники расходятся",
    "local_departure": "Расхождение с оценкой станции велико",
    "unsupported_parameter": "Внешняя поправка для показателя недоступна",
    "outside_parameter_range": "Справочная оценка вне рабочих границ",
    "insufficient_local_validation": "Для поправки нужна оценка ошибки модели станции",
    "within_local_precision": "Поправка меньше точности расчёта",
    "unvalidated_correction": "Поправка не прошла проверку на предыдущих наблюдениях станции",
}


def fill_settings(query):
    """Normalize explicit switches and the modes stored by older clients."""
    switches = ("fill_external", "fill_model")
    if any(key in query for key in switches):
        selected = []
        for key in switches:
            value = query.get(key, False)
            if type(value) not in (bool, str) or value not in (True, False, "1", "0"):
                raise ValueError("Укажите 0 или 1 для каждого способа заполнения.")
            selected.append(value is True or value == "1")
        external, model = selected
    else:
        mode = query.get("fill") or "none"
        if not isinstance(mode, str) or mode not in _LEGACY:
            raise ValueError("Выберите поддерживаемый способ заполнения.")
        external, model = _LEGACY[mode]
    return {"fill": _MODES[external, model], "fill_external": external, "fill_model": model}


def estimate_bounds(parameter, ranges):
    lower, upper = LIMITS[parameter]
    if ranges and parameter in ranges:
        current_lower, current_upper = ranges[parameter]
        return max(lower, current_lower), min(upper, current_upper)
    return lower, upper


def _minutes(stamp):
    dt = datetime.fromisoformat(stamp)
    return dt.toordinal() * 1440 + dt.hour * 60 + dt.minute


def _stamp_text(stamp):
    day, minute = divmod(stamp, 1440)
    return f"{date.fromordinal(day).isoformat()} {minute // 60:02}:{minute % 60:02}"


def _cadence(stamps):
    gaps = Counter(right - left for left, right in zip(stamps, stamps[1:]) if right - left <= 120)
    return min(gaps, key=lambda gap: (-gaps[gap], gap)) if gaps else None


def _interpolate(stamp, left, right):
    return left[1] + (right[1] - left[1]) * (stamp - left[0]) / (right[0] - left[0])


def _residuals(observed, step):
    return [(right[0], target[1] - _interpolate(target[0], left, right))
            for left, target, right in zip(observed, observed[1:], observed[2:])
            if target[0] - left[0] == step and right[0] - target[0] == step]


def _calibration(residuals):
    split = len(residuals) * 4 // 5
    training, holdout = residuals[:split], residuals[split:]
    summary = {"training_n": len(training), "test_n": len(holdout), "mae": None}
    if len(training) < 10 or len(holdout) < 3:
        return 0, None, summary
    bias = median(error for _, error in training)
    summary["mae"] = round(fmean(abs(error - bias) for _, error in holdout), 4)
    return bias, training[-1][0], summary


def station_estimates(rows, parameter, *, excluded=(), ranges=None, period=None, hidden_terms=None):
    """Interpolate with original neighbors. Fit any bias using history before the gap."""
    status = {"status": "unavailable", "training_n": 0, "test_n": 0, "mae": None,
              "step_minutes": None, "estimated_count": 0}
    if parameter == DIRECTION:
        return {}, dict(status, reason="unsupported_direction")

    points = [(_minutes(row["time"]), row) for row in rows if not row.get("synthetic")]
    occurrences = Counter(stamp for stamp, _ in points)
    originals = sorted(occurrences)
    step = _cadence(originals)
    status["step_minutes"] = step
    if step is None:
        return {}, dict(status, reason="unknown_cadence")
    usable = [(stamp, row) for stamp, row in points if occurrences[stamp] == 1 and not row.get("conflict")]
    observed = sorted((stamp, row["value"]) for stamp, row in usable if row["keep"] and row["value"] is not None)
    candidates = {stamp for stamp, row in usable if not row["keep"] or row["value"] is None}
    candidates.update(left + step for left, right in zip(originals, originals[1:]) if right - left == 2 * step)
    if hidden_terms is not None:
        candidates = {_minutes(term) for term in hidden_terms}
    if period:
        start, end = (day.toordinal() for day in period)
        candidates = {stamp for stamp in candidates if start <= stamp // 1440 <= end}
    candidates = candidates - {_minutes(time) for time in excluded}
    if not candidates:
        return {}, dict(status, reason="no_remaining_gaps" if excluded else "no_supported_gaps")

    residuals = _residuals(observed, step)
    holdout = residuals[len(residuals) * 4 // 5:]
    if holdout:
        baseline_mae = round(fmean(abs(error) for _, error in holdout), 4)
        status.update(test_n=len(holdout), mae=baseline_mae, baseline_mae=baseline_mae,
                      validation_scope="uncalibrated_observed_series")
    residual_stamps = [stamp for stamp, _ in residuals]
    stamps = [stamp for stamp, _ in observed]
    lower, upper = estimate_bounds(parameter, ranges)
    unit = PARAMS[parameter][1]
    estimates = {}
    for stamp in sorted(candidates):
        index = bisect_left(stamps, stamp)
        if not 0 < index < len(stamps):
            continue
        right_index = index + (hidden_terms is not None)
        if right_index >= len(stamps) or (stamps[index] == stamp) != (hidden_terms is not None):
            continue
        left, right = observed[index - 1], observed[right_index]
        if right[0] - left[0] > 2 * step:
            continue
        before = bisect_left(residual_stamps, stamp)
        bias, training_end, fit = _calibration(residuals[max(0, before - _CALIBRATION_WINDOW):before])
        value = round(_interpolate(stamp, left, right) + bias, 4)
        if not lower <= value <= upper:
            continue
        confidence = {"kind": "uncalibrated_interpolation", "unit": unit}
        if training_end is not None:
            confidence = {"kind": "validation_mae", "mae": fit["mae"], "test_n": fit["test_n"], "unit": unit}
            status["training_n"] = max(status["training_n"], fit["training_n"])
        provenance = {
            "kind": "station_model", "method": "station_bracket_interpolation", "unit": unit,
            "anchors": [{"time": _stamp_text(term), "value": reading} for term, reading in (left, right)],
            "bias": round(bias, 4), "training_n": fit["training_n"] if training_end is not None else 0,
            "training_end": _stamp_text(training_end) if training_end is not None else None,
            "validation": confidence,
        }
        estimates[_stamp_text(stamp)] = {
            "value": value, "local_value": value, "method": "model",
            "source": "Модель станции · интерполяция по исходным наблюдениям",
            "confidence": confidence, "provenance": provenance, "enters_working_graphs": True,
        }
    if not estimates:
        return estimates, dict(status, reason="no_supported_gaps")
    return estimates, dict(status, status="estimated", estimated_count=len(estimates))


def _target_utc(reference):
    provenance = reference["provenance"]
    stamp = provenance.get("time_utc") or provenance.get("start_utc")
    return datetime.fromisoformat(stamp) if stamp else None


def _verification_at(reference, history, known_times):
    target = _target_utc(reference)
    count = bisect_left(known_times, target) if target else 0
    recent = history[max(0, count - _VERIFICATION_WINDOW):count]
    proof = {"sample_count": len(recent), "minimum_sample_count": _MIN_VERIFICATION_PAIRS,
             "latest_known_utc": recent[-1][0].isoformat() if recent else None, "accepted": False}
    if recent:
        mae_local = fmean(initial for _, initial, _ in recent)
        mae_corrected = fmean(corrected for _, _, corrected in recent)
        rmse_local = sqrt(fmean(initial ** 2 for _, initial, _ in recent))
        rmse_corrected = sqrt(fmean(corrected ** 2 for _, _, corrected in recent))
        proof.update(local_mae=round(mae_local, 4), adjusted_mae=round(mae_corrected, 4),
                     local_rmse=round(rmse_local, 4), adjusted_rmse=round(rmse_corrected, 4),
                     accepted=(len(recent) >= _MIN_VERIFICATION_PAIRS and mae_corrected < mae_local
                               and rmse_corrected <= rmse_local))
    return proof


def verify_estimates(estimates, references, parameter, *, history=(), validated=False):
    """Keep the station estimate primary and bound any independent-model correction."""
    adjusted, verifications = {}, {}
    policy = _VERIFICATION_LIMITS.get(parameter)
    known_times = [item[0] for item in history]
    for stamp in estimates.keys() | references.keys():
        estimate, reference = estimates.get(stamp), references.get(stamp)
        local = estimate["value"] if estimate is not None else None
        external = reference.get("value") if reference else None
        sources = reference["provenance"]["sources"] if reference else []
        spread = max(item["value"] for item in sources) - min(item["value"] for item in sources) if sources else None
        limits = (dict(source_spread=policy[0], local_departure=policy[1], max_delta=policy[2]) if policy else None)
        calibration = None
        decision, delta = "external_unavailable", 0.0
        if local is None:
            decision = "no_local_estimate"
        elif policy is None:
            decision = "unsupported_parameter"
        elif reference and len({source.get("id") for source in sources}) < 2:
            decision = "fewer_than_two_sources"
        elif external is not None:
            distance = external - local
            mae = (estimate.get("confidence") or {}).get("mae")
            if not reference.get("usable", True):
                decision = "outside_parameter_range"
            elif spread > policy[0]:
                decision = "providers_disagree"
            elif abs(distance) > policy[1]:
                decision = "local_departure"
            elif round(distance, 4) == 0:
                decision = "already_equal"
            elif mae is None:
                decision = "insufficient_local_validation"
            else:
                limits["local_validation_mae"] = mae
                cap = min(policy[2], mae)
                delta = round(max(-cap, min(cap, _EXTERNAL_WEIGHT * distance)), 4)
                decision = "adjusted" if delta else "within_local_precision"
                if delta and not validated:
                    calibration = _verification_at(reference, history, known_times)
                    if not calibration["accepted"]:
                        delta = 0.0
                        decision = "unvalidated_correction"
        verification = {"decision": decision, "delta": delta, "local_value": local, "external_value": external,
                        "weight": _EXTERNAL_WEIGHT if delta else 0.0, "source_spread": spread, "limits": limits,
                        "calibration": calibration}
        verifications[stamp] = verification
        if estimate is not None:
            result = dict(estimate, local_value=local, value=round(local + delta, 4),
                          provenance=dict(estimate["provenance"], verification=verification))
            if delta:
                result.update(method="model_adjusted", source=f"Модель станции · сверка с {reference['source']}")
            adjusted[stamp] = result
    return adjusted, verifications


def verification_history(rows, observed_references, parameter, ranges=None, timezone="UTC"):
    """Validate the fixed correction on real observations hidden from each local prediction."""
    if parameter not in _VERIFICATION_LIMITS or not observed_references:
        return []
    trials, _ = station_estimates(rows, parameter, ranges=ranges, hidden_terms=observed_references)
    corrected, _ = verify_estimates(trials, observed_references, parameter, validated=True)
    measured = {row["time"]: row["value"] for row in rows if row["keep"] and not row.get("conflict")}
    zone = time_zone(timezone)
    history = []
    for stamp, estimate in corrected.items():
        if estimate["method"] != "model_adjusted" or stamp not in measured:
            continue
        right = datetime.fromisoformat(estimate["provenance"]["anchors"][1]["time"])
        right_utc = _to_utc(right, zone)
        available = observed_references[stamp]["provenance"].get("known_after_utc")
        if right_utc is None or not available:
            continue
        available_at = max(right_utc, datetime.fromisoformat(available))
        history.append((available_at, abs(estimate["local_value"] - measured[stamp]),
                        abs(estimate["value"] - measured[stamp])))
    return sorted(history)
