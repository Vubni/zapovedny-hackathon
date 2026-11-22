"""Fill options and retrospective gap estimates from a station's own observations."""

from bisect import bisect_left
from collections import Counter
from datetime import date, datetime
from statistics import fmean, median

from analytics import PARAMS
from build_data import DIRECTION, LIMITS


_MODES = {(False, False): "none", (True, False): "external", (False, True): "model", (True, True): "both"}
_LEGACY = {name: methods for methods, name in _MODES.items()} | {"0": (False, False), "1": (True, False)}
_CALIBRATION_WINDOW = 192


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
    summary.update(mae=round(fmean(abs(error - bias) for _, error in holdout), 4),
                   baseline_mae=round(fmean(abs(error) for _, error in holdout), 4), bias=round(bias, 4))
    return bias, training[-1][0], summary


def station_estimates(rows, parameter, *, excluded=(), ranges=None):
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
        if not 0 < index < len(stamps) or stamps[index] == stamp or stamps[index] - stamps[index - 1] > 2 * step:
            continue
        left, right = observed[index - 1:index + 1]
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
            "value": value, "method": "model", "source": "Модель станции · интерполяция по исходным наблюдениям",
            "confidence": confidence, "provenance": provenance, "enters_working_graphs": True,
        }
    if not estimates:
        return estimates, dict(status, reason="no_supported_gaps")
    return estimates, dict(status, status="estimated", estimated_count=len(estimates))
