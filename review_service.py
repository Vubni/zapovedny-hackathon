"""Coordinate a reversible station review and keep its paginated export in memory."""
import secrets
from collections import Counter, OrderedDict
from contextlib import closing
from datetime import date
from pathlib import PurePosixPath
from threading import Lock
from time import monotonic

from analytics import STATIONS, database
from external_fill import (
    MAX_DAYS, apply_estimates, external_reference, external_warnings, reference_inputs, station_location, synthetic_row, time_zone,
)
from gap_fill import estimate_bounds, fill_settings, station_estimates, verification_history, verify_estimates
from review_excel import export_xlsx
from review_filter import review
from review_upload import MAX_ROWS, read_upload

PAGE_SIZE = 100
LIFETIME = 1800
MAX_REPORTS = 6
MAX_CACHED_ROWS = 120000
_cache = OrderedDict()
_lock = Lock()


def _date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() == value:
            return result
    except (ValueError, TypeError):
        pass
    raise ValueError("Укажите календарную дату в формате ГГГГ-ММ-ДД.")


def _check_period(query, records, passport=None):
    station = query.get("station", "kr1")
    if station not in STATIONS and passport is None:
        raise ValueError("Неизвестная станция.")
    if records:
        days = [str(record.get("datetime", ""))[:10] for record in records]
        first = query.get("start") or min(days)
        last = query.get("end") or max(days)
    else:
        default_year = 2021 if station == "ame60" else 2024
        first = query.get("start") or f"{default_year}-04-01"
        last = query.get("end") or date.fromordinal(min(_date(first).toordinal() + 4, date.max.toordinal())).isoformat()
    start, end = _date(first), _date(last)
    if end < start:
        raise ValueError("Дата окончания раньше даты начала.")
    if records is not None and (end - start).days >= MAX_ROWS:
        raise ValueError("В книге может быть до 60 000 строк суточной сводки. Уточните даты наблюдений.")
    target_start, target_end = start, end
    if query["fill_external"] or query["fill_model"]:
        first_fill, last_fill = query.get("fill_start"), query.get("fill_end")
        if first_fill or last_fill:
            target_start, target_end = _date(first_fill), _date(last_fill)
            if not start <= target_start <= target_end <= end:
                raise ValueError("Выберите период заполнения в пределах дат исходных наблюдений.")
    if query["fill_external"]:
        time_zone(query.get("timezone"))
        if (target_end - target_start).days >= MAX_DAYS:
            raise ValueError(f"Восстановление из внешних источников доступно за период до {MAX_DAYS} суток.")
    if records is None:
        # raw_n counts source rows before exclusions and is the same for every parameter.
        with closing(database()) as db:
            count = db.execute("SELECT COALESCE(SUM(raw_n), 0) FROM daily "
                               "WHERE station=? AND param='t_air' AND day BETWEEN ? AND ?",
                               (station, first, last)).fetchone()[0]
        if count > MAX_ROWS:
            raise ValueError(f"В выбранном периоде {count:,} исходных строк. Для детальной выгрузки "
                             "выберите часть периода до 60 000 строк. В метеожурнале доступна сводка за весь период."
                             .replace(",", " "))
    query.update(start=first, end=last)
    return target_start, target_end


def _prune(now):
    for token in [key for key, (expires, _, _) in _cache.items() if expires <= now]:
        del _cache[token]


def _save(report, original):
    token = secrets.token_urlsafe(24)
    with _lock:
        now = monotonic()
        _prune(now)
        _cache[token] = (now + LIFETIME, report, original)
        while len(_cache) > 1 and (len(_cache) > MAX_REPORTS or
                sum(len(r["rows"]) for _, r, _ in _cache.values()) > MAX_CACHED_ROWS):
            _cache.popitem(last=False)
    return token


class ReportExpired(ValueError):
    pass


def _get(token):
    with _lock:
        now = monotonic()
        _prune(now)
        if token not in _cache:
            raise ReportExpired("Расчёт больше не хранится. Выполните расчёт ещё раз.")
        _, report, original = _cache[token]
        _cache[token] = (now + LIFETIME, report, original)
        _cache.move_to_end(token)
        return report, original


def _page(report, token, offset):
    try:
        offset = int(offset)
    except (ValueError, TypeError):
        raise ValueError("Некорректный номер первой строки.") from None
    rows = report["rows"]
    if offset < 0 or (offset and offset >= len(rows)):
        raise ValueError("Номер первой строки вне диапазона.")
    return dict(report, token=token, offset=offset, limit=PAGE_SIZE, total_rows=len(rows),
                rows=rows[offset:offset + PAGE_SIZE])


