"""Add hourly reanalysis estimates to a working copy of the archive."""

import json
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http.client import HTTPException
from math import isfinite
from pathlib import Path
from statistics import fmean
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_DAYS = 31
TIMEOUT = 6
FIXTURES = Path(__file__).resolve().parent / "results" / "review" / "external"
SNAPSHOT_START = date(2024, 4, 1)
SNAPSHOT_END = date(2024, 4, 30)
ARCHIVE_POINT = (56.52, 32.87)
HOUR = timedelta(hours=1)
HALF_HOUR = timedelta(minutes=30)
DAY = timedelta(days=1)
UTC = timezone.utc
ZONES = ("UTC", "Europe/Moscow")
UNITS = {"t_air": "°C", "rh": "%", "pressure_hpa": "hPa"}
RANGES = {"t_air": (-90, 65), "rh": (0, 100), "pressure_hpa": (300, 1100)}


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    model: str
    description: str
    endpoint: str
    variables: dict
    snapshot: str
    retrieved_at: str

    def url(self, start, end, point):
        latitude, longitude = point
        fields = ",".join(field for field, _ in self.variables.values())
        if self.id == "era5":
            params = dict(latitude=latitude, longitude=longitude, start_date=start,
                          end_date=end, hourly=fields, timezone="GMT", models="era5")
        else:
            params = {"parameters": fields, "community": "SB",
                      "longitude": longitude, "latitude": latitude, "start": start.strftime("%Y%m%d"),
                      "end": end.strftime("%Y%m%d"), "format": "JSON", "time-standard": "UTC"}
        return self.endpoint + "?" + urlencode(params)


MODELS = {
    "era5": Model("era5", "Open-Meteo", "ERA5", "ERA5, мгновенные снимки на границах часа",
                  "https://archive-api.open-meteo.com/v1/archive",
                  {"t_air": ("temperature_2m", "°C"), "rh": ("relative_humidity_2m", "%"),
                   "pressure_hpa": ("surface_pressure", "hPa")},
                  "era5-april-2024.json", "2026-11-22T10:07:03+00:00"),
    "merra2": Model("merra2", "NASA POWER", "MERRA-2", "MERRA-2, среднее за час",
                    "https://power.larc.nasa.gov/api/temporal/hourly/point",
                    {"t_air": ("T2M", "C"), "rh": ("RH2M", "%"), "pressure_hpa": ("PS", "kPa")},
                    "merra2-april-2024.json", "2026-11-22T10:13:38+00:00"),
}
SNAPSHOT_START_UTC = SNAPSHOT_START - DAY
SNAPSHOT_END_UTC = SNAPSHOT_END + DAY


@dataclass(frozen=True)
class ModelData:
    metadata: dict
    values: dict


_CACHE = OrderedDict()
_CACHE_LOCK = Lock()
_FETCHERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reanalysis")


