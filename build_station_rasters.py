"""Build offline WGS84 map layers from the inputs described in docs/КАРТА.md."""
import argparse
import json
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageDraw
from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon, box

from build_russia_map import geometry_parts, packed, russia_country


ROOT = Path(__file__).resolve().parent
RUSSIA = (18, 40, 193, 84)
PIXELS_PER_DEGREE = 24
DETAIL_BOUNDS = (32.900, 56.415, 33.045, 56.512)
DETAIL_SIZE = (1740, 1164)
LOCAL_ROADS = {"residential", "unclassified", "service", "tertiary", "secondary", "primary", "trunk"}
LOCAL_PATHS = {"track", "path", "footway", "cycleway", "bridleway", "pedestrian"}


def clip_raster(source, world_file, countries, destination):
    pixel_x, rotation_y, rotation_x, pixel_y, center_x, center_y = map(
        float, world_file.read_text().split()
    )
    if pixel_x <= 0 or pixel_y >= 0 or rotation_x or rotation_y:
        raise ValueError("Ожидался североориентированный растр WGS84 без поворота")
    left_edge = center_x - pixel_x / 2
    top_edge = center_y - pixel_y / 2
    west, south, east, north = RUSSIA
    width = (east - west) * PIXELS_PER_DEGREE
    height = (north - south) * PIXELS_PER_DEGREE
    background = Image.new("RGB", (width, height))

    # Natural Earth starts at −180°. Reassemble the small strip east of 180° from its west edge.
    Image.MAX_IMAGE_PIXELS = 200_000_000
    with Image.open(source) as raster:
        raster = raster.convert("RGB")
        for left, right, offset in ((west, 180, 0), (180, east, -360)):
            image = raster.transform(
                ((right - left) * PIXELS_PER_DEGREE, height),
                Image.Transform.AFFINE,
                (1 / (PIXELS_PER_DEGREE * pixel_x), 0, (left + offset - left_edge) / pixel_x,
                 0, -1 / (PIXELS_PER_DEGREE * pixel_y), (north - top_edge) / pixel_y),
                Image.Resampling.BICUBIC,
            )
            background.paste(image, ((left - west) * PIXELS_PER_DEGREE, 0))

    scale = 2 * PIXELS_PER_DEGREE
    country_mask = Image.new("L", (width * 2, height * 2))
    draw = ImageDraw.Draw(country_mask)
    for polygon in geometry_parts(russia_country(countries), "Polygon"):
        offset = 360 if polygon.bounds[2] < 0 else 0

        def plot(ring):
            return [((lon + offset - west) * scale, (north - lat) * scale)
                    for lon, lat in ring.coords]

        draw.polygon(plot(polygon.exterior), fill=255)
        for hole in polygon.interiors:
            draw.polygon(plot(hole), fill=0)

    background.putalpha(country_mask.resize((width, height), Image.Resampling.BOX))
    background.save(destination, quality=84, method=6)


def local_raster(source, destination, size=(1800, 1600), fade=0):
    with Image.open(source) as raster:
        if raster.size != size or raster.mode != "RGB":
            raise ValueError(f"Неожиданный формат регионального исходника: {raster.size}, {raster.mode}")
        # Stretch the dark reflectance mosaic for legibility without moving or blending pixels.
        display = raster.point([round(255 * (value / 255) ** .72) for value in range(256)] * 3)
        if fade:
            width, height = size
            horizontal = Image.new("L", (width, 1))
            horizontal.putdata([min(255, round(255 * min(x, width - 1 - x) / fade)) for x in range(width)])
            vertical = Image.new("L", (1, height))
            vertical.putdata([min(255, round(255 * min(y, height - 1 - y) / fade)) for y in range(height)])
            display.putalpha(ImageChops.darker(horizontal.resize(size), vertical.resize(size)))
        display.save(destination, quality=89, method=6)


def osm_tags(element):
    return {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}


