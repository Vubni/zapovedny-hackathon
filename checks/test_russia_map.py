"""Validate offline vector coverage without rebuilding or using a network."""
import json
from pathlib import Path
import re
import unittest


ASSET = Path(__file__).resolve().parents[1] / "web" / "russia-map-data.js"
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


def load_asset():
    return {
        name: [json.loads(line.strip().removesuffix(",")) for line in rows.splitlines()]
        for name, rows in re.findall(r"export const (\w+) = \[\n(.*?)\n\];", ASSET.read_text(encoding="utf-8"), re.S)
    }


def visits(feature, bounds):
    west, south, east, north = bounds
    points = feature[4]
    return any(west <= lon <= east and south <= lat <= north
               for lon, lat in zip(points[::2], points[1::2]))


class OfflineRussiaMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_asset()

    def test_packed_shapes_and_dateline(self):
        self.assertLess(ASSET.stat().st_size, 2_200_000)
        for layer in GEOMETRY:
            with self.subTest(layer=layer):
                self.assertGreater(len(self.data[layer]), 20)
                for west, south, east, north, points, *scale in self.data[layer]:
                    lon, lat = points[::2], points[1::2]
                    self.assertEqual(len(lon), len(lat))
                    self.assertGreaterEqual(len(lon), 2)
                    self.assertEqual((west, south, east, north), (min(lon), min(lat), max(lon), max(lat)))
                    self.assertTrue(-180 <= west <= east <= 180)
                    self.assertTrue(35 <= south <= north <= 85)
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
                 "Западная Сибирь", "Байкал", "Магадан", "Камчатка", "Приморье")
        for name in areas:
            with self.subTest(boundaries=name):
                self.assertTrue(any(visits(row, CORRIDORS[name]) for row in self.data["russiaBoundaries"]))

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
        self.assertGreater(len(cities), 1000)
        water = {(row[0], row[4]): row[1:4] for row in self.data["russiaWaterLabels"]}
        for kind, names in {
            "river": ("Волга", "Обь", "Енисей", "Лена", "Колыма", "Амур"),
            "lake": ("Байкал", "Ладожское озеро", "Ханка", "Таймыр"),
            "sea": ("Балтийское море", "Баренцево море", "Чукотское море", "Охотское море"),
        }.items():
            for name in names:
                with self.subTest(water=name):
                    self.assertLessEqual(water[name, kind][2], 4)


if __name__ == "__main__":
    unittest.main()
