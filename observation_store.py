"""Private import queue, multi-parameter quality control, and published observation revisions."""
from __future__ import annotations

import json
import re
import sqlite3
import zlib
from bisect import bisect_left
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from io import BytesIO
from itertools import groupby
from pathlib import Path
from statistics import fmean, median
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from analytics import PARAMS
from build_data import DIRECTION, LIMITS
from external_fill import MAX_DAYS, MODELS, ZONES, apply_estimates
from review_excel import (
    BAD, ESTIMATED, GOOD, _cell, _column, _header, _row, _sheet_open, _write_package,
)
from review_filter import (
    FILTERS, archive_observations, checked_date, mark_buddy, mark_hampel, mark_spikes,
    mark_stuck, minute_bounds, prepare, resolve_conflicts, uploaded_observations,
)
from review_upload import MAX_BYTES, SourceRow, read_upload
from station_catalog import catalog, local_db_path

MIME_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
DEFAULT_PRESSURE_MAX = str(LIMITS["pressure_hpa"][1])
EXPANDED_PRESSURE_MAX = "1550"
PRIMARY = {
    "description": "Сначала проверяем физические диапазоны и исходные флаги, включая флаг конфликтного срока. "
                   "Например, температура воздуха вне диапазона −50…45 °C исключается.",
    "ranges": LIMITS,
}
FILTER_LABELS = {
    "conflicts": "Разбор повторов срока", "hampel": "Локальная медиана",
    "spike": "Одиночные скачки", "stuck": "Залипание прибора", "buddy": "Соседняя станция",
}
FILL_OPTIONS = [
    {"id": "none", "label": "Оставить пропуски", "description": "Сохраняем пропуски после проверки."},
    {"id": "external", "label": "Внешние погодные источники",
     "description": "ERA5 и MERRA-2 по району архива или координатам станции, до 31 суток. "
                    "Нужно указать часовой пояс исходных сроков. Оценки помечаются отдельно."},
    {"id": "model", "label": "Локальная модель заполнения",
     "description": "Модель обучается на пригодных сроках файла, оценивает ошибку на отложенной части "
                    "и заполняет короткие пропуски между измерениями."},
]
POINT_BUDGET = 1200
TIMELINE_BUDGET = 1500
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    original BLOB NOT NULL,
    source BLOB,
    columns TEXT NOT NULL,
    rows INTEGER NOT NULL,
    start TEXT,
    end TEXT,
    station TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    draft BLOB,
    draft_meta TEXT,
    confirmed BLOB,
    confirmed_meta TEXT,
    confirmed_order INTEGER
);
CREATE TABLE IF NOT EXISTS published (
    file_id TEXT NOT NULL REFERENCES files(id),
    station TEXT NOT NULL,
    time TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (file_id, time)
);
CREATE INDEX IF NOT EXISTS published_station_time ON published (station, time);
COMMIT;
"""
FILE_COLUMNS = (
    "id, filename, columns, rows, start, end, station, error, created_at, updated_at, "
    "generation, version, draft_meta, confirmed_meta"
)


@contextmanager
def _database(*, write=False):
    path = local_db_path().resolve()
    if not write and not path.is_file():
        yield None
        return
    if write:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = path if write else path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(target, timeout=15, uri=not write)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='published'").fetchone()
        if not exists:
            if not write:
                yield None
                return
            db.executescript(SCHEMA)
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


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
    summary["settings"].setdefault("pressure_max", DEFAULT_PRESSURE_MAX)
    return summary


def _record(row):
    draft = _summary(row["draft_meta"])
    confirmed = _summary(row["confirmed_meta"])
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
    return {
        "id": row["id"], "filename": row["filename"], "status": status, "error": row["error"],
        "station": active["settings"]["station"] if active else row["station"],
        "params": _params(json.loads(row["columns"])),
        "start": row["start"], "end": row["end"], "rows": row["rows"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "version": row["version"], "settings": active["settings"] if active else None,
        "preview": draft, "confirmed": confirmed,
    }


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


def upload_file(query, body, content_type):
    name = query.get("filename", "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    suffix = Path(name).suffix.lower()
    if len(name) > 255:
        raise ValueError("Название файла не должно превышать 255 символов.")
    if not body or len(body) > MAX_BYTES:
        raise ValueError("Загрузите непустой файл размером до 12 МиБ.")
    station = _station(query.get("station") or "kr1")["id"]
    source = fields = None
    start = end = error = None
    rows = 0
    try:
        if suffix not in MIME_TYPES:
            raise ValueError("Поддерживаются таблицы CSV и XLSX.")
        records = read_upload(body, name, MIME_TYPES[suffix])
        fields = list(records[0])
        parameter = next(param for param in PARAMS if param in fields)
        dates = [item.time[:10] for item in uploaded_observations(records, name, parameter)]
        start, end = min(dates), max(dates)
        rows = len(records)
        source = _pack([(record.source_line, dict(record)) for record in records])
    except (ValueError, OverflowError) as exc:
        error = str(exc)
    identifier, now = uuid4().hex, _now()
    with _database(write=True) as db:
        db.execute(
            "INSERT INTO files (id, filename, mime, original, source, columns, rows, start, end, station, "
            "error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, name or "исходный файл", MIME_TYPES.get(suffix, "application/octet-stream"),
             body, source, _dumps(fields or []), rows, start, end, station, error, now, now),
        )
        return _record(_lookup(db, {"id": identifier}))


def _settings(query, default_station):
    fields = ("station", "filters", "fill", "timezone", "subhour", "pressure_reference", "sources", "conflict_policy")
    if any(key in query and not isinstance(query[key], str) for key in fields):
        raise ValueError("Настройки обработки должны быть текстовыми значениями.")
    station = _station(query.get("station") or default_station)
    requested = {item.strip() for item in query.get("filters", "").split(",") if item.strip()}
    if requested - FILTERS.keys():
        raise ValueError("Выберите фильтры из списка.")
    filters = [item for item in FILTERS if item in requested]
    fill = query.get("fill", "none") or "none"
    if fill not in {item["id"] for item in FILL_OPTIONS}:
        raise ValueError("Выберите способ заполнения из списка.")
    zone = subhour = pressure_reference = None
    sources = []
    if fill == "external":
        if not station["archive"] and station.get("location_precision") != "exact":
            raise ValueError("Для внешней оценки укажите координаты в паспорте станции.")
        zone = query.get("timezone")
        if zone not in ZONES:
            raise ValueError("Для внешней оценки укажите часовой пояс исходных сроков: UTC или МСК.")
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
    pressure_max = str(query.get("pressure_max", DEFAULT_PRESSURE_MAX))
    if pressure_max not in {DEFAULT_PRESSURE_MAX, EXPANDED_PRESSURE_MAX}:
        raise ValueError("Верхний порог давления должен быть 1050 или 1550 гПа.")
    return {
        "station": station["id"], "filters": filters, "fill": fill, "timezone": zone,
        "subhour": subhour, "pressure_reference": pressure_reference, "sources": sources,
        "conflict_policy": policy, "pressure_max": pressure_max,
    }, station


def _quality(records, name, parameter, settings, station, start, end):
    profile = station.get("profile") or station["id"]
    if profile not in {"kr1", "kr2", "ame60"}:
        raise ValueError("Для станции не настроен профиль контроля качества.")
    rows = sorted(uploaded_observations(records, name, parameter), key=lambda row: row.stamp)
    upper = int(settings["pressure_max"]) if parameter == "pressure_hpa" else LIMITS[parameter][1]
    requested = settings["filters"]
    use_references = bool(set(requested) - {"conflicts"} or settings["conflict_policy"] == "best")
    references, conflicts = prepare(rows, parameter, upper, use_references=use_references)
    # Imports keep the original QC decision even when the number lies within our ranges.
    for row in rows:
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
        mark_hampel(rows, references, stamps, parameter, profile)
    if "spike" in requested:
        mark_spikes(rows, references, stamps, parameter, profile)
    if "stuck" in requested:
        mark_stuck(rows, parameter, profile)
    warning = None
    if "buddy" in requested and parameter == "t_air":
        station_id = station["id"]
        if station_id in {"kr1", "kr2"}:
            first, last = minute_bounds(start, end)
            peer_id = "kr2" if station_id == "kr1" else "kr1"
            peer_rows = sorted(
                archive_observations(peer_id, parameter, first - 360, last + 360, {}),
                key=lambda row: row.stamp,
            )
            peer_refs, _ = prepare(peer_rows, parameter, upper)
            if not mark_buddy(rows, references, peer_refs, peer_id, first, last):
                warning = "Для сверки с соседней станцией недостаточно сопоставимых сроков в архиве."
        else:
            warning = "Для выбранной станции нет парного пункта наблюдений для сверки температуры."
    return rows, warning


def _cadence(stamps):
    gaps = Counter(right - left for left, right in zip(stamps, stamps[1:]) if right - left <= 120)
    return min(gaps, key=lambda gap: (-gaps[gap], gap)) if gaps else None


def _interpolate(stamp, left, right):
    return left.value + (right.value - left.value) * (stamp - left.stamp) / (right.stamp - left.stamp)


def _training_residuals(observed, step):
    return [
        target.value - _interpolate(target.stamp, left, right)
        for left, target, right in zip(observed, observed[1:], observed[2:])
        if right.stamp - left.stamp == 2 * step and target.stamp - left.stamp == step
    ]


def _stamp_text(stamp):
    day, minute = divmod(stamp, 1440)
    return f"{date.fromordinal(day).isoformat()} {minute // 60:02}:{minute % 60:02}"


def _model_estimates(rows, parameter, originals, step):
    status = {"status": "unavailable", "training_n": 0, "test_n": 0, "mae": None, "step_minutes": step}
    if parameter == DIRECTION or step is None:
        status["reason"] = "unsupported_direction" if parameter == DIRECTION else "unknown_cadence"
        return {}, status
    observed = [row for row in rows if row.keep and not row.conflict]
    split = len(observed) * 4 // 5
    training = _training_residuals(observed[:split], step)
    holdout = _training_residuals(observed[split:], step)
    status.update(training_n=len(training), test_n=len(holdout))
    if len(training) < 10 or len(holdout) < 3:
        status["reason"] = "insufficient_training"
        return {}, status

    bias = median(training)
    mae = fmean(abs(error - bias) for error in holdout)
    status.update(mae=round(mae, 4), baseline_mae=round(fmean(map(abs, holdout)), 4), bias=round(bias, 4))
    confidence = {"kind": "validation_mae", "mae": status["mae"], "test_n": len(holdout),
                  "unit": PARAMS[parameter][1]}
    candidates = {row.stamp for row in rows if not row.keep and not row.conflict}
    candidates.update(left + step for left, right in zip(originals, originals[1:]) if right - left == 2 * step)
    stamps = [row.stamp for row in observed]
    lower, upper = LIMITS[parameter]
    estimates = {}
    for stamp in sorted(candidates):
        index = bisect_left(stamps, stamp)
        if not 0 < index < len(stamps) or stamps[index] == stamp or stamps[index] - stamps[index - 1] > 2 * step:
            continue
        value = _interpolate(stamp, observed[index - 1], observed[index]) + bias
        if lower <= value <= upper:
            estimates[_stamp_text(stamp)] = {
                "value": round(value, 4), "method": "model", "source": "Регрессия по соседним срокам файла",
                "confidence": confidence, "enters_working_graphs": True,
            }
    if estimates:
        status["status"] = "estimated"
    else:
        status["reason"] = "no_supported_gaps"
    return estimates, status


def _external_estimates(rows, parameter, settings, station, start, end):
    report = {"param": parameter, "start": start.isoformat(), "end": end.isoformat(), "rows": [
        {"time": row.time, "keep": row.keep, "value": row.value, "conflict": row.conflict}
        for row in rows
    ]}
    request = {
        "fill": "1", "timezone": settings["timezone"], "subhour": settings["subhour"],
        "pressure_reference": settings["pressure_reference"], "sources": ",".join(settings["sources"]),
    }
    if station.get("location_precision") == "exact":
        request.update(latitude=station["latitude"], longitude=station["longitude"])
    apply_estimates(report, request)
    estimates = {}
    first, last = rows[0].time, rows[-1].time
    lower, upper = LIMITS[parameter]
    for row in report["rows"]:
        value = row.get("estimate")
        if value is None or not first <= row["time"] <= last or not lower <= value <= upper:
            continue
        source_values = [source["value"] for source in row["provenance"]["sources"]]
        confidence = {"kind": "source_disagreement", "spread": round(max(source_values) - min(source_values), 4),
                      "source_count": len(source_values), "unit": PARAMS[parameter][1]}
        estimates[row["time"]] = {
            "value": value, "method": "external", "source": row["estimate_sources"],
            "confidence": confidence, "provenance": row["provenance"], "enters_working_graphs": True,
        }
    status = report["external"]
    observed_times = {row.time for row in rows}
    status.update(status="estimated" if estimates else "unavailable", estimated_count=len(estimates),
                  synthetic_count=sum(stamp not in observed_times for stamp in estimates))
    return estimates, status


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


def _record_point(row, marked, estimate):
    return {
        "time": row.time, "line": row.line, "value": row.value, "marked": marked,
        "estimate": estimate, "reasons": [description for _, description in row.notes],
    }


def _review(source, fields, filename, settings, station, start, end):
    warnings = []
    supported = {param["id"] for param in station["params"]}
    parameters = _params(supported & set(fields))
    omitted = _params((set(fields) & PARAMS.keys()) - supported)
    if omitted:
        warnings.append("В профиле станции нет параметров: " + ", ".join(item["label"].lower() for item in omitted) + ".")
    if not parameters:
        raise ValueError("В файле нет параметров выбранной станции.")
    declared = [item["id"] for item in parameters]
    if "pressure_hpa" in declared and settings["pressure_max"] == EXPANDED_PRESSURE_MAX:
        warnings.append("Для этого файла выбран расширенный порог давления 1550 гПа. "
                        "Значения выше 1050 гПа могут быть ошибкой прибора.")
    if settings["fill"] == "external" and (end - start).days >= MAX_DAYS:
        raise ValueError(f"Для внешних источников выберите файл с периодом до {MAX_DAYS} суток.")
    if "buddy" in settings["filters"] and not any(item["id"] == "t_air" for item in parameters):
        warnings.append("Сверка с соседней станцией применяется к температуре воздуха.")
    records = [SourceRow(values, line) for line, values in source]
    cooked = {}
    changes = []
    repeats = []
    counts, points, models, external = {}, {}, {}, {}
    totals = {"marked": 0, "filled": 0}
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
        return timeline.setdefault(label, {"time": label, "marked": 0, "filled": 0, "parameters": {}})

    originals, original_minutes, step = None, None, None
    for item in parameters:
        parameter = item["id"]
        rows, warning = _quality(records, filename, parameter, settings, station, start, end)
        if warning:
            warnings.append(warning)
        if originals is None:
            original_minutes = sorted({row.stamp for row in rows})
            originals = {row.time for row in rows}
            step = _cadence(original_minutes)
            for stamp in sorted(originals):
                term(stamp)
                bin_for(stamp)

        estimates = {}
        if settings["fill"] == "external":
            estimates, external[parameter] = _external_estimates(rows, parameter, settings, station, start, end)
        if settings["fill"] == "model":
            estimates, models[parameter] = _model_estimates(rows, parameter, original_minutes, step)

        stat = {"marked": 0, "kept": 0, "missing": 0, "filled": len(estimates)}
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
                                [code for code, _ in row.notes]))

        for stamp, estimate in estimates.items():
            term(stamp, stamp not in originals)["estimates"][parameter] = estimate
            bin_for(stamp)["filled"] += 1
        selected = [
            _record_point(row, not row.keep and row.quality != "missing", estimates.get(row.time))
            for row in (rows[index] for index in _point_indices(rows, estimates))
        ]
        synthetic = [
            {"time": stamp, "line": None, "value": None, "marked": False,
             "estimate": estimate, "reasons": ["В исходном файле нет записи за этот срок."]}
            for stamp, estimate in estimates.items() if stamp not in originals
        ]
        selected.extend(_sample(synthetic, POINT_BUDGET // 3))
        points[parameter] = sorted(selected, key=lambda item: item["time"])
        counts[parameter] = stat
        totals["marked"] += stat["marked"]
        totals["filled"] += stat["filled"]

    if settings["fill"] == "external" and not totals["filled"]:
        warnings.append("Для выбранных пропусков не получены сопоставимые значения из двух внешних источников.")
    if settings["fill"] == "model" and not totals["filled"]:
        warnings.append("Модель не нашла коротких пропусков с пригодными соседними сроками и достаточной выборкой.")
    if repeats:
        warnings.append("При повторе срока рабочий ряд использует первую пригодную исходную ячейку "
                        "по каждому параметру. Остальные значения сохранились в оригинале и листе повторов XLSX.")
    return {
        "params": parameters, "warnings": warnings, "stats": dict(totals, parameters=counts),
        "points": points, "timeline": [timeline[stamp] for stamp in sorted(timeline)], "timeline_days": bucket,
        "model": models, "external": external, "rows": [cooked[stamp] for stamp in sorted(cooked)],
        "changes": changes, "repeats": repeats,
    }


def _response(row, payload, summary):
    return {
        key: value for key, value in payload.items() if key not in {"rows", "changes", "repeats"}
    } | {"file": _record(row), "id": row["id"], "revision": summary["revision"], "settings": summary["settings"]}


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
    payload = _review(_unpack(row["source"]), json.loads(row["columns"]), row["filename"], settings, station,
                      checked_date(row["start"]), checked_date(row["end"]))
    created = _now()
    summary = {
        "revision": uuid4().hex, "settings": settings, "stats": payload["stats"], "created_at": created,
        "params": [param["id"] for param in payload["params"]],
    }
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


def confirm_file(query):
    revision = query.get("revision")
    if not revision:
        raise ValueError("Для подтверждения нужен актуальный предпросмотр.")
    with _database() as db:
        row = _lookup(db, query, ", draft")
    settings, _ = _settings(query, row["station"])
    record = _record(row)
    confirmed = record["confirmed"]
    if confirmed and revision == confirmed["revision"] and settings == confirmed["settings"] and not record["preview"]:
        return record
    draft = record["preview"]
    if not draft or draft["revision"] != revision or draft["settings"] != settings:
        raise ValueError("Настройки или версия изменились. Запустите фильтрацию заново.")
    payload = _unpack(row["draft"])
    next_version = row["version"] + 1
    confirmed = dict(draft, version=next_version, confirmed_at=_now())
    station = settings["station"]
    entries = [(row["id"], station, item["datetime"], item["synthetic"], _dumps(item)) for item in payload["rows"]]
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


def preview_excel(query):
    record, payload, _ = _saved(query)
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
        (row["datetime"], param, data["value"], data["source"], data["confidence"])
        for row in rows for param, data in row["estimates"].items()
    ]
    repeats = payload.get("repeats", [])
    workbook = BytesIO()
    with ZipFile(workbook, "w", ZIP_DEFLATED, compresslevel=4) as archive:
        names = ["Очищенные данные", "Исключённые ячейки", "Оценки"]
        if repeats:
            names.append("Повторы сроков")
        _write_package(archive, names)
        tables = [
            ([("datetime", 22), *[(param, 18) for param in parameters]], working_rows(), len(rows)),
            ([("Срок", 22), ("Строка источника", 18), ("Параметр", 18), ("Исходная ячейка", 23), ("Правила", 38)],
             ([(time, 0), (line, 0), (param, 0), (value, BAD), (", ".join(codes), 0)]
              for time, line, param, value, codes in changes), len(changes)),
            ([("Срок", 22), ("Параметр", 18), ("Оценка", 18), ("Источник", 48), ("Валидация", 62)],
             ([(time, 0), (param, 0), (value, ESTIMATED), (source, 0), (confidence, 0)]
              for time, param, value, source, confidence in estimates), len(estimates)),
        ]
        if repeats:
            tables.append((
                [("Срок", 22), ("Строка источника", 18), ("Параметр", 18), ("Исходная ячейка", 23)],
                ([(stamp, 0), (line, 0), (param, 0), (value, 0)] for stamp, line, param, value in repeats),
                len(repeats),
            ))
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


def _merge_terms(db, stamp, candidates, legacy):
    merged = {"datetime": stamp, "synthetic": True, "declared": [], "estimates": {}}
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
            if param in term:
                merged[param] = term[param]
            elif param in estimates:
                merged["estimates"][param] = dict(estimates[param], synthetic=False)
    merged["declared"] = [param for param in PARAMS if param in declared]
    return merged


def confirmed_rows(station_id, start, end):
    """Yield one row per time with the latest real declaration for each parameter."""
    first, last = _limits(start, end)
    with _database() as db:
        if db is None:
            return
        records = db.execute(
            "SELECT p.time, p.data, p.file_id FROM published p JOIN files f ON f.id=p.file_id "
            "WHERE p.station=? AND p.time BETWEEN ? AND ? "
            "ORDER BY p.time, p.synthetic, f.confirmed_order DESC",
            (station_id, first, last),
        )
        legacy = {}
        for stamp, candidates in groupby(records, lambda row: row["time"]):
            yield _merge_terms(db, stamp, candidates, legacy)


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
