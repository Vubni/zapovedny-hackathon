"""Validate offline vector coverage without rebuilding or using a network."""
import json
from pathlib import Path
import re
import unittest


WEB = Path(__file__).resolve().parents[1] / "web"
ASSET = WEB / "russia-map-data.js"
GEOMETRY = ("russiaLand", "russiaCoastlines", "russiaBorders", "russiaBoundaries",
            "russiaLakes", "russiaRivers", "russiaRoads")
# Windows around travel corridors spread across the whole country, including the Far East.
CORRIDORS = {
    "Калининград": (19, 54, 23, 56),
    "район заповедника": (31, 55, 35, 58),
    "Ростов": (39, 46, 42, 49),
    "Поволжье": (46, 51, 51, 57),
    "Урал": (57, 54, 63, 60),
    "Западная Сибирь": (82, 53, 91, 58),
    "Байкал": (103, 51, 112, 56),
    "Якутск": (127, 60, 132, 64),
    "Магадан": (147, 58, 155, 63),
    "Камчатка": (156, 51, 163, 57),
    "Приморье": (130, 42, 135, 48),
    "Сахалин": (140, 45, 145, 52),
}


def load_asset(asset):
    return {
        name: [json.loads(line.strip().removesuffix(",")) for line in rows.splitlines()]
        for name, rows in re.findall(r"^export const (\w+) = \[\n(.*?)^\];", asset.read_text(encoding="utf-8"), re.S | re.M)
    }


def visits(feature, bounds):
    west, south, east, north = bounds
    points = feature[4]
    return any(west <= lon <= east and south <= lat <= north
               for lon, lat in zip(points[::2], points[1::2]))


def land_at(rings, longitude, latitude):
    inside = False
    for west, south, east, north, points in rings:
        if not (west <= longitude <= east and south <= latitude <= north):
            continue
        vertices = list(zip(points[::2], points[1::2]))
        for (x0, y0), (x1, y1) in zip(vertices, vertices[1:] + vertices[:1]):
            if (y0 > latitude) != (y1 > latitude) and longitude < x0 + (latitude - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
    return inside


class OfflineRussiaMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_asset(ASSET)

    def test_packed_shapes_and_dateline(self):
        self.assertLess(ASSET.stat().st_size, 1_500_000)
        self.assertEqual(self.data["russiaExtent"], [[18, 40, 180, 84], [-180, 40, -167, 84]])
        for layer in GEOMETRY:
            with self.subTest(layer=layer):
                self.assertGreater(len(self.data[layer]), 5 if layer == "russiaBorders" else 20)
                for west, south, east, north, points, *scale in self.data[layer]:
                    lon, lat = points[::2], points[1::2]
                    self.assertEqual(len(lon), len(lat))
                    self.assertGreaterEqual(len(lon), 2)
                    self.assertEqual((west, south, east, north), (min(lon), min(lat), max(lon), max(lat)))
                    self.assertTrue(-180 <= west <= east <= 180)
                    self.assertTrue(40 <= south <= north <= 84)
                    self.assertTrue(all(abs(a - b) < 180 for a, b in zip(lon, lon[1:])))
                    self.assertTrue(not scale or (len(scale) == 1 and scale[0] >= 0))
        coast = self.data["russiaCoastlines"]
        self.assertTrue(any(visits(row, (-180, 63, -168, 72)) for row in coast))
        self.assertTrue(any(visits(row, (167, 63, 180, 72)) for row in coast))
        self.assertTrue(any(visits(row, (36, 79, 65, 83)) for row in coast))  # Земля Франца-Иосифа.

    def test_major_roads_and_boundaries_cover_distant_regions(self):
        roads = self.data["russiaRoads"]
        for name, window in CORRIDORS.items():
            with self.subTest(area=name):
                self.assertGreaterEqual(sum(visits(row, window) and row[5] <= 11 for row in roads), 2)
        areas = ("район заповедника", "Ростов", "Поволжье", "Урал",
                 "Западная Сибирь", "Байкал", "Магадан", "Приморье")
        for name in areas:
            with self.subTest(boundaries=name):
                self.assertTrue(any(visits(row, CORRIDORS[name]) for row in self.data["russiaBoundaries"]))
        self.assertTrue(any(visits(row, (162, 61, 165, 65)) for row in self.data["russiaBoundaries"]))

    def test_named_cities_and_water(self):
        cities = {row[0]: row[1:] for row in self.data["russiaPlaces"]}
        for name, (longitude, latitude) in {
            "Калининград": (20.50, 54.70), "Тверь": (35.89, 56.86),
            "Москва": (37.62, 55.75), "Волгоград": (44.51, 48.71),
            "Екатеринбург": (60.60, 56.85), "Новосибирск": (82.92, 55.03),
            "Якутск": (129.73, 62.03), "Хабаровск": (135.08, 48.48),
            "Петропавловск-Камчатский": (158.65, 53.04), "Анадырь": (177.47, 64.73),
            "Уэлен": (-169.81, 66.15),
        }.items():
            with self.subTest(city=name):
                lon, lat, zoom = cities[name]
                self.assertAlmostEqual(lon, longitude, delta=.12)
                self.assertAlmostEqual(lat, latitude, delta=.12)
                self.assertLessEqual(zoom, 18)
        self.assertGreater(len(cities), 550)
        water = {(row[0], row[4]): row[1:4] for row in self.data["russiaWaterLabels"]}
        for kind, names in {
            "river": ("Волга", "Обь", "Енисей", "Лена", "Колыма", "Амур"),
            "lake": ("Байкал", "Ладожское озеро", "Ханка", "Таймыр"),
            "sea": ("Балтийское море", "Баренцево море", "Чукотское море", "Охотское море"),
        }.items():
            for name in names:
                with self.subTest(water=name):
                    self.assertLessEqual(water[name, kind][2], 4)

    def test_foreign_cities_and_roads_are_absent(self):
        cities = {row[0] for row in self.data["russiaPlaces"]}
        for name, (lon, lat) in {
            "Хельсинки": (24.94, 60.17), "Рига": (24.11, 56.96), "Минск": (27.57, 53.90),
            "Киев": (30.52, 50.45), "Астана": (71.43, 51.13), "Улан-Батор": (106.91, 47.92),
            "Харбин": (126.63, 45.75), "Ном": (-165.41, 64.50),
        }.items():
            with self.subTest(city=name):
                around_city = (lon - .25, lat - .25, lon + .25, lat + .25)
                self.assertNotIn(name, cities)
                self.assertFalse(land_at(self.data["russiaLand"], lon, lat))
                self.assertFalse(any(visits(row, around_city) for row in self.data["russiaRoads"]))
        for lon, lat in [(20.50, 54.70), (32.97029, 56.46431), (169, 67), (-173.3, 65.4)]:
            with self.subTest(russian_land=(lon, lat)):
                self.assertTrue(land_at(self.data["russiaLand"], lon, lat))

    def test_local_overlay_stays_in_satellite_crop(self):
        asset = WEB / "station-map-data.js"
        self.assertLess(asset.stat().st_size, 30_000)
        local = load_asset(asset)
        west, south, east, north = (32.1, 56.1, 33.75, 56.95)
        for name in ("land", "lakes", "rivers", "roads"):
            for left, bottom, right, top, _ in local[name]:
                self.assertTrue(west <= left <= right <= east)
                self.assertTrue(south <= bottom <= top <= north)
        self.assertIn("Посёлок Заповедный", {row[0] for row in local["places"]})
        self.assertTrue(all(west <= lon <= east and south <= lat <= north
                            for _, lon, lat, _ in local["places"]))


if __name__ == "__main__":
    unittest.main()
