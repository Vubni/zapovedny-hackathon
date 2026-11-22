"""Private import queue, multi-parameter quality control, and published observation revisions."""
from __future__ import annotations

import csv
import json
import re
import zlib
from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO, StringIO
from itertools import groupby
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from analytics import PARAMS
from build_data import LIMITS
from external_fill import (
    MAX_DAYS, MODELS, apply_estimates, external_reference, external_warnings, reference_inputs, station_location, time_zone,
)
from gap_fill import VERIFICATION_LABELS, estimate_bounds, fill_settings, station_estimates, verification_history, verify_estimates
from observations_db import connection, current_center
from review_excel import (
    BAD, ESTIMATED, GOOD, _cell, _column, _header, _row, _sheet_open, _write_package,
)
from review_filter import (
    FILTERS, FILTER_LABELS, STAMP_PATTERN, checked_date, mark_hampel, mark_spikes, mark_stuck,
    number, prepare, quality_metadata, quality_options, quality_profile, resolve_conflicts, uploaded_observations,
)
from review_upload import MAX_BYTES, SourceRow, csv_rows, read_upload
from station_catalog import catalog

MIME_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
PRIMARY = {
    "description": "Сначала проверяем рабочие числовые границы и исходные флаги, включая флаг конфликтного срока.",
    **{key: value for key, value in quality_metadata().items() if key != "filters"},
}
FILL_OPTIONS = [
    {"id": "none", "label": "Оставить пропуски", "description": "Сохраняем пропуски после проверки."},
    {"id": "external", "label": "Сверить с метеосервисами",
     "description": "Справочные значения ERA5 и NASA POWER по координатам станции до 31 суток. "
                    "При согласованности они могут скорректировать оценку модели станции."},
    {"id": "model", "label": "Восстановить по данным станции",
     "description": "Короткие пропуски между измерениями восстанавливаются по соседним наблюдениям. "
                    "Поправка модели оценивается по данным до пропуска."},
]
POINT_BUDGET = 1200
TIMELINE_BUDGET = 1500
MAX_CORRECTED_BYTES = 16 * 1024 * 1024
FILE_COLUMNS = (
    'id, filename, columns, rows, start, "end", station, error, created_at, updated_at, '
    "generation, version, draft_meta, confirmed_meta"
)


