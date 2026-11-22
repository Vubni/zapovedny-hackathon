"""Verify center boundaries for domain operations on both observation backends."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import observation_store as store
from observations import chart_data, coverage_overview
from observations_db import (
    CENTRAL_CENTER, _postgres_namespace, center_scope, connection, current_center, local_db_path, uses_archive,
)
from recent_observations import latest_five
from station_catalog import register_station, station_list


DAY = "2026-05-17"
OTHER = "north-reserve"
THIRD = "south-reserve"
QUERY = {"stations": "shared", "start": DAY, "end": DAY, "chart": "air", "grain": "day"}
SELECTION = {"station": "shared", "param": "t_air", "start": DAY, "end": DAY}
PG = os.environ.get("METEO_TEST_POSTGRES_DSN")


class CenterIsolationContract:
    def publish(self, station, value):
        register_station({"id": station, "label": "Полевой пост", "minimum_slots": 1})
        csv = f"datetime,t_air\n{DAY} 00:15,{value}\n".encode()
        file = store.upload_file({"station": station, "filename": "test.csv"}, csv, "text/csv")
        settings = {"id": file["id"], "station": station}
        draft = store.preview_file(settings)
        store.confirm_file(dict(settings, revision=draft["revision"]))
        return file

    def test_centers_have_private_files_stations_results_and_dates(self):
        self.assertEqual(current_center(), CENTRAL_CENTER)
        central = self.publish("shared", 8.125)
        with connection(write=True, global_store=True) as db:
            db.execute("CREATE TABLE global_scope_probe (name TEXT PRIMARY KEY)")
            db.execute("INSERT INTO global_scope_probe (name) VALUES (?)", ("global",))

        with center_scope(OTHER):
            empty = station_list()
            self.assertEqual(empty["stations"], [])
            self.assertEqual((empty["start"], empty["end"]), (None, None))
            self.assertEqual(store.list_files()["files"], [])
            with connection() as db:
                self.assertIsNone(db)
            direct_archive = dict(QUERY, stations="kr1")
            with self.assertRaises(ValueError):
                chart_data(direct_archive)
            with self.assertRaises(ValueError):
                coverage_overview(direct_archive)
            with self.assertRaises(ValueError):
                latest_five({"station": "kr1", "param": "t_air"})
            with self.assertRaises(ValueError):
                store.from_observations(dict(SELECTION, station="kr1"))
            with self.assertRaises(ValueError):
                register_station({"label": "Зарезервированный код", "id": "kr1"})
            for lookup in (store.file_record, store.original_file, store.get_file, store.preview_file):
                with self.assertRaises(ValueError):
                    lookup(central)
            with self.assertRaises(ValueError):
                store.review_source(central["id"])

            other = self.publish("shared", 13.125)
            self.assertEqual(station_list()["start"], DAY)
            with patch("analytics.database", side_effect=AssertionError("Tenant read the central archive")):
                self.assertEqual(chart_data(QUERY)["series"][0]["points"][-1]["y"], 13.125)
                self.assertEqual(coverage_overview(QUERY)["rows"][0]["valid_days"], 1)
                self.assertEqual(latest_five(SELECTION)["rows"][-1]["mean"], 13.125)
                selection = store.from_observations(SELECTION)
            self.assertNotEqual(selection["id"], central["id"])
            self.assertEqual(selection["provenance"]["sources"], {"confirmed": 1})
            with connection() as db:
                self.assertFalse(db.table_exists("global_scope_probe"))
            with connection(global_store=True) as db:
                self.assertEqual(db.execute("SELECT name FROM global_scope_probe").fetchone()[0], "global")

        with center_scope(THIRD):
            self.assertFalse(station_list()["stations"])
            self.assertFalse(store.list_files()["files"])
            with self.assertRaises(ValueError):
                store.file_record(other)
        self.assertEqual(chart_data(QUERY)["series"][0]["points"][-1]["y"], 8.125)
        self.assertEqual([file["id"] for file in store.list_files()["files"]], [central["id"]])
        with self.assertRaises(ValueError):
            store.file_record(other)

    def test_public_archive_does_not_read_private_imports_and_disallows_writes(self):
        private = self.publish("private-site", 5)
        day = "2025-12-31"
        query = {"stations": "kr1", "start": day, "end": day, "chart": "air", "grain": "day"}
        public = chart_data(query)
        archive = store.from_observations({"station": "kr1", "param": "t_air", "start": day, "end": day})
        correction = store.upload_file({"station": "kr1", "filename": "prepared.csv"},
                                       f"datetime,t_air\n{day} 00:00,44\n".encode(), "text/csv")
        request = {"id": correction["id"], "station": "kr1"}
        draft = store.preview_file(request)
        store.confirm_file(dict(request, revision=draft["revision"]))
        self.assertNotEqual(chart_data(query)["series"], public["series"])
        with center_scope(CENTRAL_CENTER, public_only=True):
            self.assertTrue(uses_archive())
            self.assertEqual({item["id"] for item in station_list()["stations"]}, {"kr1", "kr2", "ame60"})
            self.assertEqual(chart_data(query), public)
            self.assertEqual(store.list_files()["files"], [])
            with self.assertRaises(ValueError):
                store.original_file(private)
            with self.assertRaises(ValueError):
                store.original_file(archive)
            with self.assertRaises(PermissionError):
                register_station({"id": "guest-site", "label": "Гостевая запись"})
            with connection(global_store=True) as db:
                self.assertTrue(db.table_exists("files"))
        self.assertEqual(store.file_record(private)["station"], "private-site")

    def test_contexts_restore_on_errors_and_remain_thread_local(self):
        def scoped(center_id):
            with center_scope(center_id):
                self.assertEqual(current_center(), center_id)
                with self.assertRaises(RuntimeError):
                    with center_scope(CENTRAL_CENTER, public_only=True):
                        raise RuntimeError("restore")
                self.assertEqual(current_center(), center_id)
                return current_center()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(scoped, (OTHER, THIRD))), [OTHER, THIRD])
        self.assertEqual(current_center(), CENTRAL_CENTER)
        with self.assertRaises(ValueError):
            with center_scope("../central-forest"):
                pass


class SQLiteCenterIsolationTests(CenterIsolationContract, unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="meteo-center-tests-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name) / "observations.sqlite3"
        environment = patch.dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(self.base))
        environment.start()
        self.addCleanup(environment.stop)

    def test_backend_uses_distinct_local_files_without_moving_original(self):
        self.assertEqual(local_db_path(), self.base)
        self.publish("shared", 6)
        with center_scope(OTHER):
            local = local_db_path()
            self.assertNotEqual(local, self.base)
            self.assertFalse(local.exists())
            self.assertFalse(store.list_files()["files"])
            self.assertFalse(local.exists())
            self.publish("shared", 7)
            self.assertTrue(local.is_file())
            self.assertEqual(local_db_path(global_store=True), self.base)
        self.assertTrue(self.base.is_file())


@unittest.skipUnless(PG, "Set METEO_TEST_POSTGRES_DSN for PostgreSQL tests")
class PostgreSQLCenterIsolationTests(CenterIsolationContract, unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        base = "meteo_center_qa_" + uuid4().hex
        with psycopg.connect(PG, autocommit=True) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(base)))
        settings = make_conninfo(PG, options=f"-csearch_path={base}")
        with psycopg.connect(settings) as db:
            scopes = tuple(_postgres_namespace(db, center) for center in (OTHER, THIRD))

        def cleanup():
            with psycopg.connect(PG, autocommit=True) as db:
                for schema in (*scopes, base):
                    db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

        self.addCleanup(cleanup)
        environment = patch.dict(os.environ, METEO_DATABASE_URL=settings)
        environment.start()
        self.addCleanup(environment.stop)
