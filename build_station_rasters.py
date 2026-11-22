"""Build two WGS84 WebP backgrounds from the inputs described in docs/КАРТА.md."""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw
import shapefile

from build_russia_map import geometry_parts
from shapely import make_valid
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parent
RUSSIA = (18, 40, 193, 84)
PIXELS_PER_DEGREE = 24


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
    with shapefile.Reader(str(countries)) as dataset:
        russian_shapes = [row.shape for row in dataset.iterShapeRecords(fields=["ADM0_A3"])
                          if row.record["ADM0_A3"] == "RUS"]
    if not russian_shapes:
        raise ValueError("В исходном векторном слое не найдена Россия")
    for russian_shape in russian_shapes:
        for polygon in geometry_parts(make_valid(shape(russian_shape.__geo_interface__)), "Polygon"):
            offset = 360 if polygon.bounds[2] < 0 else 0

            def plot(ring):
                return [((lon + offset - west) * scale, (north - lat) * scale)
                        for lon, lat in ring.coords]

            draw.polygon(plot(polygon.exterior), fill=255)
            for hole in polygon.interiors:
                draw.polygon(plot(hole), fill=0)

    background.putalpha(country_mask.resize((width, height), Image.Resampling.BOX))
    background.save(destination, quality=84, method=6)


def local_raster(source, destination):
    with Image.open(source) as raster:
        if raster.size != (1800, 1600) or raster.mode != "RGB":
            raise ValueError(f"Неожиданный формат регионального исходника: {raster.size}, {raster.mode}")
        # Stretch the dark reflectance mosaic for legibility without moving or blending pixels.
        display = raster.point([round(255 * (value / 255) ** .72) for value in range(256)] * 3)
        display.save(destination, quality=89, method=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--national-raster", required=True, type=Path)
    parser.add_argument("--countries", required=True, type=Path)
    parser.add_argument("--reserve-raster", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "web")
    args = parser.parse_args()
    clip_raster(args.national_raster, args.national_raster.with_suffix(".tfw"),
                args.countries, args.output / "russia-relief.webp")
    local_raster(args.reserve_raster, args.output / "reserve-imagery.webp")


if __name__ == "__main__":
    main()
