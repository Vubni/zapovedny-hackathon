"""Run the mutable-store lifecycle in an isolated PostgreSQL test schema."""
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import observation_store as store
from observations import chart_data
from observations_db import check_ready, connection, initialize
from station_catalog import register_station


POSTGRES_DSN = os.environ.get("METEO_TEST_POSTGRES_DSN")
DAY = "2026-05-16"


@unittest.skipUnless(POSTGRES_DSN, "Set METEO_TEST_POSTGRES_DSN for PostgreSQL tests")
class PostgreSQLObservationStoreTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        schema = "meteo_import_" + uuid4().hex

        def schema_command(statement):
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as db:
                db.execute(sql.SQL(statement).format(sql.Identifier(schema)))

        schema_command("CREATE SCHEMA {}")
        self.addCleanup(schema_command, "DROP SCHEMA {} CASCADE")
        setting = make_conninfo(POSTGRES_DSN, options=f"-csearch_path={schema}")
        environment = patch.dict(os.environ, METEO_DATABASE_URL=setting)
        environment.start()
        self.addCleanup(environment.stop)

    def add_station(self):
        return register_station({
            "id": "import-postgres", "label": "Пост PostgreSQL", "minimum_slots": 1,
            "latitude": 0, "longitude": -72.125,
        })["station"]["id"]

    def upload(self, station, value, name="Кр1.csv"):
        body = f"datetime,t_air\n{DAY} 00:15,{value}\n".encode()
        file = store.upload_file({"station": station, "filename": name}, body, "text/csv")
        self.assertEqual(store.original_file(file)[2], body)
        return {"id": file["id"], "station": station}

    def preview(self, query, **settings):
        request = dict(query, **settings)
        response = store.preview_file(request)
        return dict(request, revision=response["revision"])

    def chart_value(self, station):
        chart = chart_data({"stations": station, "chart": "air", "grain": "day", "start": DAY, "end": DAY})
        return chart["series"][0]["points"][0]["y"]

    def test_concurrent_initialization_and_bytea_publish_lifecycle(self):
        command = [sys.executable, "-B", "-c", "from observations_db import initialize; initialize()"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            processes = [pool.submit(subprocess.run, command, timeout=15, capture_output=True, text=True)
                         for _ in range(4)]
            for future in processes:
                result = future.result(timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(check_ready())
        with connection() as db:
            self.assertTrue(all(db.table_exists(table) for table in
                                ("files", "published", "registered_stations", "file_corrections")))
        station = self.add_station()
        query = self.upload(station, 12.5)
        parent = self.preview(query)
        name, original = store.draft_csv(parent)
        corrected = store.corrected_file(dict(parent, filename=name), original.replace(b";12.5", b";12.875"), "text/csv")
        self.assertIsNone(self.chart_value(station))
        confirmed = store.confirm_file(dict(query, revision=corrected["revision"]))
        self.assertEqual(self.chart_value(station), 12.875)
        self.assertEqual(store.all_date_bounds(), {station: (DAY, DAY)})
        with connection() as db:
            lineage = db.execute("SELECT parent_revision, original_csv FROM file_corrections WHERE file_id=?",
                                 (query["id"],)).fetchone()
            self.assertEqual(lineage["parent_revision"], parent["revision"])
            self.assertEqual(lineage[1], original.replace(b";12.5", b";12.875"))
        discarded = store.discard_clean(dict(query, version=confirmed["version"]))
        self.assertEqual(discarded["status"], "original")
        self.assertIsNone(self.chart_value(station))

    def test_concurrent_preview_and_confirm_do_not_publish_an_old_revision(self):
        initialize()
        station = self.add_station()
        query = self.upload(station, 7.125)
        original_review = store._review
        started, release = Event(), Event()

        def wait_for_newer(*args):
            if args[3]["filters"] == ["spike"]:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Timed out waiting for the newer preview")
            return original_review(*args)

        with patch.object(store, "_review", side_effect=wait_for_newer), ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(self.preview, query, filters="spike")
            try:
                self.assertTrue(started.wait(5))
                latest = self.preview(query, filters="hampel")
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "новый расчёт"):
                old.result(timeout=10)
        self.assertIsNone(self.chart_value(station))
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirms = [pool.submit(store.confirm_file, latest) for _ in range(2)]
            self.assertEqual([future.result(timeout=10)["version"] for future in confirms], [1, 1])
        self.assertEqual(self.chart_value(station), 7.125)

    def test_atomic_publications_have_distinct_order_and_discard_races_are_safe(self):
        initialize()
        station = self.add_station()
        values = (6.125, 15.125)
        queries = [self.upload(station, value, name=f"{value}.csv") for value in values]
        temperatures = {query["id"]: value for query, value in zip(queries, values)}
        previews = [self.preview(query) for query in queries]
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirms = [pool.submit(store.confirm_file, req) for req in previews]
            for future in confirms:
                self.assertEqual(future.result(timeout=10)["version"], 1)
        with connection() as db:
            published = db.execute("SELECT id, confirmed_order FROM files ORDER BY confirmed_order")
            ids, orders = zip(*(tuple(row) for row in published))
            self.assertEqual(orders, (1, 2))
        newest = next(query for query in queries if query["id"] == ids[-1])
        self.assertEqual(self.chart_value(station), temperatures[ids[-1]])

        pending = self.preview(newest, filters="hampel")
        start = Barrier(2)

        def run(operation, request):
            start.wait(timeout=5)
            try:
                return operation(request)["status"]
            except ValueError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(run, store.confirm_file, pending)
            removal = pool.submit(run, store.discard_clean, dict(newest, version=1))
            statuses = publication.result(timeout=10), removal.result(timeout=10)
        self.assertIn(statuses, [("confirmed", "stale"), ("stale", "original")])
        self.assertEqual(store.file_record(newest)["version"], 2)
        winner = ids[-1] if statuses[0] == "confirmed" else ids[0]
        self.assertEqual(self.chart_value(station), temperatures[winner])
