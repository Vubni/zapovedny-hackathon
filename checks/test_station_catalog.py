import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from analytics import PARAMS
from import_station import preview_import
from observations_db import connection
from station_catalog import COLORS, catalog, register_station, station_list


class StationCatalogContract:
    def test_archive_terrain_and_persisted_registration(self):
        archive = {station["id"]: station for station in catalog()}
        self.assertEqual({key: item["terrain"] for key, item in archive.items()},
                         {"kr1": "clearing", "kr2": "field", "ame60": "clearing"})
        self.assertEqual(len({item["color"] for item in archive.values()}), 3)
        self.assertEqual({item["location_precision"] for item in archive.values()}, {"area"})
        self.assertEqual({key: (item["latitude"], item["longitude"]) for key, item in archive.items()},
                         {"kr1": (56.464310, 32.970290), "kr2": (56.463800, 32.970370),
                          "ame60": (56.464310, 32.970290)})
        self.assertEqual(archive["kr2"]["location_label"], "Дорога на Красное")
        self.assertIn("1992", archive["ame60"]["location_reference"])

        station = register_station({"id": "test-forest", "label": "Лесной кордон", "terrain": "forest",
                                    "latitude": 56.31, "longitude": 32.94})["station"]
        self.assertEqual(station["terrain"], "forest")
        self.assertEqual(station["location_precision"], "exact")
        self.assertNotIn("profile", station)
        self.assertEqual(next(item for item in catalog() if item["id"] == station["id"]), station)

    def test_default_legacy_and_invalid_terrain(self):
        for terrain in ("from-label", ["forest"], None):
            with self.assertRaisesRegex(ValueError, "тип местности"):
                register_station({"label": "Тестовый пост", "terrain": terrain})
        station = register_station({"id": "legacy", "label": "Лесная"})["station"]
        self.assertEqual(station.pop("terrain"), "unknown")
        with connection(write=True) as db:
            db.execute("UPDATE registered_stations SET passport=? WHERE id=?",
                       (json.dumps(station, ensure_ascii=False), station["id"]))
        old = next(item for item in catalog() if item["id"] == station["id"])
        self.assertEqual(old["terrain"], "unknown")
        self.assertEqual(old["location_precision"], "unknown")
        self.assertIsNone(old["latitude"])
        self.assertIsNone(old["longitude"])

    def test_parameter_options_and_profile_free_station_preview(self):
        self.assertEqual([option["id"] for option in station_list()["param_options"]], list(PARAMS))
        payload = {"id": "mixed-post", "label": "Пост с давлением и почвой", "latitude": 0,
                   "longitude": -71.123456789, "params": ["pressure_hpa", "soil_10", "soil_10"],
                   "minimum_slots": 1}
        for changes in ({"params": []}, {"params": ["unsupported"]}):
            with self.assertRaisesRegex(ValueError, "поддерживаемый показатель"):
                register_station(dict(payload, **changes))
        with self.assertRaisesRegex(ValueError, "обе координаты"):
            register_station(dict(payload, longitude=None))

        station = register_station(payload)["station"]
        self.assertNotIn("profile", station)
        self.assertEqual((station["latitude"], station["longitude"], station["location_precision"]),
                         (0, -71.123456789, "exact"))
        self.assertEqual([option["id"] for option in station["params"]], ["pressure_hpa", "soil_10"])
        raw = b"datetime,qc_flag,pressure_hpa,soil_10\n2025-01-01 00:00,,1011.2,4.5\n"
        query = {"station": station["id"], "param": "soil_10", "year": "2025"}
        preview = preview_import(raw, "csv", query)
        self.assertEqual((preview["expected_slots"], preview["minimum_slots"]), (24, 1))
        self.assertEqual(preview["rows"][0]["mean"], 4.5)
        with self.assertRaisesRegex(ValueError, "не измеряет"):
            preview_import(raw, "csv", dict(query, param="t_air"))

        legacy = register_station({"id": "legacy-ame", "label": "Старый паспорт АМЕ", "profile": "ame60"})["station"]
        self.assertEqual((legacy["profile"], legacy["slot_minutes"], legacy["minimum_slots"]), ("ame60", 1, 72))
        legacy_preview = preview_import(b"datetime,qc_flag,soil_10\n2025-01-01 00:00,,4.5\n", "csv",
                                        dict(query, station=legacy["id"]))
        self.assertEqual((legacy_preview["expected_slots"], legacy_preview["minimum_slots"]), (96, 72))

    def test_concurrent_id_and_casefolded_label_uniqueness(self):
        entries = [
            {"id": "same-id", "label": "Код первый"},
            {"id": "same-id", "label": "Код второй"},
            {"id": "unicode-one", "label": "Straße"},
            {"id": "unicode-two", "label": "  STRASSE  "},
        ]
        barrier = Barrier(len(entries), timeout=15)

        def try_register(entry):
            barrier.wait()
            try:
                return register_station(entry)["station"], None
            except ValueError as error:
                return None, str(error)

        with ThreadPoolExecutor(max_workers=len(entries)) as executor:
            outcomes = list(executor.map(try_register, entries))
        records = [station for station, _ in outcomes if station is not None]
        errors = [error for _, error in outcomes if error is not None]
        self.assertEqual(len(records), 2)
        self.assertEqual(errors, ["Станция с таким кодом или названием уже есть."] * 2)
        self.assertEqual(sum(item["id"] == "same-id" for item in records), 1)
        self.assertEqual(sum(item["label"].casefold() == "strasse" for item in records), 1)
        self.assertCountEqual([item for item in catalog() if not item["archive"]], records)

    def test_concurrent_registrations_keep_catalog_order_and_colors(self):
        entries = [{"id": f"test-{i}", "label": f"Пост {i}"} for i in range(6)]
        barrier = Barrier(len(entries), timeout=15)

        def register(entry):
            barrier.wait()
            return register_station(entry)["station"]

        with ThreadPoolExecutor(max_workers=len(entries)) as executor:
            records = list(executor.map(register, entries))
        catalog_records = [item for item in catalog() if not item["archive"]]
        self.assertCountEqual(catalog_records, records)
        self.assertEqual([item["color"] for item in catalog_records], [*COLORS[3:], *COLORS[:3]])

    def test_registered_station_dates_come_from_published_observations(self):
        station = register_station({"id": "new-records", "label": "Пост с наблюдениями"})["station"]
        first = "2026-02-10T00:00:00"
        last = "2026-02-12T00:00:00"
        with connection(write=True) as db:
            db.execute("INSERT INTO files (id,filename,mime,original,columns,rows,station,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       ("file", "test.csv", "text/csv", b"", "[]", 2, station["id"], first, first))
            db.executemany("INSERT INTO published (file_id,station,time,synthetic,data) VALUES (?,?,?,?,?)",
                           [("file", station["id"], stamp, 0, "{}") for stamp in (first, last)])
        result = station_list()
        found = next(item for item in result["stations"] if item["id"] == station["id"])
        self.assertEqual((found["start"], found["end"]), ("2026-02-10", "2026-02-12"))
        self.assertEqual(result["end"], "2026-02-12")


class SQLiteStationCatalogTests(StationCatalogContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "stations.sqlite3"
        config = patch.dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(self.database))
        config.start()
        self.addCleanup(config.stop)

    def test_reads_original_station_only_sqlite_file(self):
        with self.assertRaisesRegex(ValueError, "тип местности"):
            register_station({"label": "Тестовый пост", "terrain": "no-such-type"})
        self.assertFalse(self.database.exists())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE registered_stations "
                       "(id TEXT PRIMARY KEY,name_key TEXT UNIQUE NOT NULL,passport TEXT NOT NULL)")
            for key in ("older-z", "older-a"):
                passport = {"id": key, "label": key, "archive": False, "start": None, "end": None}
                db.execute("INSERT INTO registered_stations VALUES (?,?,?)",
                           (key, key, json.dumps(passport)))
        read = [item for item in catalog() if not item["archive"]]
        self.assertEqual([(item["id"], item["terrain"]) for item in read],
                         [("older-z", "unknown"), ("older-a", "unknown")])
        register_station({"id": "recent", "label": "Новый пост"})
        self.assertEqual([item["id"] for item in catalog() if not item["archive"]],
                         ["older-z", "older-a", "recent"])


@unittest.skipUnless(os.environ.get("METEO_TEST_POSTGRES_DSN"), "Set METEO_TEST_POSTGRES_DSN for PostgreSQL tests")
class PostgreSQLStationCatalogTests(StationCatalogContract, unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        database = os.environ["METEO_TEST_POSTGRES_DSN"]
        name = f"meteo_catalog_test_{uuid4().hex}"
        schema = sql.Identifier(name)
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(schema))

        def drop_schema():
            with psycopg.connect(database, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))

        self.addCleanup(drop_schema)
        isolated = make_conninfo(database, options=f"-csearch_path={name}")
        config = patch.dict(os.environ, METEO_DATABASE_URL=isolated)
        config.start()
        self.addCleanup(config.stop)

    def test_writes_the_configured_postgres_schema(self):
        register_station({"id": "pg-only", "label": "Пост PostgreSQL"})
        with connection() as db:
            self.assertTrue(db.is_postgres)
            row = db.execute("SELECT id, ordinal FROM registered_stations WHERE id=?", ("pg-only",)).fetchone()
            self.assertEqual(tuple(row), ("pg-only", 1))
