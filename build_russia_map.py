"""Generate the offline Russia overview from unpacked Natural Earth 1:10m layers.

Usage: python3 build_russia_map.py /path/to/unpacked/source
Build dependencies: pyshp 2.3.1 and Shapely 2.1.2. The server serves the result as-is.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from shapefile import Reader
from shapely import line_merge, make_valid, prepare, union_all
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box, shape


EXTENTS = [(18, 40, 180, 84), (-180, 40, -167, 84)]
WINDOWS = tuple(box(*region) for region in EXTENTS)
RESERVE_EXTENT = (32.10, 56.10, 33.75, 56.95)
ROOT = Path(__file__).resolve().parent

# These cities must appear early enough to anchor the country view, including the Far East.
ANCHORS = set("""
Москва|Санкт-Петербург|Калининград|Мурманск|Архангельск|Ростов-на-Дону|Астрахань|
Казань|Нижний Новгород|Волгоград|Уфа|Екатеринбург|Омск|Новосибирск|Красноярск|
Иркутск|Оренбург|Норильск|Якутск|Анадырь|Магадан|Петропавловск-Камчатский|
Хабаровск|Южно-Сахалинск|Владивосток
""".replace("\n", "").split("|"))
KEY_RIVERS = {"Волга", "Дон", "Печора", "Северная Двина", "Урал", "Обь", "Иртыш",
              "Енисей", "Ангара", "Лена", "Алдан", "Яна", "Индигирка", "Колыма", "Амур"}
KEY_LAKES = {"Байкал", "Ладожское озеро", "Онежское озеро", "Ханка", "Таймыр",
             "Чудское озеро", "Ильмень"}
KEY_WATERS = {"Баренцево море", "Белое море", "Карское море", "Море Лаптевых",
              "Восточно-Сибирское море", "Чукотское море", "Берингово море", "Охотское море",
              "Японское море", "Балтийское море", "Чёрное море", "Каспийское море"}


def source_rows(source, layer, fields):
    with Reader(str(source / f"ne_10m_{layer}")) as dataset:
        for row in dataset.iterShapeRecords(fields=fields):
            if not row.shape.points:
                continue
            if row.shape.shapeType == 1:
                longitude, latitude = row.shape.points[0]
                bounds = (longitude, latitude, longitude, latitude)
            else:
                bounds = row.shape.bbox
            if any(intersects(bounds, extent) for extent in EXTENTS):
                yield row


def intersects(left, right):
    return not (left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3])


def geometry_parts(geometry, kind):
    if geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from geometry_parts(part, kind)


def clipped_pieces(geometry, kind):
    if kind == "Polygon" and not geometry.is_valid:
        geometry = make_valid(geometry)
    for window in WINDOWS:
        if intersects(geometry.bounds, window.bounds):
            yield from geometry_parts(geometry.intersection(window), kind)


def label(name, point, scale, kind=None):
    row = [name, round(point.x, 4), round(point.y, 4), scale]
    return row + [kind] if kind else row


def packed(points, scale=None, precision=3):
    rounded = []
    for x, y in points:
        point = (round(x, precision), round(y, precision))
        if not rounded or point != rounded[-1]:
            rounded.append(point)
    points = rounded
    if len(points) < 2:
        return None
    longitude, latitude = zip(*points)
    row = [min(longitude), min(latitude), max(longitude), max(latitude),
           [coordinate for point in points for coordinate in point]]
    return row + [scale] if scale is not None else row


def line_pieces(points):
    start = 0
    for index in range(1, len(points)):
        if abs(points[index][0] - points[index - 1][0]) > 180:
            if index - start > 1:
                yield LineString(points[start:index])
            start = index
    if len(points) - start > 1:
        yield LineString(points[start:])


def source_lines(source, layer, fields, area=None):
    for row in source_rows(source, layer, fields):
        stops = list(row.shape.parts[1:]) + [len(row.shape.points)]
        for start, stop in zip(row.shape.parts, stops):
            for line in line_pieces(row.shape.points[start:stop]):
                for part in regional_pieces(line, "LineString", area):
                    yield part, row.record


def regional_pieces(geometry, kind, area):
    for part in clipped_pieces(geometry, kind):
        if area is None or area.covers(part):
            yield part
        elif area.intersects(part):
            yield from geometry_parts(part.intersection(area), kind)


def lines(source, layer, fields, style, tolerance, area=None):
    ranks = defaultdict(list)
    for line, record in source_lines(source, layer, fields, area):
        scale = style(record)
        if scale is not None:
            ranks[scale].append(line)
    rows = []
    for scale, features in sorted(ranks.items()):
        for line in geometry_parts(line_merge(MultiLineString(features)), "LineString"):
            for part in regional_pieces(line.simplify(tolerance), "LineString", area):
                item = packed(part.coords, scale)
                if item:
                    rows.append(item)
    return rows


def land(country):
    result = []
    for polygon in clipped_pieces(country, "Polygon"):
        if polygon.area < .001:
            continue
        polygon = polygon.simplify(.008, preserve_topology=True)
        west, south, east, north = polygon.bounds
        for x in range(int(west // 12) * 12, int(east // 12) * 12 + 1, 12):
            for y in range(int(south // 12) * 12, int(north // 12) * 12 + 1, 12):
                tile = box(x, y, x + 12, y + 12)
                for part in geometry_parts(polygon.intersection(tile), "Polygon"):
                    for ring in (part.exterior, *part.interiors):
                        item = packed(ring.coords)
                        if item and len(item[4]) >= 6:
                            result.append(item)
    return result


def lakes(source, area):
    result, candidates = [], {}
    for row in source_rows(source, "lakes", ["scalerank", "name_ru"]):
        name, rank = row.record["name_ru"], row.record["scalerank"]
        scale = 2 if name in KEY_LAKES or rank <= 2 else 6 if rank <= 4 else 15
        geometry = shape(row.shape.__geo_interface__).simplify(.008, preserve_topology=True)
        for polygon in regional_pieces(geometry, "Polygon", area):
            if polygon.area < .0005:
                continue
            for ring in (polygon.exterior, *polygon.interiors):
                item = packed(ring.coords, scale)
                if item and len(item[4]) >= 6:
                    result.append(item)
            if name and (name not in candidates or polygon.area > candidates[name][0]):
                candidates[name] = polygon.area, label(name, polygon.representative_point(), scale + 1, "lake")
    return result, [entry for _, entry in candidates.values()]


def rivers(source, area):
    result, candidates = [], {}
    main_names = set()
    for layer in ("rivers_lake_centerlines", "rivers_europe"):
        fields = ["featurecla", "name", "name_ru", "scalerank"]
        for line, record in source_lines(source, layer, fields, area):
            if record["featurecla"] == "Lake Centerline":
                continue
            name, identity = record["name_ru"], record["name"] or record["name_ru"]
            if layer == "rivers_lake_centerlines" and identity:
                main_names.add(identity.casefold())
            if layer == "rivers_europe" and identity and identity.casefold() in main_names:
                continue
            rank = record["scalerank"]
            scale = 2 if name in KEY_RIVERS or rank <= 3 else 7 if rank <= 6 else 15 if rank <= 9 else 32
            for part in regional_pieces(line.simplify(.012), "LineString", area):
                item = packed(part.coords, scale)
                if item:
                    result.append(item)
                if name and (name not in candidates or part.length > candidates[name][0]):
                    candidates[name] = part.length, label(name, part.interpolate(.5, normalized=True),
                                                           scale + 2, "river")
    return result, [entry for _, entry in candidates.values()]


def seas(source, waters):
    candidates = {}
    for row in source_rows(source, "geography_marine_polys", ["name_ru", "featurecla", "scalerank"]):
        name, kind, rank = row.record["name_ru"], row.record["featurecla"], row.record["scalerank"]
        if not name or kind == "ocean" or (rank > 5 and name not in KEY_WATERS):
            continue
        for polygon in regional_pieces(shape(row.shape.__geo_interface__), "Polygon", waters):
            if name not in candidates or polygon.area > candidates[name][0]:
                scale = 2 if name in KEY_WATERS else 9
                candidates[name] = polygon.area, label(name, polygon.representative_point(), scale, "sea")
    return [entry for _, entry in candidates.values()]


def places(source, country):
    ranked = []
    fields = ["NAME", "NAME_RU", "ADM0_A3", "SCALERANK", "POP_MAX", "FEATURECLA"]
    for row in source_rows(source, "populated_places", fields):
        record = row.record
        lon, lat = row.shape.points[0]
        if record["ADM0_A3"] != "RUS" or not country.covers(Point(lon, lat)):
            continue
        name = record["NAME_RU"] or record["NAME"]
        rank, feature, population = record["SCALERANK"], record["FEATURECLA"], record["POP_MAX"]
        if name in ANCHORS:
            scale = 2
        elif feature == "Admin-0 capital" or population >= 500000:
            scale = 5
        elif feature == "Admin-1 capital" or population >= 150000:
            scale = 9
        else:
            scale = 18 if rank <= 6 or population >= 35000 else 42
        ranked.append([scale, -population, [name, round(lon, 4), round(lat, 4), scale]])
    return [item for _, _, item in sorted(ranked)]


def road_scale(record):
    if record["type"] == "Ferry Route":
        return None
    zoom = record["min_zoom"]
    if zoom <= 3:
        return 3
    if zoom <= 4:
        return 6
    if zoom <= 5:
        return 11
    return 26 if zoom <= 7.1 else 48


def russia_country(countries):
    with Reader(str(countries)) as dataset:
        polygons = [make_valid(shape(row.shape.__geo_interface__))
                    for row in dataset.iterShapeRecords(fields=["ADM0_A3"])
                    if row.record["ADM0_A3"] == "RUS"]
    if not polygons:
        raise ValueError("В слое Natural Earth не найдена территория России")
    country = union_all(polygons)
    prepare(country)
    return country


def common_border(record):
    return 0 if "RUS" in (record["ADM0_A3_L"], record["ADM0_A3_R"]) else None


def generate(source):
    country = russia_country(source / "ne_10m_admin_0_countries.shp")
    # The separate coastline and border datasets can differ from the country mask by small amounts.
    edge = country.buffer(.02, quad_segs=3)
    waters = country.simplify(.04, preserve_topology=True).buffer(2, quad_segs=6).difference(country)
    prepare(edge)
    prepare(waters)
    packed_lakes, lake_names = lakes(source, country)
    packed_rivers, river_names = rivers(source, country)
    return {
        "russiaExtent": EXTENTS,
        "russiaLand": land(country),
        "russiaCoastlines": lines(source, "coastline", ["scalerank"], lambda row: 0, .012, edge),
        "russiaBorders": lines(source, "admin_0_boundary_lines_land", ["ADM0_A3_L", "ADM0_A3_R"],
                               common_border, .012, edge),
        "russiaBoundaries": lines(source, "admin_1_states_provinces_lines", ["ADM0_A3"],
                                lambda row: 3 if row["ADM0_A3"] == "RUS" else None, .018, country),
        "russiaLakes": packed_lakes,
        "russiaRivers": packed_rivers,
        "russiaRoads": lines(source, "roads", ["type", "min_zoom"], road_scale, .018, country),
        "russiaPlaces": places(source, country),
        "russiaWaterLabels": sorted(lake_names + river_names + seas(source, waters),
                                    key=lambda row: (row[3], row[4], row[0])),
    }


def clip_local_overlay(path, country):
    reserve = box(*RESERVE_EXTENT)
    if not country.covers(reserve):
        raise ValueError("Обзорное окно заповедника выходит за географическую маску")
    text = path.read_text(encoding="utf-8")
    source = {name: [json.loads(row.strip().removesuffix(",")) for row in body.splitlines()]
              for name, body in re.findall(r"^export const (\w+) = \[\n(.*?)^\];", text, re.S | re.M)}
    result = {}
    for name in ("land", "lakes", "rivers", "roads"):
        rows = []
        closed = name in {"land", "lakes"}
        for west, south, east, north, coordinates in source[name]:
            if not intersects((west, south, east, north), RESERVE_EXTENT):
                continue
            points = tuple(zip(coordinates[::2], coordinates[1::2]))
            if closed:
                parts = geometry_parts(make_valid(Polygon(points)).intersection(reserve), "Polygon")
                features = (ring.coords for part in parts for ring in (part.exterior, *part.interiors))
            else:
                parts = geometry_parts(LineString(points).intersection(reserve), "LineString")
                features = (part.coords for part in parts)
            for feature in features:
                row = packed(feature, precision=5)
                if row and (not closed or len(row[4]) >= 6):
                    rows.append(row)
        result[name] = rows
    result["places"] = [row for row in source["places"] if reserve.covers(Point(row[1], row[2]))]
    return result


def write_module(layers, output, *, local=False):
    with output.open("w", encoding="utf-8") as module:
        module.write("// Generated by build_russia_map.py from Natural Earth (public domain).\n")
        module.write("// https://www.naturalearthdata.com/about/terms-of-use/\n")
        if local:
            module.write("// Local settlements: GeoNames (CC BY 4.0), clipped to the reserve imagery extent.\n")
            module.write("// https://www.geonames.org/export/ · https://creativecommons.org/licenses/by/4.0/\n")
        module.write("// Shape: [west, south, east, north, [lon, lat, ...], optional minScale].\n")
        module.write("// Label: [name, lon, lat, minScale, optional kind].\n")
        for name, rows in layers.items():
            module.write(f"\nexport const {name} = [\n")
            for row in rows:
                module.write(f"  {json.dumps(row, ensure_ascii=False, separators=(',', ':'))},\n")
            module.write("];\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "web" / "russia-map-data.js")
    parser.add_argument("--clip-local-overlay", type=Path, help="Clip the existing reserve overlay module in place")
    args = parser.parse_args()
    layers = generate(args.source)
    write_module(layers, args.output)
    if args.clip_local_overlay:
        country = russia_country(args.source / "ne_10m_admin_0_countries.shp")
        overlay = clip_local_overlay(args.clip_local_overlay, country)
        write_module(overlay, args.clip_local_overlay, local=True)
    print(", ".join(f"{name}: {len(rows)}" for name, rows in layers.items()))


if __name__ == "__main__":
    main()