def _database(*, write=False):
    return connection(write=write, require="published")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dumps(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _pack(payload):
    return zlib.compress(_dumps(payload).encode(), level=3)


def _unpack(payload):
    return json.loads(zlib.decompress(payload))


def _params(fields):
    return [{"id": key, "label": label, "unit": unit} for key, (label, unit) in PARAMS.items() if key in fields]


def _station(value):
    for item in catalog():
        if item["id"] == value:
            return item
    raise ValueError("Выберите станцию из каталога.")


def _lookup(db, query, extra=""):
    identifier = query.get("id", "")
    row = db.execute(f"SELECT {FILE_COLUMNS}{extra} FROM files WHERE id=?", (identifier,)).fetchone() if db else None
    if row is None:
        raise ValueError("Файл не найден в истории импорта.")
    return row


def _summary(raw):
    if raw is None:
        return None
    summary = json.loads(raw)
    summary["settings"].update(quality_options(summary["settings"]))
    summary["settings"].update(fill_settings(summary["settings"]))
    summary["settings"].setdefault("fill_start", None)
    summary["settings"].setdefault("fill_end", None)
    summary["settings"].setdefault("fill_params", [])
    return summary


def _file_columns(row):
    header = json.loads(row["columns"])
    if isinstance(header, list):
        return header, None
    return header["fields"], header["provenance"]


def _record(row):
    draft = _summary(row["draft_meta"])
    confirmed = _summary(row["confirmed_meta"])
    fields, provenance = _file_columns(row)
    active = draft or confirmed
    status = "uploaded"
    if row["error"]:
        status = "error"
    elif confirmed:
        status = "confirmed"
    elif draft:
        status = "preview"
    elif row["version"]:
        status = "original"
    record = {
        "id": row["id"], "filename": row["filename"], "status": status, "error": row["error"],
        "station": active["settings"]["station"] if active else row["station"],
        "params": _params(fields),
        "start": row["start"], "end": row["end"], "rows": row["rows"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "version": row["version"], "settings": active["settings"] if active else None,
        "preview": draft, "confirmed": confirmed,
    }
    if provenance is not None:
        record["provenance"] = provenance
    return record


def list_files():
    with _database() as db:
        rows = db.execute(f"SELECT {FILE_COLUMNS} FROM files ORDER BY created_at DESC, id") if db else ()
        files = [_record(row) for row in rows]
    return {
        "files": files, "primary": PRIMARY, "fills": FILL_OPTIONS,
        "filters": [{"id": key, "label": FILTER_LABELS[key], "description": value}
                    for key, value in FILTERS.items()],
    }


def file_record(query):
    with _database() as db:
        return _record(_lookup(db, query))


def review_source(identifier):
    with _database() as db:
        row = _lookup(db, {"id": identifier}, ", original, mime")
        return _record(row), row["original"], row["mime"]


def _filename(query):
    name = query.get("filename", "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if len(name) > 255:
        raise ValueError("Название файла не должно превышать 255 символов.")
    return name


def upload_file(query, body, content_type):
    name = _filename(query)
    suffix = Path(name).suffix.lower()
    if not body or len(body) > MAX_BYTES:
        raise ValueError("Загрузите непустой файл размером до 12 МиБ.")
    station = _station(query.get("station") or "kr1")["id"]
    source = fields = None
    start = end = error = None
    rows = 0
    try:
        if suffix not in MIME_TYPES:
            raise ValueError("Поддерживаются форматы CSV, XLSX и JSON.")
        records = read_upload(body, name, MIME_TYPES[suffix])
        fields = (list(dict.fromkeys(key for record in records for key in record))
                  if suffix == ".json" else list(records[0]))
        parameter = next(param for param in PARAMS if param in fields)
        dates = [item.time[:10] for item in uploaded_observations(records, name, parameter)]
        start, end = min(dates), max(dates)
        rows = len(records)
        source = _pack([(record.source_line, dict(record)) for record in records])
    except json.JSONDecodeError as exc:
        error = f"Не удалось прочитать JSON. Ошибка в строке {exc.lineno}, столбце {exc.colno}."
    except RecursionError:
        error = "Ожидается JSON-массив записей без вложенных объектов."
    except (ValueError, OverflowError) as exc:
        error = str(exc)
    identifier, now = uuid4().hex, _now()
    with _database(write=True) as db:
        db.execute(
            'INSERT INTO files (id, filename, mime, original, source, columns, rows, start, "end", station, '
            "error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, name or "исходный файл", MIME_TYPES.get(suffix, "application/octet-stream"),
             body, source, _dumps(fields or []), rows, start, end, station, error, now, now),
        )
        return _record(_lookup(db, {"id": identifier}))


def _settings(query, default_station, *, allow_legacy_buddy=False):
    fields = ("station", "filters", "fill", "timezone", "subhour", "pressure_reference", "sources", "conflict_policy",
              "fill_start", "fill_end", "fill_params")
    if any(key in query and not isinstance(query[key], str) for key in fields):
        raise ValueError("Настройки обработки должны быть текстовыми значениями.")
    station = _station(query.get("station") or default_station)
    requested = {item.strip() for item in query.get("filters", "").split(",") if item.strip()}
    allowed = (*FILTERS, "buddy") if allow_legacy_buddy else tuple(FILTERS)
    if requested - set(allowed):
        raise ValueError("Выберите фильтры из списка.")
    filters = [item for item in allowed if item in requested]
    filling = fill_settings(query)
    zone = query.get("timezone") or None
    if zone is not None:
        time_zone(zone)
    fill_start = fill_end = None
    fill_params = []
    if filling["fill_external"] or filling["fill_model"]:
        fill_start, fill_end = query.get("fill_start") or None, query.get("fill_end") or None
        if (fill_start is None) != (fill_end is None) or (
            fill_start is not None and checked_date(fill_start) > checked_date(fill_end)
        ):
            raise ValueError("Укажите начало и конец периода заполнения в календарном порядке.")
        requested_params = {item.strip() for item in query.get("fill_params", "").split(",") if item.strip()}
        if requested_params - PARAMS.keys():
            raise ValueError("Выберите поддерживаемые показатели для заполнения.")
        fill_params = [param for param in PARAMS if param in requested_params]
    subhour = pressure_reference = None
    sources = []
    if filling["fill_external"]:
        station_location(station)
        if zone is None:
            time_zone(zone)
        subhour = query.get("subhour") or "skip"
        if subhour not in {"skip", "interpolate"}:
            raise ValueError("Выберите режим обработки минутных сроков.")
        pressure_reference = query.get("pressure_reference") or None
        if pressure_reference not in (None, "surface"):
            raise ValueError("Укажите давление у поверхности или оставьте его тип неизвестным.")
        source_ids = {item.strip() for item in (query.get("sources") or ",".join(MODELS)).split(",")}
        if not source_ids or source_ids - MODELS.keys():
            raise ValueError("Выберите поддерживаемые внешние источники.")
        sources = [source for source in MODELS if source in source_ids]
    policy = query.get("conflict_policy") or "all"
    if policy not in {"all", "best"}:
        raise ValueError("Выберите способ разбора конфликтов.")
    if "conflicts" not in filters:
        policy = "all"
    return {
        "station": station["id"], "filters": filters, **filling, "timezone": zone,
        "fill_start": fill_start, "fill_end": fill_end, "fill_params": fill_params,
        "subhour": subhour, "pressure_reference": pressure_reference, "sources": sources,
        "conflict_policy": policy, **quality_options(query),
    }, station


def _quality(records, name, parameter, settings, station):
    profile = quality_profile(station)
    rows = sorted(uploaded_observations(records, name, parameter), key=lambda row: row.stamp)
    bounds = settings["ranges"][parameter]
    thresholds = settings["thresholds"]
    requested = settings["filters"]
    use_references = bool(set(requested) - {"conflicts"} or settings["conflict_policy"] == "best")
    references, conflicts = prepare(rows, parameter, bounds, use_references=use_references)
    # Imports keep the original QC decision even when the number lies within our ranges.
    for row in rows:
        previous = row.original.get("origin_decision")
        origin = row.original.get("origin")
        if isinstance(previous, str) and previous and isinstance(origin, str) and origin.startswith("confirmed:"):
            row.note("previously_excluded", f"Ранее исключено при обработке файла: {previous}.", reject=True)
            row.base_keep = False
        if row.base_keep and f"{parameter}:range" in row.flags:
            row.notes = [(code, text) for code, text in row.notes if code != "source_range_rechecked"]
            row.note("source_range", f"Исходный флаг {parameter}:range исключает значение.", reject=True)
            row.base_keep = False
    references = [row for row in references if row.base_keep]
    stamps = [row.stamp for row in references]
    policy = settings["conflict_policy"] if "conflicts" in requested else "off"
    resolve_conflicts(conflicts, references, stamps, parameter, profile, policy)
    for group in conflicts:
        if any("dup_conflict" in row.flags for row in group):
            for row in group:
                row.note("source_duplicate", "Исходный флаг dup_conflict исключает весь срок.", reject=True)
    if "hampel" in requested:
        mark_hampel(rows, references, stamps, parameter, profile, thresholds["hampel"][parameter])
    if "spike" in requested:
        mark_spikes(rows, references, stamps, parameter, profile, thresholds["spike"][parameter])
    if "stuck" in requested:
        mark_stuck(rows, parameter, profile, thresholds["stuck"][parameter])
    return rows


def _external_references(rows, parameter, settings, station, start, end, *, validate=False):
    start_text, end_text = start.isoformat(), end.isoformat()
    selected = [row for row in rows if start_text <= row["time"][:10] <= end_text]
    report = {"param": parameter, "start": start_text, "end": end_text,
              "rows": reference_inputs(selected, settings["subhour"], validate)}
    request = {
        "fill": "1", "timezone": settings["timezone"], "subhour": settings["subhour"],
        "pressure_reference": settings["pressure_reference"], "sources": ",".join(settings["sources"]),
        **station_location(station),
    }
    apply_estimates(report, request)
    references, observed_references = {}, {}
    first, last = rows[0]["time"], rows[-1]["time"]
    bounds = estimate_bounds(parameter, settings.get("ranges"))
    outside_range = 0
    for row in report["rows"]:
        if not first <= row["time"] <= last:
            continue
        reference = external_reference(row, bounds)
        if reference is None:
            continue
        if row.get("calibration_sample"):
            observed_references[row["time"]] = reference
            continue
        outside_range += reference["value"] is not None and not reference["usable"]
        references[row["time"]] = reference
    status = report["external"]
    if outside_range:
        status.setdefault("reason_counts", {})["outside_parameter_range"] = outside_range
    observed_times = {row["time"] for row in rows}
    comparable = {stamp for stamp, reference in references.items() if reference["value"] is not None}
    status.update(status="reference" if comparable else "unavailable", estimated_count=0, reference_count=len(comparable),
                  validation_reference_count=sum(item["usable"] for item in observed_references.values()),
                  synthetic_count=sum(stamp not in observed_times for stamp in comparable))
    return references, observed_references, status


def _sample(sequence, limit):
    if len(sequence) <= limit:
        return sequence
    return [sequence[index * (len(sequence) - 1) // (limit - 1)] for index in range(limit)]


def _point_indices(rows, estimates):
    marked, estimated = [], []
    for index, row in enumerate(rows):
        if not row.keep and row.quality != "missing":
            marked.append(index)
        if row.time in estimates:
            estimated.append(index)
    if len(rows) <= POINT_BUDGET:
        return range(len(rows))
    selected = set(_sample(marked, POINT_BUDGET // 3)) | set(_sample(estimated, POINT_BUDGET // 3))
    return sorted(selected | set(_sample(range(len(rows)), POINT_BUDGET - len(selected))))


def _source_flags(row, parameter):
    return ";".join(flag for flag in row.flags if flag == "dup_conflict" or flag.startswith(parameter + ":"))


def _record_point(row, parameter, marked, estimate, reference, verification):
    flags = _source_flags(row, parameter)
    reasons = [description for _, description in row.notes]
    if flags and not any(flags in reason for reason in reasons):
        reasons.append(f"Исходный флаг: {flags}.")
    return {
        "time": row.time, "line": row.line, "value": row.value, "marked": marked,
        "estimate": estimate, "external_reference": reference, "verification": verification,
        "qc_flag": flags, "reasons": reasons,
    }


def _review(source, fields, filename, settings, station, start, end):
    warnings = []
    supported = {param["id"] for param in station["params"]}
    parameters = _params(supported & set(fields))
    omitted = _params((set(fields) & PARAMS.keys()) - supported)
    if omitted:
        warnings.append("В паспорте станции нет параметров: " + ", ".join(item["label"].lower() for item in omitted) + ".")
    if not parameters:
        raise ValueError("В файле нет параметров выбранной станции.")
    declared = [item["id"] for item in parameters]
    for param in declared:
        lower, upper = settings["ranges"][param]
        if (lower, upper) != LIMITS[param]:
            label, unit = PARAMS[param]
            warnings.append(f"Рабочий диапазон · {label.lower()} · {lower}–{upper} {unit}.")
    fill_start = checked_date(settings["fill_start"]) if settings["fill_start"] else start
    fill_end = checked_date(settings["fill_end"]) if settings["fill_end"] else end
    if fill_start < start or fill_end > end:
        raise ValueError("Выберите период заполнения в пределах дат исходных наблюдений.")
    fill_params = set(settings["fill_params"] or declared)
    if fill_params - set(declared):
        raise ValueError("Выбранный показатель для заполнения отсутствует в исходном ряду станции.")
    if settings["fill_external"] and (fill_end - fill_start).days >= MAX_DAYS:
        raise ValueError(f"Для внешних источников выберите период заполнения до {MAX_DAYS} суток.")
    if settings["fill_external"] and station.get("location_precision") == "area":
        label = station.get("location_label") or "район архива"
        reference = station.get("location_reference")
        detail = f" ({reference})" if reference else ""
        warnings.append(f"Геопривязка внешних оценок приблизительная: {label}{detail}.")
    records = [SourceRow(values, line) for line, values in source]
    cooked = {}
    changes = []
    repeats = []
    counts, points, models, external, external_references = {}, {}, {}, {}, {}
    totals = {"marked": 0, "filled": 0, "references": 0}
    timeline = {}
    span = (end - start).days + 1
    bucket = max(1, (span + TIMELINE_BUDGET - 1) // TIMELINE_BUDGET)

    def term(stamp, synthetic=False):
        if stamp not in cooked:
            cooked[stamp] = {
                "datetime": stamp, "synthetic": synthetic, "declared": [] if synthetic else declared, "estimates": {},
            }
        return cooked[stamp]

    def bin_for(stamp):
        day = date.fromisoformat(stamp[:10])
        if bucket > 1:
            day = date.fromordinal(day.toordinal() - ((day - start).days % bucket))
        label = day.isoformat()
        return timeline.setdefault(label, {
            "time": label, "marked": 0, "filled": 0, "parameters": {}, "filled_parameters": {},
            "references": 0, "reference_parameters": {},
        })

    originals = None
    for item in parameters:
        parameter = item["id"]
        rows = _quality(records, filename, parameter, settings, station)
        if originals is None:
            originals = {row.time for row in rows}
            for stamp in sorted(originals):
                term(stamp)
                bin_for(stamp)
            for row in rows:
                origin = row.original.get("origin")
                if isinstance(origin, str) and origin.startswith("archive:") and "dup_conflict" in row.flags:
                    term(row.time)["archived_conflict"] = True

        estimates, references, verifications = {}, {}, {}
        if parameter in fill_params and (settings["fill_external"] or settings["fill_model"]):
            terms = [{"time": row.time, "keep": row.keep, "value": row.value, "conflict": row.conflict} for row in rows]
            if settings["fill_model"]:
                estimates, models[parameter] = station_estimates(
                    terms, parameter, ranges=settings.get("ranges"),
                    period=(fill_start, fill_end),
                )
            if settings["fill_external"]:
                references, observed_refs, external[parameter] = _external_references(
                    terms, parameter, settings, station, fill_start, fill_end, validate=bool(estimates),
                )
                history = verification_history(terms, observed_refs, parameter, settings.get("ranges"), settings["timezone"])
                estimates, verifications = verify_estimates(estimates, references, parameter, history=history)
                external[parameter]["validation_pair_count"] = len(history)
                external[parameter]["adjusted_count"] = sum(item["method"] == "model_adjusted" for item in estimates.values())
                external_references[parameter] = references

        stat = {"marked": 0, "kept": 0, "missing": 0, "filled": len(estimates), "references": 0}
        for row in rows:
            if row.keep:
                target = term(row.time)
                if parameter in target:
                    repeats.append((row.time, row.line, parameter, row.value))
                    row.note("duplicate_collapsed", "В рабочем ряду сохранена первая пригодная ячейка срока.")
                else:
                    target[parameter] = row.value
                stat["kept"] += 1
            elif row.quality == "missing":
                stat["missing"] += 1
            else:
                stat["marked"] += 1
                group = bin_for(row.time)
                group["marked"] += 1
                group["parameters"][parameter] = group["parameters"].get(parameter, 0) + 1
                changes.append((row.time, row.line, parameter, row.original.get(parameter),
                                [code for code, _ in row.notes], _source_flags(row, parameter),
                                row.original.get("origin_decision")))

        for stamp, estimate in estimates.items():
            term(stamp, stamp not in originals)["estimates"][parameter] = estimate
            group = bin_for(stamp)
            group["filled"] += 1
            filled_parameters = group["filled_parameters"]
            filled_parameters[parameter] = filled_parameters.get(parameter, 0) + 1
        for stamp, reference in references.items():
            if reference["value"] is not None:
                group = bin_for(stamp)
                group["references"] += 1
                reference_parameters = group["reference_parameters"]
                reference_parameters[parameter] = reference_parameters.get(parameter, 0) + 1
                stat["references"] += 1
        seen = estimates.keys() | references.keys()
        selected = [
            _record_point(row, parameter, not row.keep and row.quality != "missing", estimates.get(row.time),
                          references.get(row.time), verifications.get(row.time))
            for row in (rows[index] for index in _point_indices(rows, seen))
        ]
        synthetic = [
            {"time": stamp, "line": None, "value": None, "marked": False,
             "estimate": estimates.get(stamp), "external_reference": references.get(stamp),
             "verification": verifications.get(stamp), "reasons": ["В исходном файле нет записи за этот срок."]}
            for stamp in sorted(seen - originals)
        ]
        selected.extend(_sample(synthetic, POINT_BUDGET // 3))
        points[parameter] = sorted(selected, key=lambda item: item["time"])
        counts[parameter] = stat
        totals["marked"] += stat["marked"]
        totals["filled"] += stat["filled"]
        totals["references"] += stat["references"]

    for parameter, status in external.items():
        warnings.extend(f"{PARAMS[parameter][0]} · {message}" for message in external_warnings(status))
    if models and not any(status["estimated_count"] or status.get("reason") == "no_remaining_gaps"
                          for status in models.values()):
        warnings.append("Модель станции не нашла коротких пропусков с пригодными соседними наблюдениями.")
    if repeats:
        warnings.append("При повторе срока рабочий ряд использует первую пригодную исходную ячейку "
                        "по каждому параметру. Остальные значения сохранились в оригинале и листе повторов XLSX.")
    return {
        "params": parameters, "warnings": warnings, "stats": dict(totals, parameters=counts),
        "points": points, "timeline": [timeline[stamp] for stamp in sorted(timeline)], "timeline_days": bucket,
        "model": models, "external": external, "external_references": external_references,
        "rows": [cooked[stamp] for stamp in sorted(cooked)],
        "changes": changes, "repeats": repeats,
    }


def _response(row, payload, summary):
    return {
        key: value for key, value in payload.items() if key not in {"rows", "changes", "repeats", "manual_cells", "external_references"}
    } | {"file": _record(row), "id": row["id"], "revision": summary["revision"], "settings": summary["settings"]}


def _draft_summary(settings, payload, created):
    return {
        "revision": uuid4().hex, "settings": settings, "stats": payload["stats"], "created_at": created,
        "params": [param["id"] for param in payload["params"]],
    }


def _selection_files(db, scope):
    if db is None:
        return []
    rows = db.execute(f'SELECT {FILE_COLUMNS} FROM files WHERE start=? AND "end"=?', (scope["start"], scope["end"]))
    files = []
    for row in rows:
        _, provenance = _file_columns(row)
        if provenance and all(provenance.get(key) == value for key, value in scope.items()):
            files.append((row, provenance))
    return files


def _confirmed_input(file_id, version):
    with _database() as db:
        row = (db.execute("SELECT source, confirmed FROM files WHERE id=? AND version=?", (file_id, version)).fetchone()
               if db is not None else None)
    if row is None or row["source"] is None or row["confirmed"] is None:
        return (), None
    return _unpack(row["source"]), _unpack(row["confirmed"])


def _legacy_selection_matches(db, row, parameter, records, payload):
    source = db.execute("SELECT source FROM files WHERE id=?", (row["id"],)).fetchone()[0]
    if source is None:
        return False
    origins = {item["datetime"]: item["origin"] for _, item in records}
    incoming = [(item["datetime"], item.get(parameter), origins[item["datetime"]]) for item in payload["rows"]]
    previous = [(item["datetime"], number(item.get(parameter))[0], item["origin"]) for _, item in _unpack(source)]
    return previous == incoming


def from_observations(query):
    """Persist a raw snapshot and reopen it while the independent input stays the same."""
    from observation_selection import selection_rows

    station = _station(query.get("station"))
    station_id, parameter = station["id"], query.get("param")
    if not isinstance(parameter, str) or parameter not in {item["id"] for item in station["params"]}:
        raise ValueError("Выберите показатель, который измеряет станция.")
    first, last = checked_date(query.get("start")), checked_date(query.get("end"))
    if first > last:
        raise ValueError("Дата начала должна предшествовать дате окончания.")
    start, end = first.isoformat(), last.isoformat()
    scope = {"kind": "observations", "station": station_id, "param": parameter, "start": start, "end": end}
    with _database() as db:
        existing = _selection_files(db, scope)
    previous_ids = {row["id"] for row, _ in existing}
    confirmed = confirmed_rows(
        station_id, start, end, include_observation_sources=True, exclude_file_ids=previous_ids,
    )
    records, source_counts = selection_rows(station_id, parameter, start, end, confirmed, _confirmed_input)
    fields = ["datetime", parameter, "qc_flag", "origin", "origin_line", "origin_decision"]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(row for _, row in records)
    body = output.getvalue().encode("utf-8-sig")
    fingerprint = sha256(body).hexdigest()
    for row, provenance in existing:
        if provenance.get("fingerprint") == fingerprint:
            return _record(row)

    identity = f"meteoarchive:observations:{current_center()}:{station_id}:{parameter}:{start}:{end}:{fingerprint}"
    identifier = uuid5(NAMESPACE_URL, identity).hex
    filename = f"{station_id}-{parameter}-{start}-{end}-observations.csv"
    settings, _ = _settings({"station": station_id}, station_id)
    payload = _review(records, fields, filename, settings, station, first, last)
    stats = payload["stats"]["parameters"][parameter]
    provenance = {**scope, "fingerprint": fingerprint, "sources": source_counts,
                  "measurements": sum(parameter in item for item in payload["rows"]),
                  "empty": stats["missing"], "flagged": stats["marked"]}
    created = _now()
    summary = _draft_summary(settings, payload, created)
    header = _dumps({"fields": fields, "provenance": provenance})
    source, draft = _pack(records), _pack(payload)
    with _database(write=True) as db:
        for row, prior in _selection_files(db, scope):
            if prior.get("fingerprint") == fingerprint:
                return _record(row)
            if "fingerprint" in prior or not _legacy_selection_matches(db, row, parameter, records, payload):
                continue
            # Only refresh the original, untouched automatic draft from the old export format.
            if row["version"] or row["generation"] or not row["draft_meta"]:
                return _record(row)
            db.execute(
                "UPDATE files SET original=?, source=?, columns=?, rows=?, updated_at=?, draft=?, draft_meta=?, "
                "generation=generation+1 WHERE id=?",
                (body, source, header, len(records), created, draft, _dumps(summary), row["id"]),
            )
            return _record(_lookup(db, {"id": row["id"]}))
        db.execute(
            'INSERT INTO files (id, filename, mime, original, source, columns, rows, start, "end", station, '
            "created_at, updated_at, draft, draft_meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            (identifier, filename, MIME_TYPES[".csv"], body, source, header, len(records),
             start, end, station_id, created, created, draft, _dumps(summary)),
        )
        return _record(_lookup(db, {"id": identifier}))


def preview_file(query):
    with _database() as db:
        row = _lookup(db, query, ", source")
    if row["error"]:
        raise ValueError(row["error"])
    settings, station = _settings(query, row["station"])
    with _database(write=True) as db:
        generation = _lookup(db, query)["generation"] + 1
        db.execute("UPDATE files SET generation=?, draft=NULL, draft_meta=NULL WHERE id=?",
                   (generation, row["id"]))
    fields, _ = _file_columns(row)
    payload = _review(_unpack(row["source"]), fields, row["filename"], settings, station,
                      checked_date(row["start"]), checked_date(row["end"]))
    created = _now()
    summary = _draft_summary(settings, payload, created)
    stored = _pack(payload)
    with _database(write=True) as db:
        result = db.execute(
            "UPDATE files SET draft=?, draft_meta=?, updated_at=? WHERE id=? AND generation=?",
            (stored, _dumps(summary), created, row["id"], generation),
        )
        if not result.rowcount:
            raise ValueError("В другом запросе начат новый расчёт. Откройте актуальный предпросмотр.")
        return _response(_lookup(db, query), payload, summary)


def _saved(query):
    with _database() as db:
        row = _lookup(db, query, ", draft, confirmed")
    revision = query.get("revision")
    for key in ("draft", "confirmed"):
        if row[key]:
            summary = _summary(row[f"{key}_meta"])
            if not revision or revision == summary["revision"]:
                return row, _unpack(row[key]), summary
    raise ValueError("Предпросмотр устарел или ещё не рассчитан. Нажмите «Отфильтровать».")


def get_file(query):
    return _response(*_saved(query))


def draft_csv(query):
    row, payload, _ = _saved(query)
    fields = ["datetime", *(item["id"] for item in payload["params"])]
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(fields)
    for term in payload["rows"]:
        if not term["synthetic"]:
            writer.writerow([term.get(field) for field in fields])
    return f"{Path(row['filename']).stem}-draft.csv", output.getvalue().encode("utf-8-sig")


def _corrected_time(value, line):
    raw = value.strip()
    try:
        if STAMP_PATTERN.fullmatch(raw):
            return datetime.fromisoformat(raw).isoformat(" ", timespec="minutes")
        return datetime.strptime(raw, "%d.%m.%Y %H:%M").isoformat(" ", timespec="minutes")
    except ValueError:
        raise ValueError(f"Строка {line}: сохраните исходную дату и время срока.") from None


def _apply_corrections(payload, settings, body):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Сохраните исправленную таблицу как CSV в UTF-8.") from None
    parameters = [item["id"] for item in payload["params"]]
    columns = {"datetime", *parameters}
    rows = {item["datetime"]: item for item in payload["rows"] if not item["synthetic"]}
    pending = set(rows)
    edits = payload.setdefault("manual_cells", {})
    applied = 0
    for source in csv_rows(text):
        if source.keys() != columns:
            raise ValueError("Столбцы исправленного CSV должны совпадать со столбцами скачанного черновика.")
        line = source.source_line
        stamp = _corrected_time(source["datetime"], line)
        if stamp not in pending:
            raise ValueError(f"Строка {line}: повторяющийся или новый срок {stamp}. Сохраните исходные сроки.")
        pending.remove(stamp)
        term = rows[stamp]
        for parameter in parameters:
            raw = source[parameter]
            value, quality = number(raw)
            lower, upper = settings["ranges"][parameter]
            if quality == "invalid_number" or (value is not None and not lower <= value <= upper):
                raise ValueError(f"Строка {line}, {parameter}: введите число в диапазоне {lower}–{upper} или оставьте ячейку пустой.")
            before = term.get(parameter)
            if value == before:
                continue
            parameter_edits = edits.setdefault(parameter, {})
            initial = parameter_edits.get(stamp, {}).get("initial_value", before)
            parameter_edits[stamp] = {"value": value, "previous_value": before, "initial_value": initial, "line": line}
            term["estimates"].pop(parameter, None)
            stats = payload["stats"]["parameters"][parameter]
            stats["kept"] += int(value is not None) - int(before is not None)
            if value is None:
                del term[parameter]
            else:
                term[parameter] = value
            applied += 1
    if pending:
        raise ValueError("В исправленном CSV отсутствуют сроки из черновика.")
    if not applied:
        raise ValueError("В CSV нет изменений относительно черновика.")
    return applied


def _manual_timeline(payload):
    timeline = payload["timeline"]
    origin = date.fromisoformat(timeline[0]["time"])
    bucket = payload["timeline_days"]
    groups = {entry["time"]: entry for entry in timeline}
    fills = Counter()
    method_fills = Counter()
    method_synthetic = Counter()
    adjusted_counts = Counter()
    for group in groups.values():
        group.update(filled=0, filled_parameters={}, manual=0, manual_parameters={})

    def group_for(stamp):
        day = date.fromisoformat(stamp[:10])
        day = date.fromordinal(day.toordinal() - ((day - origin).days % bucket))
        return groups[day.isoformat()]

    for row in payload["rows"]:
        if row["estimates"]:
            group = group_for(row["datetime"])
            for parameter, estimate in row["estimates"].items():
                group["filled"] += 1
                group["filled_parameters"][parameter] = group["filled_parameters"].get(parameter, 0) + 1
                fills[parameter] += 1
                method = estimate.get("method")
                if method == "model_adjusted":
                    adjusted_counts[parameter] += 1
                    method = "model"
                method_fills[method, parameter] += 1
                method_synthetic[method, parameter] += row["synthetic"]
    for parameter, entries in payload["manual_cells"].items():
        for stamp in entries:
            group = group_for(stamp)
            group["manual"] += 1
            group["manual_parameters"][parameter] = group["manual_parameters"].get(parameter, 0) + 1
    for parameter, stats in payload["stats"]["parameters"].items():
        stats["filled"] = fills[parameter]
    payload["stats"]["filled"] = sum(fills.values())
    for method in ("external", "model"):
        for parameter, status in payload.get(method, {}).items():
            if method == "external" and "reference_count" in status:
                status["adjusted_count"] = adjusted_counts[parameter]
                continue
            count = method_fills[method, parameter]
            if status["status"] == "estimated" and not count:
                status.update(status="unavailable", reason="replaced_manually")
            status["estimated_count"] = count
            if "synthetic_count" in status:
                status["synthetic_count"] = method_synthetic[method, parameter]


def _manual_points(payload):
    edits = payload["manual_cells"]
    sampled = {param: dict(_sample(sorted(items.items()), POINT_BUDGET // 3)) for param, items in edits.items()}
    audited = {(stamp, param): (line, value) for stamp, line, param, value, *_ in payload["changes"]
               if stamp in sampled.get(param, {})}
    for parameter, points in payload["points"].items():
        if parameter not in edits:
            continue
        selected = sampled[parameter]
        readings = {}
        base = []
        for point in points:
            if point.get("manual_only"):
                continue
            stamp = point["time"]
            if stamp in edits[parameter]:
                point["estimate"] = None
                for field in ("manual", "manual_value", "previous_value"):
                    point.pop(field, None)
            if stamp in selected and (stamp not in readings or point["marked"]):
                readings[stamp] = point
            base.append(point)
        for stamp, edit in selected.items():
            point = readings.get(stamp)
            if point is None:
                rejected = audited.get((stamp, parameter))
                line, original = rejected if rejected is not None else (None, edit["initial_value"])
                point = {
                    "time": stamp, "line": line, "value": number(original)[0], "marked": rejected is not None,
                    "estimate": None, "reasons": [], "manual_only": True,
                }
                base.append(point)
            point.update(manual=True, manual_value=edit["value"], previous_value=edit["previous_value"])
        payload["points"][parameter] = sorted(base, key=lambda item: item["time"])


def _current_parent(row):
    key = "draft" if row["draft_meta"] else "confirmed"
    return key, _summary(row[f"{key}_meta"])


def corrected_file(query, body, content_type):
    name = _filename(query)
    if Path(name).suffix.lower() != ".csv" or not body or len(body) > MAX_CORRECTED_BYTES:
        raise ValueError("Загрузите непустой исправленный CSV размером до 16 МиБ.")
    with _database() as db:
        row = _lookup(db, query, ", draft, confirmed")
    parent_key, parent = _current_parent(row)
    if not parent or parent["revision"] != query.get("revision"):
        raise ValueError("Версия черновика изменилась. Скачайте актуальный CSV.")
    parent_result = row[parent_key]
    payload = _unpack(parent_result)
    applied = _apply_corrections(payload, parent["settings"], body)
    _manual_timeline(payload)
    _manual_points(payload)
    counts = {param: len(entries) for param, entries in payload["manual_cells"].items()}
    manual = {"parent_revision": parent["revision"], "filename": name, "changed": sum(counts.values()),
              "applied": applied, "parameters": counts}
    payload["manual"] = manual
    revision, now = uuid4().hex, _now()
    summary = {
        "revision": revision, "settings": parent["settings"], "stats": payload["stats"],
        "params": [param["id"] for param in payload["params"]], "created_at": now, "manual": manual,
    }
    stored = _pack(payload)
    with _database(write=True) as db:
        current = _lookup(db, query)
        _, current_parent = _current_parent(current)
        if (current["generation"] != row["generation"] or current["version"] != row["version"]
                or not current_parent or current_parent["revision"] != parent["revision"]):
            raise ValueError("Версия черновика изменилась. Скачайте актуальный CSV.")
        db.execute(
            "INSERT INTO file_corrections (file_id, revision, parent_revision, filename, original_csv, "
            "parent_result, parent_meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], revision, parent["revision"], name, body, parent_result, row[f"{parent_key}_meta"], now),
        )
        db.execute("UPDATE files SET draft=?, draft_meta=?, updated_at=?, generation=generation+1 WHERE id=?",
                   (stored, _dumps(summary), now, row["id"]))
        return _response(_lookup(db, query), payload, summary)


def _publication_entries(identifier, station, rows):
    for row in rows:
        if row.get("archived_conflict") and not any(parameter in row for parameter in row["declared"]):
            if not row["estimates"]:
                continue
            row = dict(row, synthetic=True, declared=[])
        yield identifier, station, row["datetime"], int(row["synthetic"]), _dumps(row)


def confirm_file(query):
    revision = query.get("revision")
    if not revision:
        raise ValueError("Для подтверждения нужен актуальный предпросмотр.")
    with _database() as db:
        row = _lookup(db, query, ", draft")
    record = _record(row)
    confirmed, draft = record["confirmed"], record["preview"]
    legacy = any(item and revision == item["revision"] and "buddy" in item["settings"]["filters"]
                 for item in (draft, confirmed))
    settings, _ = _settings(query, row["station"], allow_legacy_buddy=legacy)
    if confirmed and revision == confirmed["revision"] and settings == confirmed["settings"] and not record["preview"]:
        return record
    if not draft or draft["revision"] != revision or draft["settings"] != settings:
        raise ValueError("Настройки или версия изменились. Запустите фильтрацию заново.")
    payload = _unpack(row["draft"])
    next_version = row["version"] + 1
    confirmed = dict(draft, version=next_version, confirmed_at=_now())
    station = settings["station"]
    entries = list(_publication_entries(row["id"], station, payload["rows"]))
    with _database(write=True) as db:
        current = _lookup(db, query)
        current_record = _record(current)
        if current_record["confirmed"] and current_record["confirmed"]["revision"] == revision and not current["draft_meta"]:
            return current_record
        if current["generation"] != row["generation"] or current["version"] != row["version"]:
            raise ValueError("Предпросмотр устарел. Откройте текущий расчёт перед подтверждением.")
        order = db.execute("SELECT COALESCE(MAX(confirmed_order), 0) + 1 FROM files").fetchone()[0]
        db.execute("DELETE FROM published WHERE file_id=?", (row["id"],))
        db.executemany("INSERT INTO published (file_id, station, time, synthetic, data) VALUES (?, ?, ?, ?, ?)", entries)
        db.execute(
            "UPDATE files SET station=?, version=?, confirmed=draft, confirmed_meta=?, confirmed_order=?, "
            "draft=NULL, draft_meta=NULL, updated_at=? WHERE id=?",
            (station, next_version, _dumps(confirmed), order, confirmed["confirmed_at"], row["id"]),
        )
        return _record(_lookup(db, query))


def discard_clean(query):
    try:
        version = int(query.get("version", ""))
    except (TypeError, ValueError):
        raise ValueError("Укажите актуальную версию очищенных данных.") from None
    with _database(write=True) as db:
        row = _lookup(db, query)
        if version != row["version"] or row["confirmed_meta"] is None:
            raise ValueError("Версия очищенных данных изменилась. Обновите историю файлов.")
        db.execute("DELETE FROM published WHERE file_id=?", (row["id"],))
        db.execute(
            "UPDATE files SET confirmed=NULL, confirmed_meta=NULL, confirmed_order=NULL, draft=NULL, "
            "draft_meta=NULL, version=version+1, generation=generation+1, updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        return _record(_lookup(db, query))


def original_file(query):
    with _database() as db:
        row = _lookup(db, query, ", original, mime")
        return row["filename"], row["mime"], row["original"]


def _write_sheet(output, columns, rows, count):
    output.write(_sheet_open(columns, count + 1, frozen_rows=1, frozen_columns=1).encode())
    output.write(_header(1, columns).encode())
    for index, row in enumerate(rows, 2):
        cells = [_cell(f"{_column(col)}{index}", value, style) for col, (value, style) in enumerate(row, 1)]
        output.write(_row(index, cells).encode())
    output.write((f'</sheetData><autoFilter ref="A1:{_column(len(columns))}{count + 1}"/></worksheet>').encode())


def _quality_settings_table(parameters, settings):
    columns = [
        ("Параметр", 31), ("Единица", 12), ("Нижняя граница", 17), ("Верхняя граница", 17),
        ("Хампель · отклонение", 22), ("Скачок · отклонение", 22), ("Без изменений, ч", 19),
        ("Повторы срока", 25), ("Архивный фильтр", 28),
    ]
    filters = settings["filters"]
    repeat_mode = None
    if "conflicts" in filters:
        repeat_mode = "Разобрать" if settings["conflict_policy"] == "best" else "Исключить все"
    rows = []
    for param in parameters:
        label, unit = PARAMS[param]
        thresholds = [settings["thresholds"][method][param] if method in filters else None
                      for method in ("hampel", "spike", "stuck")]
        legacy = "Соседняя станция" if param == "t_air" and "buddy" in filters else None
        values = [label, unit, *settings["ranges"][param], *thresholds, repeat_mode, legacy]
        rows.append([(value, 0) for value in values])
    return columns, rows, len(rows)


def preview_excel(query):
    record, payload, summary = _saved(query)
    parameters = [item["id"] for item in payload["params"]]
    rows, changes = payload["rows"], payload["changes"]
    marked = {(item[0], item[2]) for item in changes}

    def working_rows():
        for row in rows:
            time = row["datetime"]
            cells = [(time, 0)]
            for parameter in parameters:
                if parameter in row["estimates"]:
                    cells.append((row["estimates"][parameter]["value"], ESTIMATED))
                elif parameter in row:
                    cells.append((row[parameter], GOOD))
                elif (time, parameter) in marked:
                    cells.append((None, BAD))
                else:
                    cells.append((None, 0))
            yield cells

    estimates = [
        (row["datetime"], param, data)
        for row in rows for param, data in row["estimates"].items()
    ]
    references = [(stamp, param, reference) for param, rows in payload.get("external_references", {}).items()
                  for stamp, reference in rows.items()]
    methods = {"model": "Модель станции", "model_adjusted": "Модель станции со сверкой",
               "external": "Внешняя оценка (сохранённая версия)"}
    repeats = payload.get("repeats", [])
    workbook = BytesIO()
    with ZipFile(workbook, "w", ZIP_DEFLATED, compresslevel=4) as archive:
        names = ["Очищенные данные", "Исключённые ячейки", "Оценки"]
        if repeats:
            names.append("Повторы сроков")
        if references:
            names.append("Справочная сверка")
        names.append("Настройки контроля")
        _write_package(archive, names)
        tables = [
            ([("datetime", 22), *[(param, 18) for param in parameters]], working_rows(), len(rows)),
            ([("Срок", 22), ("Строка источника", 18), ("Параметр", 18), ("Исходная ячейка", 23),
              ("Правила", 38), ("Исходный флаг", 27), ("Причина прежнего исключения", 38)],
             ([(time, 0), (line, 0), (param, 0), (value, BAD), (", ".join(codes), 0),
               (metadata[0] if metadata else None, 0), (metadata[1] if len(metadata) > 1 else None, 0)]
              for time, line, param, value, codes, *metadata in changes), len(changes)),
            ([("Срок", 22), ("Параметр", 18), ("Итог", 18), ("Способ", 27), ("Источник", 48),
              ("Оценка станции", 18), ("Поправка", 14), ("Решение сверки", 48),
              ("Валидация", 62), ("Происхождение оценки", 62)],
             ([(time, 0), (param, 0), (estimate["value"], ESTIMATED), (methods.get(estimate.get("method")), 0),
               (estimate["source"], 0), (estimate.get("local_value"), 0), (verification.get("delta"), 0),
               (VERIFICATION_LABELS.get(verification.get("decision")), 0),
               (estimate["confidence"], 0), (estimate.get("provenance"), 0)]
              for time, param, estimate in estimates
              for verification in [(estimate.get("provenance") or {}).get("verification") or {}]), len(estimates)),
        ]
        if repeats:
            tables.append((
                [("Срок", 22), ("Строка источника", 18), ("Параметр", 18), ("Исходная ячейка", 23)],
                ([(stamp, 0), (line, 0), (param, 0), (value, 0)] for stamp, line, param, value in repeats),
                len(repeats),
            ))
        if references:
            tables.append((
                [("Срок", 22), ("Параметр", 18), ("Справочное значение", 21), ("Источник", 48),
                 ("Сопоставимые данные", 25), ("Происхождение оценки", 62)],
                ([(stamp, 0), (param, 0), (data["value"], 0), (data["source"], 0),
                  ("Да" if data["usable"] else "Нет", 0), (data["provenance"], 0)]
                 for stamp, param, data in sorted(references)), len(references),
            ))
        tables.append(_quality_settings_table(parameters, summary["settings"]))
        for index, (columns, data, count) in enumerate(tables, 1):
            with archive.open(f"xl/worksheets/sheet{index}.xml", "w") as part:
                _write_sheet(part, columns, data, count)
    return f"{Path(record['filename']).stem}-filtered.xlsx", workbook.getvalue()


def _limits(start, end):
    first = start.isoformat() if isinstance(start, date) else checked_date(start).isoformat()
    last = end.isoformat() if isinstance(end, date) else checked_date(end).isoformat()
    return first, last + " 23:59"


def _legacy_params(db, file_id):
    meta, payload = db.execute("SELECT confirmed_meta, confirmed FROM files WHERE id=?", (file_id,)).fetchone()
    fields = json.loads(meta).get("params")
    if fields is None:
        fields = _unpack(payload)["params"]
    return [item["id"] if isinstance(item, dict) else item for item in fields]


def _merge_terms(db, stamp, candidates, legacy, temperature_calendars, include_observation_sources):
    merged = {"datetime": stamp, "synthetic": True, "declared": [], "estimates": {}}
    if include_observation_sources:
        merged["observation_sources"] = {}
    declared = set()
    for entry in candidates:
        term = json.loads(entry["data"])
        estimates = term["estimates"]
        if term["synthetic"]:
            for param, estimate in estimates.items():
                if param not in declared and param not in merged["estimates"]:
                    merged["estimates"][param] = dict(estimate, synthetic=True)
            continue
        merged["synthetic"] = False
        fields = term.get("declared")
        if fields is None:
            file_id = entry["file_id"]
            if file_id not in legacy:
                legacy[file_id] = _legacy_params(db, file_id)
            fields = legacy[file_id]
        for param in fields:
            if param in declared:
                continue
            declared.add(param)
            if include_observation_sources:
                merged["observation_sources"][param] = (entry["file_id"], entry["version"])
            if param in term:
                merged[param] = term[param]
                if param == "t_air" and temperature_calendars is not None:
                    file_id = entry["file_id"]
                    if file_id not in temperature_calendars:
                        meta, = db.execute("SELECT confirmed_meta FROM files WHERE id=?", (file_id,)).fetchone()
                        temperature_calendars[file_id] = json.loads(meta)["settings"].get("timezone")
                    merged["temperature_timezone"] = temperature_calendars[file_id]
            elif param in estimates:
                merged["estimates"][param] = dict(estimates[param], synthetic=False)
    merged["declared"] = [param for param in PARAMS if param in declared]
    return merged


def confirmed_rows(station_id, start, end, *, include_temperature_timezone=False, include_observation_sources=False,
                   exclude_file_ids=()):
    """Yield one row per time with the latest real declaration for each parameter."""
    first, last = _limits(start, end)
    with _database() as db:
        if db is None:
            return
        fields = "p.time, p.data, p.file_id, f.version" if include_observation_sources else "p.time, p.data, p.file_id"
        records = db.execute(
            f"SELECT {fields} FROM published p JOIN files f ON f.id=p.file_id "
            "WHERE p.station=? AND p.time BETWEEN ? AND ? "
            "ORDER BY p.time, p.synthetic, f.confirmed_order DESC",
            (station_id, first, last),
        )
        legacy = {}
        temperature_calendars = {} if include_temperature_timezone else None
        selected = (row for row in records if row["file_id"] not in exclude_file_ids) if exclude_file_ids else records
        for stamp, candidates in groupby(selected, lambda row: row["time"]):
            yield _merge_terms(db, stamp, candidates, legacy, temperature_calendars, include_observation_sources)


def confirmed_days(station_id, start, end):
    first, last = _limits(start, end)
    with _database() as db:
        if db is None:
            return set()
        rows = db.execute("SELECT DISTINCT SUBSTR(time, 1, 10) FROM published WHERE station=? AND time BETWEEN ? AND ?",
                          (station_id, first, last))
        return {row[0] for row in rows}


def date_bounds(station_id):
    with _database() as db:
        if db is None:
            return None, None
        first, last = db.execute("SELECT MIN(time), MAX(time) FROM published WHERE station=?", (station_id,)).fetchone()
    return (first[:10], last[:10]) if first else (None, None)


def all_date_bounds():
    with _database() as db:
        if db is None:
            return {}
        rows = db.execute("SELECT station, MIN(time), MAX(time) FROM published GROUP BY station")
        return {station: (first[:10], last[:10]) for station, first, last in rows}
