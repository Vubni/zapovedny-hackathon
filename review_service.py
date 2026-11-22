"""Coordinate a reversible station review and keep its paginated export in memory."""
import secrets
from collections import Counter, OrderedDict
from contextlib import closing
from datetime import date
from pathlib import PurePosixPath
from threading import Lock
from time import monotonic

from admin_auth import OPERATOR_REPORT_PREFIX
from analytics import STATIONS, database
from external_fill import MAX_DAYS, ZONES, apply_estimates, synthetic_row
from gap_fill import estimate_bounds, fill_settings, station_estimates
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


def _check_period(query, records):
    station = query.get("station", "kr1")
    if station not in STATIONS:
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
    if query["fill_external"]:
        if query.get("timezone") not in ZONES:
            raise ValueError("Выберите часовой пояс исходных сроков для сопоставления с внешними данными.")
        if (end - start).days >= MAX_DAYS:
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


def _prune(now):
    for token in [key for key, (expires, _, _) in _cache.items() if expires <= now]:
        del _cache[token]


def _save(report, original, saved_source=False):
    token = secrets.token_urlsafe(24)
    if saved_source:
        token = OPERATOR_REPORT_PREFIX + token
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
            raise ReportExpired("Расчёт больше не хранится в этой сессии. Нажмите «Отфильтровать» ещё раз.")
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


def _external_rows(report, query, ranges):
    lower, upper = estimate_bounds(report["param"], ranges)
    apply_estimates(report, dict(query, fill="1"))
    filled = set()
    for row in report["rows"]:
        value = row["estimate"]
        if value is None:
            continue
        if not lower <= value <= upper:
            row.update(estimate=None, estimate_sources="", estimate_status="unavailable",
                       estimate_reason="outside_parameter_range")
            continue
        row["estimate_method"] = "external"
        filled.add(row["time"])
    counts = Counter(row["estimate_status"] for row in report["rows"])
    report["external"].update(estimated_count=len(filled), unavailable_count=counts["unavailable"],
                              status="estimated" if filled else "unavailable")
    return filled


def _fill_report(report, query):
    originals = report["rows"]
    ranges = report["settings"].get("ranges")
    filled = _external_rows(report, query, ranges) if query["fill_external"] else set()
    if not query["fill_model"]:
        return
    estimates, report["model"] = station_estimates(originals, report["param"], excluded=filled, ranges=ranges)
    rows = report["rows"]
    by_time = {row["time"]: row for row in rows}
    if not query["fill_external"]:
        for row in rows:
            row.update(estimate=None, estimate_sources="", provenance=None, estimate_reason=None,
                       estimate_status="not_needed" if row["keep"] else "unavailable")
    for stamp, estimate in estimates.items():
        row = by_time.get(stamp)
        if row is None:
            row = synthetic_row(stamp, "Созданный срок на шаге исходных наблюдений.")
            rows.append(row)
        row.update(estimate=estimate["value"], estimate_sources=estimate["source"], estimate_method="model",
                   estimate_status="estimated", estimate_reason=None, provenance=estimate["provenance"])
    if estimates:
        rows.sort(key=lambda row: row["time"])


def create_report(query, body, content_type):
    query = dict(query)
    filling = fill_settings(query)
    query.update(filling)
    passport = None
    if "file_id" in query:
        from observation_store import review_source
        from station_catalog import catalog
        if body:
            raise ValueError("Выберите сохранённый оригинал или загрузите новый файл.")
        item, body, content_type = review_source(query["file_id"])
        if item["error"]:
            raise ValueError(item["error"])
        passport = next(station for station in catalog() if station["id"] == item["station"])
        available = {p["id"] for p in passport["params"]} & {p["id"] for p in item["params"]}
        if query.get("param", "t_air") not in available:
            raise ValueError("В файле или паспорте станции нет выбранного показателя.")
        query.update(station=passport["profile"], filename=item["filename"])
        if query["fill_external"]:
            if passport["location_precision"] == "unknown":
                raise ValueError("Для внешней оценки нужны координаты станции.")
            query.update(latitude=passport["latitude"], longitude=passport["longitude"])
    name = PurePosixPath(query.get("filename", "").replace("\\", "/")).name
    if not body and (name or query.get("source") == "file"):
        raise ValueError("Файл наблюдений пуст.")
    records = read_upload(body, name, content_type) if body else None
    query["source"] = name or ("Загруженный файл" if records else "Архив организаторов")
    _check_period(query, records)
    report = review(query, records, passport=passport)
    if passport:
        report.update(station=passport["id"], station_label=passport["label"])
    report.update(filling)
    if query["fill_external"] or query["fill_model"]:
        _fill_report(report, query)
    sheet = getattr(records[0], "sheet_member", None) if records else None
    original = (body, sheet, records[0].column_offset) if sheet else None
    report["original_workbook"] = original is not None
    return _page(report, _save(report, original, saved_source="file_id" in query), 0)


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
