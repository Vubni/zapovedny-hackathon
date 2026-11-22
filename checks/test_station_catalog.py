import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from station_catalog import catalog, register_station


class StationTerrainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "stations.sqlite3"
        config = patch.dict(os.environ, METEO_OBSERVATION_DB=str(self.database))
        config.start()
        self.addCleanup(config.stop)

    def test_archive_terrain_and_persisted_registration(self):
        archive = {station["id"]: station for station in catalog()}
        self.assertEqual({key: item["terrain"] for key, item in archive.items()},
                         {"kr1": "clearing", "kr2": "field", "ame60": "unknown"})
        self.assertEqual(len({item["color"] for item in archive.values()}), 3)
        self.assertEqual({item["location_precision"] for item in archive.values()}, {"area"})

        station = register_station({"id": "test-forest", "label": "Лесной кордон", "terrain": "forest",
                                    "latitude": 56.31, "longitude": 32.94})["station"]
        self.assertEqual(station["terrain"], "forest")
        self.assertEqual(station["location_precision"], "exact")
        self.assertEqual(next(item for item in catalog() if item["id"] == station["id"]), station)

    def test_default_legacy_and_invalid_terrain(self):
        for terrain in ("from-label", ["forest"], None):
            with self.assertRaisesRegex(ValueError, "тип местности"):
                register_station({"label": "Тестовый пост", "terrain": terrain})
        self.assertFalse(self.database.exists())
        station = register_station({"id": "legacy", "label": "Лесная"})["station"]
        self.assertEqual(station.pop("terrain"), "unknown")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE registered_stations SET passport = ? WHERE id = ?",
                               (json.dumps(station, ensure_ascii=False), station["id"]))
        old = next(item for item in catalog() if item["id"] == station["id"])
        self.assertEqual(old["terrain"], "unknown")
        self.assertEqual(old["location_precision"], "unknown")