def _external_references(report, query, ranges, period, *, validate=False):
    bounds = estimate_bounds(report["param"], ranges)
    first, last = (day.isoformat() for day in period)
    selected = [row for row in report["rows"] if first <= row["time"][:10] <= last]
    original_by_time = {row["time"]: row for row in selected if row["keep"]}
    estimate_report = {"param": report["param"], "start": first, "end": last,
                       "rows": reference_inputs(selected, query.get("subhour", "skip"), validate)}
    apply_estimates(estimate_report, dict(query, fill="1"))
    references, observed_references = {}, {}
    for index, row in enumerate(estimate_report["rows"]):
        reference = external_reference(row, bounds)
        if row.get("calibration_sample"):
            if reference:
                observed_references[row["time"]] = reference
            estimate_report["rows"][index] = dict(original_by_time[row["time"]], estimate=None, estimate_sources="",
                                                   provenance=None, estimate_status="not_needed", estimate_reason=None)
            continue
        if reference:
            references[row["time"]] = reference
            if reference["value"] is not None and not reference["usable"]:
                row["estimate_reason"] = "outside_parameter_range"
        row.update(estimate=None, estimate_sources="", provenance=None, external_reference=reference,
                   estimate_status="not_needed" if row["keep"] else "unavailable")
    report["external"] = estimate_report["external"]
    outside = [row for row in report["rows"] if not first <= row["time"][:10] <= last]
    report["rows"] = sorted(estimate_report["rows"] + outside, key=lambda row: row["time"])
    counts = Counter(row["estimate_status"] for row in estimate_report["rows"])
    reasons = Counter(row["estimate_reason"] for row in estimate_report["rows"] if row["estimate_reason"])
    count = sum(reference["value"] is not None for reference in references.values())
    report["external"].update(estimated_count=0, reference_count=count, unavailable_count=counts["unavailable"] - count,
                              status="reference" if count else "unavailable", reason_counts=dict(reasons),
                              validation_reference_count=sum(item["usable"] for item in observed_references.values()))
    report["warnings"].extend(external_warnings(report["external"]))
    if query.get("location_precision") == "area":
        label = query.get("location_label", "район архива")
        reference = query.get("location_reference")
        detail = f" ({reference})" if reference else ""
        report["warnings"].append(f"Геопривязка внешних оценок приблизительная: {label}{detail}.")
    return references, observed_references


def _fill_report(report, query, period):
    originals = report["rows"]
    ranges = report["settings"].get("ranges")
    parameter = report["param"]
    estimates = {}
    if query["fill_model"]:
        estimates, report["model"] = station_estimates(originals, parameter, ranges=ranges, period=period)
    verifications = {}
    if query["fill_external"]:
        references, observed_refs = _external_references(report, query, ranges, period, validate=bool(estimates))
        history = verification_history(originals, observed_refs, parameter, ranges, query["timezone"])
        estimates, verifications = verify_estimates(estimates, references, parameter, history=history)
        report["external"]["validation_pair_count"] = len(history)
        report["external"]["adjusted_count"] = sum(item["method"] == "model_adjusted" for item in estimates.values())
    rows = report["rows"]
    by_time = {row["time"]: row for row in rows}
    if not query["fill_external"]:
        for row in rows:
            row.update(estimate=None, estimate_sources="", provenance=None, estimate_reason=None,
                       estimate_status="not_needed" if row["keep"] else "unavailable")
    for stamp, verification in verifications.items():
        if stamp in by_time:
            by_time[stamp]["verification"] = verification
    for stamp, estimate in estimates.items():
        row = by_time.get(stamp)
        if row is None:
            row = synthetic_row(stamp, "Созданный срок на шаге исходных наблюдений.")
            rows.append(row)
        row.update(estimate=estimate["value"], estimate_sources=estimate["source"], estimate_method=estimate["method"],
                   local_estimate=estimate.get("local_value", estimate["value"]), verification=verifications.get(stamp),
                   estimate_status="estimated", estimate_reason=None, provenance=estimate["provenance"])
    if estimates:
        rows.sort(key=lambda row: row["time"])


def create_report(query, body, content_type):
    query = dict(query)
    filling = fill_settings(query)
    query.update(filling)
    source_provenance = None
    if "file_id" in query:
        from observation_store import review_source
        if body:
            raise ValueError("Выберите сохранённый оригинал или загрузите новый файл.")
        item, body, content_type = review_source(query["file_id"])
        if item["error"]:
            raise ValueError(item["error"])
        if query.get("param", "t_air") not in {p["id"] for p in item["params"]}:
            raise ValueError("В файле нет выбранного показателя.")
        source_provenance = item.get("provenance")
        fixed_station = source_provenance and source_provenance.get("kind") == "observations"
        station = item["station"] if fixed_station else query.get("station") or item["station"]
        query.update(station=station, filename=item["filename"])
    passport = None
    station_id = query.get("station", "kr1")
    if "file_id" in query or query["fill_external"] or (body and station_id not in STATIONS):
        from station_catalog import catalog
        passport = next((station for station in catalog() if station["id"] == station_id), None)
        if passport is None:
            raise ValueError("Станция не найдена в каталоге.")
    if query["fill_external"]:
        query.update(station_location(passport))
    name = PurePosixPath(query.get("filename", "").replace("\\", "/")).name
    if not body and (name or query.get("source") == "file"):
        raise ValueError("Файл наблюдений пуст.")
    records = read_upload(body, name, content_type) if body else None
    query["source"] = name or ("Загруженный файл" if records else "Архив организаторов")
    period = _check_period(query, records, passport)
    report = review(query, records, passport=passport)
    if passport:
        report.update(station=passport["id"], station_label=passport["label"])
    report.update(filling)
    if source_provenance:
        report["source_provenance"] = source_provenance
    if query["fill_external"] or query["fill_model"]:
        requested = {part.strip() for part in (query.get("fill_params") or "").split(",") if part.strip()}
        if not requested or report["param"] in requested:
            _fill_report(report, query, period)
    sheet = getattr(records[0], "sheet_member", None) if records else None
    original = (body, sheet, records[0].column_offset) if sheet else None
    report["original_workbook"] = original is not None
    return _page(report, _save(report, original), 0)


def report_page(query):
    token = query.get("token", "")
    report, _ = _get(token)
    return _page(report, token, query.get("offset", 0))


def report_excel(query, *, annotated=False):
    report, original = _get(query.get("token", ""))
    filename = "review_{}_{}_{}_{}.xlsx".format(report["station"], report["param"], report["start"], report["end"])
    if annotated:
        if original is None:
            raise ValueError("Для копии исходной книги загрузите файл XLSX.")
        from review_workbook_copy import attach_original
        source, sheet, column_offset = original
        return "original_" + filename, attach_original(
            source, sheet, dict(report, column_offset=column_offset), export_xlsx(report))
    return filename, export_xlsx(report)