def reserve_vectors(source, destination):
    data = ElementTree.parse(source).getroot()
    region = box(*DETAIL_BOUNDS)
    nodes = {node.attrib["id"]: (float(node.attrib["lon"]), float(node.attrib["lat"]))
             for node in data.findall("node")}
    layers = {"roads": [], "paths": [], "buildings": [], "waterways": [], "labels": []}
    names = {}

    def add_label(name, point, scale, kind, weight):
        key = (name, kind)
        if name and (key not in names or weight > names[key][0]):
            names[key] = weight, [name, round(point.x, 6), round(point.y, 6), scale, kind]

    for node in data.findall("node"):
        tags = osm_tags(node)
        point = Point(nodes[node.attrib["id"]])
        if not region.covers(point):
            continue
        name = tags.get("name:ru", tags.get("name"))
        if tags.get("place") in {"hamlet", "village", "locality", "isolated_dwelling"}:
            add_label(name, point, 2600, "place", 1)
        elif tags.get("tourism") == "museum":
            add_label(name, point, 4150, "place", 1)

    for way in data.findall("way"):
        tags = osm_tags(way)
        references = [nd.attrib["ref"] for nd in way.findall("nd")]
        if len(references) < 2 or any(reference not in nodes for reference in references):
            continue
        coordinates = [nodes[reference] for reference in references]
        if tags.get("building") not in (None, "no") and len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
            footprint = make_valid(Polygon(coordinates)).intersection(region)
            for polygon in geometry_parts(footprint, "Polygon"):
                for ring in (polygon.exterior, *polygon.interiors):
                    shape = packed(ring.coords, 3900, precision=6)
                    if shape and len(shape[4]) >= 8:
                        layers["buildings"].append(shape)
            continue
        highway = tags.get("highway")
        if highway in LOCAL_ROADS:
            layer, scale, kind = "roads", 2600, "road"
        elif highway in LOCAL_PATHS:
            layer, scale, kind = "paths", 2900, "road"
        elif tags.get("waterway") in {"river", "stream"}:
            layer, scale, kind = "waterways", 2600, "river"
        else:
            continue
        for line in geometry_parts(LineString(coordinates).intersection(region), "LineString"):
            shape = packed(line.coords, scale, precision=6)
            if shape:
                layers[layer].append(shape)
            name = tags.get("name:ru", tags.get("name"))
            add_label(name, line.interpolate(.5, normalized=True), 3700, kind, line.length)

    layers["labels"] = [row for _, row in sorted(names.values(), key=lambda item: (item[1][3], item[1][0]))]
    with destination.open("w", encoding="utf-8") as output:
        output.write("// © OpenStreetMap contributors. Derived database under ODbL 1.0.\n"
                     "// https://www.openstreetmap.org/copyright · https://opendatacommons.org/licenses/odbl/1-0/\n"
                     "// Source: /api/0.6/map?bbox=32.900,56.415,33.045,56.512\n"
                     "export const reserveObjects = {\n")
        for name, rows in layers.items():
            output.write(f"  {name}: [\n")
            for row in rows:
                output.write(f"    {json.dumps(row, ensure_ascii=False, separators=(',', ':'))},\n")
            output.write("  ],\n")
        output.write("};\n")
    print(", ".join(f"{layer}: {len(rows)}" for layer, rows in layers.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--national-raster", type=Path)
    parser.add_argument("--countries", type=Path)
    parser.add_argument("--reserve-raster", type=Path)
    parser.add_argument("--detail-raster", type=Path)
    parser.add_argument("--reserve-osm", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "web")
    args = parser.parse_args()
    if bool(args.national_raster) != bool(args.countries):
        parser.error("--national-raster и --countries используются вместе")
    if not any((args.national_raster, args.reserve_raster, args.detail_raster, args.reserve_osm)):
        parser.error("Укажите хотя бы один исходный слой")
    if args.national_raster:
        clip_raster(args.national_raster, args.national_raster.with_suffix(".tfw"),
                    args.countries, args.output / "russia-relief.webp")
    if args.reserve_raster:
        local_raster(args.reserve_raster, args.output / "reserve-imagery.webp")
    if args.detail_raster:
        local_raster(args.detail_raster, args.output / "reserve-detail.webp", DETAIL_SIZE, fade=36)
    if args.reserve_osm:
        reserve_vectors(args.reserve_osm, args.output / "reserve-detail-osm.js")


if __name__ == "__main__":
    main()
