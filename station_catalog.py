"""Station passports for the observation workspace and its interactive map."""
import json
import math
import re
import uuid

from analytics import PARAMS, metadata
from build_data import STATIONS
from observations_db import connection


COLORS = ("#2b7650", "#426583", "#735579", "#945320", "#507f76", "#854f64")
ARCHIVE_COLORS = dict(kr1=COLORS[0], kr2=COLORS[1], ame60=COLORS[2])
ARCHIVE_TERRAIN = {"kr1": "clearing", "kr2": "field"}
TERRAIN_TYPES = {"clearing", "field", "forest", "wetland", "settlement", "other", "unknown"}
AREA = (56.464310, 32.970290)


def parameter_options(params=PARAMS):
    return [dict(id=key, label=PARAMS[key][0], unit=PARAMS[key][1]) for key in params]


def catalog():
    stations = metadata()["stations"]
    for item in stations:
        key = item["id"]
        passport = STATIONS[key]
        item.update(profile=key, archive=True, color=ARCHIVE_COLORS[key],
                    terrain=ARCHIVE_TERRAIN.get(key, "unknown"),
                    minimum_slots=passport.minimum_slots, slot_minutes=passport.slot_minutes,
                    latitude=AREA[0], longitude=AREA[1], location_precision="area",
                    location_label="Красное", location_reference="Точка сбора 26.07.2019")
    with connection() as db:
        if db is None:
            return stations
        if db.table_exists("registered_stations"):
            order = "ordinal" if db.is_postgres else "rowid"
            stations.extend(json.loads(body) for (body,) in
                            db.execute(f"SELECT passport FROM registered_stations ORDER BY {order}"))

    from observation_store import all_date_bounds
    published = all_date_bounds()
    for item in stations:
        item.setdefault("terrain", "unknown")
        bounds = published.get(item["id"])
        if bounds:
            item["start"] = min(item["start"] or bounds[0], bounds[0])
            item["end"] = max(item["end"] or bounds[1], bounds[1])
    return stations


def station_list():
    stations = catalog()
    return {"stations": stations, "param_options": parameter_options(),
            "start": min(item["start"] for item in stations if item["start"]),
            "end": max(item["end"] for item in stations if item["end"])}


def coordinate(value, limit):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if math.isfinite(number) and -limit <= number <= limit:
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    raise ValueError("Проверьте широту (−90…90) и долготу (−180…180).")


def register_station(payload):
    label = payload.get("label")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100 or not label.isprintable():
        raise ValueError("Введите название станции длиной до 100 символов.")
    label = label.strip()
    profile = payload.get("profile")
    if "profile" in payload and (not isinstance(profile, str) or profile not in STATIONS):
        raise ValueError("Неизвестный архивный профиль прибора.")
    defaults = STATIONS[profile or "kr1"]
    terrain = payload.get("terrain", "unknown")
    if not isinstance(terrain, str) or terrain not in TERRAIN_TYPES:
        raise ValueError("Выберите тип местности из списка.")
    supported = defaults.params if profile else PARAMS
    params = payload.get("params", list(defaults.params))
    if (not isinstance(params, list) or not params or
            any(not isinstance(p, str) or p not in supported for p in params)):
        raise ValueError("Выберите хотя бы один поддерживаемый показатель станции.")
    key = payload.get("id") or ("station-" + uuid.uuid4().hex[:12])
    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,55}", key):
        raise ValueError("Код станции может содержать латинские строчные буквы, цифры, дефис и подчёркивание.")
    lat, lon = payload.get("latitude"), payload.get("longitude")
    lat = None if lat == "" else lat
    lon = None if lon == "" else lon
    if (lat is None) != (lon is None):
        raise ValueError("Введите обе координаты или оставьте оба поля пустыми.")
    if lat is not None:
        lat, lon = coordinate(lat, 90), coordinate(lon, 180)
    step = payload.get("slot_minutes", defaults.slot_minutes)
    minimum = payload.get("minimum_slots", defaults.minimum_slots)
    if type(step) is not int or step not in (1, 60) or type(minimum) is not int or not 1 <= minimum <= 1440 // step:
        raise ValueError("Проверьте единицу учёта покрытия и суточный порог в паспорте станции.")
    if key in STATIONS or label.casefold() in {s["label"].casefold() for s in metadata()["stations"]}:
        raise ValueError("Станция с таким кодом или названием уже есть.")
    with connection(write=True) as db:
        count, = db.execute("SELECT COUNT(*) FROM registered_stations").fetchone()
        item = dict(id=key, label=label, terrain=terrain, minimum_slots=minimum, slot_minutes=step,
                    color=COLORS[(len(STATIONS) + count) % len(COLORS)], latitude=lat, longitude=lon,
                    location_precision="exact" if lat is not None else "unknown", archive=False,
                    start=None, end=None, params=parameter_options(dict.fromkeys(params)))
        if profile:
            item["profile"] = profile
        inserted = db.execute("INSERT INTO registered_stations (id,name_key,passport) VALUES (?,?,?) "
                              "ON CONFLICT DO NOTHING", (key, label.casefold(), json.dumps(item, ensure_ascii=False)))
        if inserted.rowcount != 1:
            raise ValueError("Станция с таким кодом или названием уже есть.")
    return {"station": item}
