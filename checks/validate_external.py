"""Сравнить внешние оценки с удержанными замерами апреля 2024 года."""

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from statistics import fmean

import external_fill
from analytics import ARCHIVE
from review_filter import review


START, END = "2024-04-01", "2024-04-30"
HOUR = timedelta(hours=1)
MAX_GAP = timedelta(minutes=45)
RAPID_CHANGE_C = 3
STATIONS = ("kr1", "kr2")
FILTERS = "conflicts,hampel,spike,stuck"
MODELS = (*external_fill.MODELS, "mean")
RESULT = Path(__file__).resolve().parents[1] / "results/review/external-validation.json"


@dataclass(frozen=True)
class Reference:
    value: float
    change: float
    lines: tuple[int, ...]


def date_hours():
    start = datetime.fromisoformat(START)
    end = datetime.fromisoformat(END) + timedelta(days=1)
    return [start + i * HOUR for i in range((end - start) // HOUR)]


def reference_at(begin, times, rows):
    """Получить среднее за полный час между пригодными исходными сроками."""
    end = begin + HOUR
    first = bisect_right(times, begin) - 1
    last = bisect_left(times, end)
    if first < 0 or last == len(times):
        return None

    values = []
    integral = 0.0
    for index in range(first, last):
        left, right = times[index:index + 2]
        a, b = rows[index:index + 2]
        gap = right - left
        if not timedelta(0) < gap <= MAX_GAP or not all(
            row["keep"] and row["official_keep"] for row in (a, b)
        ):
            return None
        lower, upper = max(left, begin), min(right, end)
        slope = b["value"] - a["value"]
        bound_a = a["value"] + slope * ((lower - left) / gap)
        bound_b = a["value"] + slope * ((upper - left) / gap)
        values.extend((bound_a, bound_b))
        integral += (bound_a + bound_b) / 2 * ((upper - lower) / HOUR)

    return Reference(integral, values[-1] - values[0], tuple(row["line"] for row in rows[first:last + 1]))


def references(station, hours):
    # Соседние даты нужны для часовых интервалов на границах месяца.
    response = review({"station": station, "param": "t_air", "start": "2024-03-31",
                       "end": "2024-05-01", "filters": FILTERS})
    rows = response["rows"]
    times = [datetime.fromisoformat(row["time"]) for row in rows]
    return {hour: ref for hour in hours if (ref := reference_at(hour, times, rows)) is not None}


def predictions(zone):
    # В оператор оценки не передаются ни исходные замеры, ни их производные.
    query = {"fill": "1", "timezone": zone}
    result = external_fill.apply_estimates({"start": START, "end": END, "param": "t_air", "rows": []}, query,
                                           allow_snapshot=True)
    if any(source["mode"] != "snapshot" for source in result["external"]["sources"]):
        raise RuntimeError("Для проверки требуются сохранённые ответы источников")
    predicted = {}
    for row in result["rows"]:
        if row["estimate_status"] != "estimated":
            continue
        provenance = row["provenance"]
        sources = {source["id"]: source["value"] for source in provenance["sources"]}
        if set(sources) != set(external_fill.MODELS):
            raise RuntimeError("Состав источников в восстановленном часе изменился")
        predicted[datetime.fromisoformat(row["time"])] = (provenance["start_utc"], dict(sources, mean=row["estimate"]))
    return predicted


def metrics(observed, predicted, hours):
    result = {"n": len(hours)}
    for model in MODELS:
        errors = [predicted[hour][1][model] - observed[hour].value for hour in hours]
        result[model] = {
            "mae_c": round(fmean(map(abs, errors)), 2) if errors else None,
            "bias_c": round(fmean(errors), 2) if errors else None,
        }
    return result


def example(hour, observed, by_zone):
    ref = observed[hour]
    return {
        "archive_hour": hour.isoformat(" ", timespec="minutes"),
        "observed_hour_mean_c": round(ref.value, 2),
        "observed_end_minus_start_c": round(ref.change, 2),
        "observation_csv_lines": list(ref.lines),
        "predictions": {
            zone: {
                "start_utc": predicted[hour][0],
                **{model: round(value, 2) for model, value in predicted[hour][1].items()},
            }
            for zone, predicted in by_zone.items()
        },
    }


def validate():
    hours = date_hours()
    by_zone = {zone: predictions(zone) for zone in external_fill.ZONES}
    available = set.intersection(*(set(data) for data in by_zone.values()))
    if not available:
        raise RuntimeError("Нет часов с обеими моделями при обеих трактовках времени")
    observed = {station: references(station, hours) for station in STATIONS}
    common = sorted(available.intersection(*(set(rows) for rows in observed.values())))
    if not common:
        raise RuntimeError("Нет контрольных часов с наблюдениями обеих станций")

    stations = {}
    for station, refs in observed.items():
        matched = sorted(available.intersection(refs))
        rapid = [hour for hour in matched if abs(refs[hour].change) >= RAPID_CHANGE_C]
        other = sorted(set(matched) - set(rapid))
        stations[station] = {
            "reference_hours": len(refs),
            "metrics": {
                zone: {
                    "station_hours": metrics(refs, predicted, matched),
                    "common_station_hours": metrics(refs, predicted, common),
                    "rapid_change_hours": metrics(refs, predicted, rapid),
                    "other_hours": metrics(refs, predicted, other),
                }
                for zone, predicted in by_zone.items()
            },
            "examples": {
                "fastest_observed_change": example(max(matched, key=lambda hour: abs(refs[hour].change)), refs, by_zone),
                "largest_mean_error_by_timezone": {
                    zone: example(max(matched, key=lambda hour: abs(predicted[hour][1]["mean"] - refs[hour].value)),
                                  refs, {zone: predicted})
                    for zone, predicted in by_zone.items()
                },
            },
        }

    return {
        "parameter": "t_air",
        "unit": "°C",
        "archive_period": {"start": START, "end": END},
        "run": "python3 -m checks.validate_external",
        "input_sha256": {
            Path(ARCHIVE).name: sha256(Path(ARCHIVE).read_bytes()).hexdigest(),
            **{model.snapshot: sha256((external_fill.FIXTURES / model.snapshot).read_bytes()).hexdigest()
               for model in external_fill.MODELS.values()},
        },
        "sample_hours": {
            "calendar": len(hours),
            "both_models_by_timezone": {zone: len(predicted) for zone, predicted in by_zone.items()},
            "both_models_both_timezones": len(available),
            "both_stations_both_models_both_timezones": len(common),
        },
        "method": {
            "reference_filters": FILTERS.split(","),
            "reference": "Строки проходят правила кейса и указанные фильтры. Среднее за [H:00, H+1:00) рассчитано интегрированием линейной связи соседних измерений. Требуется полное покрытие интервала. Удалённые сроки, повторы и длинные разрывы не соединяются.",
            "max_observation_gap_minutes": MAX_GAP // timedelta(minutes=1),
            "prediction": "На пустом месяце вызван external_fill.apply_estimates. Отдельные значения взяты из происхождения оценки, среднее взято из результата оператора с производственным округлением. Замеры используются только для сравнения, веса и смещение не подгонялись.",
            "comparison": "Для обоих источников и обеих трактовок использованы одни и те же архивные интервалы. common_station_hours дополнительно ограничен часами с эталоном обеих станций.",
            "bias": "Оценка минус измеренное среднее за час. Положительное смещение соответствует завышению температуры.",
            "rapid_change": "Модуль изменения между границами часа не меньше заданного порога. Это признак быстрого изменения температуры без диагностики погодных фронтов.",
            "rapid_change_threshold_c": RAPID_CHANGE_C,
        },
        "limits": [
            "Архивный часовой пояс требует подтверждения. Сравнение ошибок помогает оценить две трактовки на этом месяце.",
            "Модельные ячейки не разрешают микроклимат станций. Фронты и другие быстрые изменения могут быть сглажены или сдвинуты относительно рядов станций.",
            "Вычисленный часовой эталон зависит от частоты записи и линейной аппроксимации между замерами. На часы без пригодных замеров, другие месяцы, давление и влажность эти метрики напрямую не переносятся.",
        ],
        "stations": stations,
    }


def main():
    result = validate()
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(RESULT)


if __name__ == "__main__":
    main()