def _request_point(query):
    if "latitude" not in query and "longitude" not in query:
        return ARCHIVE_POINT
    latitude, longitude = query.get("latitude"), query.get("longitude")
    if (type(latitude) not in (int, float) or type(longitude) not in (int, float)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        raise ValueError("Для внешних оценок нужны числовые широта от −90 до 90 и долгота от −180 до 180")
    return float(latitude), float(longitude)


def _coordinates(point):
    latitude, longitude = point
    return {"latitude": latitude, "longitude": longitude}


def _metadata(model, start, end, snapshot, point):
    return {"id": model.id, "name": model.name, "model": model.model,
            "description": model.description, "kind": "reanalysis", "url": model.url(start, end, point),
            "mode": "snapshot" if snapshot else "network", "start_utc": start.isoformat(),
            "end_utc": end.isoformat(), "timezone": "UTC", "variable_units": dict(model.variables.values()),
            "requested_coordinates": _coordinates(point),
            "retrieved_at": model.retrieved_at if snapshot else None}


def _number(value, param, unit, fill=None):
    if type(value) not in (int, float) or not isfinite(value) or value == fill:
        return None
    normalized = value * 10 if unit == "kPa" else value
    lower, upper = RANGES[param]
    return float(normalized) if lower <= normalized <= upper else None


def _parse_model(model, body, start, end, point):
    """Keep raw provider numbers keyed by the validated UTC hour."""
    if model.id == "era5":
        if body.get("utc_offset_seconds") != 0 or body.get("timezone") not in {"GMT", "UTC"}:
            raise ValueError("Ожидалась временная шкала UTC")
        coordinates = dict(latitude=body["latitude"], longitude=body["longitude"], elevation_m=body["elevation"])
        times = body["hourly"]["time"]
        variables = body["hourly"]
        units = body["hourly_units"]
        timestamp_format = "%Y-%m-%dT%H:%M"
        fill = None
    else:
        header = body["header"]
        if header.get("time_standard") != "UTC" or set(header.get("sources", ())) != {"MERRA2", "POWER"}:
            raise ValueError("Ожидался ответ MERRA-2 во временной шкале UTC")
        longitude, latitude, elevation = body["geometry"]["coordinates"]
        coordinates = dict(latitude=latitude, longitude=longitude, elevation_m=elevation)
        variables = body["properties"]["parameter"]
        units = {code: details["units"] for code, details in body["parameters"].items()}
        timestamp_format = "%Y%m%d%H"
        fill = header["fill_value"]

    if not all(type(n) in (int, float) and isfinite(n) for n in coordinates.values()):
        raise ValueError("В ответе отсутствуют координаты или высота")
    latitude, longitude = point
    longitude_distance = abs((coordinates["longitude"] - longitude + 180) % 360 - 180)
    if abs(coordinates["latitude"] - latitude) > 1 or longitude_distance > 1:
        raise ValueError("Координаты в ответе выходят за ожидаемую область")
    values = {}
    for param, (field, unit) in model.variables.items():
        if units.get(field) != unit or field not in variables:
            continue
        if model.id == "era5":
            if len(variables[field]) != len(times):
                continue
            samples = zip(times, variables[field])
        else:
            samples = variables[field].items()
        hours = {}
        seen = set()
        for stamp, raw in samples:
            dt = datetime.strptime(stamp, timestamp_format).replace(tzinfo=UTC)
            if dt.strftime(timestamp_format) != stamp or dt.minute or not start <= dt.date() <= end:
                continue
            normalized = _number(raw, param, unit, fill)
            if dt in seen:
                hours.pop(dt, None)
            elif normalized is not None:
                hours[dt] = (raw, normalized)
            seen.add(dt)
        values[param] = hours
    if not any(values.values()):
        raise ValueError("В ответе нет пригодных часовых данных в ожидаемых единицах")
    return coordinates, values


def _load_model(model, start, end, snapshot, point):
    info = _metadata(model, start, end, snapshot, point)
    try:
        if snapshot:
            body = json.loads((FIXTURES / model.snapshot).read_bytes())
        else:
            with urlopen(info["url"], timeout=TIMEOUT) as response:
                data = response.read(512 * 1024 + 1)
            if len(data) > 512 * 1024:
                raise ValueError("Ответ превышает 512 КиБ")
            body = json.loads(data)
            info["retrieved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        coordinates, values = _parse_model(model, body, start, end, point)
    except HTTPError as exc:
        return ModelData(dict(info, status="unavailable", error=f"HTTP {exc.code}"), {})
    except (OSError, HTTPException):
        return ModelData(dict(info, status="unavailable", error="Ответ источника недоступен"), {})
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return ModelData(dict(info, status="unavailable", error="Несовместимый формат или пустые данные"), {})
    return ModelData(dict(info, status="available", coordinates=coordinates), values)


def _cached_model(model, start, end, snapshot, point):
    key = (model.id, start, end, snapshot, point)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and entry[0] > monotonic():
            _CACHE.move_to_end(key)
            return entry[1]
    data = _load_model(model, start, end, snapshot, point)
    ttl = 6 * 3600 if data.metadata["status"] == "available" else 60
    with _CACHE_LOCK:
        _CACHE[key] = (monotonic() + ttl, data)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 48:
            _CACHE.popitem(last=False)
    return data


def _source_hours(model, stamp):
    start = stamp.replace(minute=0, second=0, microsecond=0)
    if model.id == "era5":
        return (start, start + HOUR)
    if stamp == start or stamp == start + HALF_HOUR:
        return (start,)
    if stamp < start + HALF_HOUR:
        return (start - HOUR, start)
    return (start, start + HOUR)


def _load_sources(ids, stamps, snapshot, point):
    requests = []
    for model_id in ids:
        model = MODELS[model_id]
        if snapshot:
            start, end = SNAPSHOT_START_UTC, SNAPSHOT_END_UTC
        else:
            days = {hour.date() for stamp in stamps for hour in _source_hours(model, stamp)}
            start, end = min(days), max(days)
        future = _FETCHERS.submit(_cached_model, model, start, end, snapshot, point)
        requests.append((model, start, end, future))
    done, _ = wait([request[3] for request in requests], timeout=TIMEOUT)
    results = []
    for model, start, end, future in requests:
        if future in done:
            results.append(future.result())
        else:
            future.cancel()
            info = _metadata(model, start, end, snapshot, point)
            results.append(ModelData(dict(info, status="unavailable", error="Истекло время ожидания"), {}))
    return results


def _to_utc(local, zone):
    """Do not choose one of two repeated local times or invent a skipped hour."""
    try:
        first = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = local.replace(tzinfo=zone, fold=1).astimezone(UTC)
        if first == second and first.astimezone(zone).replace(tzinfo=None) == local:
            return first
    except OverflowError:
        pass
    return None


def _sample(source, param, stamp):
    info = source.metadata
    model = MODELS[info["id"]]
    field, unit = model.variables[param]
    source_hours = _source_hours(model, stamp)
    timeline = source.values.get(param, {})
    if any(hour not in timeline for hour in source_hours):
        return None
    samples = [{"time_utc": hour.isoformat(), "value": timeline[hour][0]} for hour in source_hours]
    values = [timeline[hour][1] for hour in source_hours]
    if stamp.minute:
        # NASA labels an hour mean by its start; use its midpoint for point interpolation.
        anchors = [hour if model.id == "era5" else hour + HALF_HOUR for hour in source_hours]
        for sample, anchor in zip(samples, anchors):
            sample["anchor_utc"] = anchor.isoformat()
        if len(anchors) == 1:
            value = values[0]
        else:
            fraction = (stamp - anchors[0]) / (anchors[1] - anchors[0])
            value = values[0] + fraction * (values[1] - values[0])
    else:
        value = fmean(values)
    return {"id": model.id, "name": model.name, "model": model.model, "url": info["url"],
            "coordinates": dict(info["coordinates"]),
            "retrieved_at": info["retrieved_at"], "variable": field, "source_unit": unit,
            "description": model.description, "unit": UNITS[param], "samples": samples,
            "value": value}


def synthetic_row(stamp, reason="Созданный часовой срок без исходной записи."):
    return {"time": stamp, "line": None, "source": None, "original": {}, "value": None,
            "keep": False, "official_keep": False, "synthetic": True, "conflict": False,
            "reasons": [reason], "reason_codes": ["created_hour"],
            "official_status": "missing", "official_reasons": []}


def _synthetic_rows(rows, start, end, zone):
    occupied = {row["time"][:13] for row in rows}
    first = datetime.combine(start, time.min)
    for offset in range(((end - start).days + 1) * 24):
        dt = first + offset * HOUR
        stamp = dt.isoformat(sep=" ", timespec="minutes")
        if stamp[:13] not in occupied and _to_utc(dt, zone) is not None:
            yield synthetic_row(stamp)


def _eligible_terms(rows, zone, now, subhour):
    times = Counter(row["time"] for row in rows)
    candidates = []
    last_complete_hour = datetime.combine(now.date(), time.min, tzinfo=UTC)
    local_today = now.astimezone(zone).date()
    for row in rows:
        if row["keep"] and row["value"] is not None:
            row["estimate_status"] = "not_needed"
            continue
        local = datetime.fromisoformat(row["time"])
        stamp = _to_utc(local, zone)
        if row.get("conflict") or times[row["time"]] > 1:
            reason = "duplicate_term"
        elif (local.minute and subhour == "skip") or local.second or local.microsecond:
            reason = "subhourly"
        elif stamp is None:
            reason = "ambiguous_local_time"
        elif local.date() >= local_today or stamp >= last_complete_hour:
            reason = "incomplete_day"
        else:
            candidates.append((row, stamp))
            continue
        row["estimate_reason"] = reason
    return candidates


def _apply_estimate(row, param, stamp, sources, point):
    available = [sample for source in sources if (sample := _sample(source, param, stamp)) is not None]
    count = len(available)
    provenance = {"source_count": count, "unit": UNITS[param], "sources": available,
                  "requested_coordinates": _coordinates(point),
                  "pressure_reference": "surface" if param == "pressure_hpa" else None}
    if stamp.minute:
        provenance.update(kind="subhour_reanalysis", time_utc=stamp.isoformat(),
                          method="arithmetic_mean_of_linear_model_interpolations")
        detail = "интерполяция к минуте"
    else:
        provenance.update(kind="hourly_reanalysis", start_utc=stamp.isoformat(), end_utc=(stamp + HOUR).isoformat(),
                          method="arithmetic_mean_of_model_hour_means")
        detail = "среднее за час"
    row["provenance"] = provenance
    if count >= 2:
        row["estimate"] = round(fmean(sample["value"] for sample in available), 2)
        row["estimate_status"] = "estimated"
        row["estimate_sources"] = " + ".join(sample["model"] for sample in available) + " · " + detail
    else:
        row["estimate_reason"] = "fewer_than_two_sources"


def apply_estimates(report, query):
    """Attach model estimates and provenance without changing observation decisions."""
    if query.get("fill") != "1":
        return report
    zone_name = query.get("timezone")
    if zone_name not in ZONES:
        raise ValueError("Для внешних оценок явно выберите часовой пояс архива: UTC или Europe/Moscow")
    try:
        zone = ZoneInfo(zone_name) if zone_name != "UTC" else UTC
    except ZoneInfoNotFoundError:
        raise ValueError("Часовой пояс Europe/Moscow отсутствует в системной базе часовых поясов") from None
    start, end = date.fromisoformat(report["start"]), date.fromisoformat(report["end"])
    if not 1 <= (end - start).days + 1 <= MAX_DAYS:
        raise ValueError(f"Внешние оценки доступны для периода от 1 до {MAX_DAYS} суток")
    subhour = query.get("subhour", "skip")
    if subhour not in {"skip", "interpolate"}:
        raise ValueError("Выберите поддерживаемый режим для минутных сроков")
    requested = query.get("sources", ",".join(MODELS)).split(",")
    ids = tuple(dict.fromkeys(requested))
    if any(model_id not in MODELS for model_id in ids):
        raise ValueError("Неизвестный источник внешних оценок")
    point = _request_point(query)

    rows = [dict(row, synthetic=False) for row in report["rows"] if not row.get("synthetic")]
    rows.extend(_synthetic_rows(rows, start, end, zone))
    rows.sort(key=lambda row: row["time"])
    for row in rows:
        row.update(estimate=None, estimate_sources="", provenance=None, estimate_status="unavailable", estimate_reason=None)
    candidates = _eligible_terms(rows, zone, datetime.now(UTC), subhour)
    param = report["param"]
    sources = []
    if param not in UNITS:
        reason = "unsupported_parameter"
    elif param == "pressure_hpa" and query.get("pressure_reference") != "surface":
        reason = "unknown_pressure_reference"
    else:
        reason = None
        if candidates:
            stamps = {stamp for _, stamp in candidates}
            snapshot = point == ARCHIVE_POINT and SNAPSHOT_START <= start <= end <= SNAPSHOT_END
            sources = _load_sources(ids, stamps, snapshot, point)

    for row, stamp in candidates:
        if reason:
            row["estimate_reason"] = reason
        else:
            _apply_estimate(row, param, stamp, sources, point)
    counts = Counter(row["estimate_status"] for row in rows)
    source_counts = Counter(sample["id"] for row in rows for sample in (row["provenance"] or {}).get("sources", []))
    report["rows"] = rows
    report["external"] = {"status": "estimated" if counts["estimated"] else "unavailable",
                          "timezone": zone_name, "parameter": param, "unit": UNITS.get(param), "reason": reason,
                          "requested_coordinates": _coordinates(point),
                          "min_sources": 2, "requested_sources": list(ids), "grid_minutes": 60, "subhour": subhour,
                          "estimated_count": counts["estimated"], "unavailable_count": counts["unavailable"],
                          "synthetic_count": sum(row["synthetic"] for row in rows),
                          "sources": [dict(s.metadata, count=source_counts[s.metadata["id"]]) for s in sources]}
    return report
