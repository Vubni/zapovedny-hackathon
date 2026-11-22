"""Read-only source checks and optional integration against a dedicated empty PostgreSQL test database."""
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import observation_store as store
from migrate_observations import COLUMNS, MigrationError, _source_layout, migrate, source_snapshot
from station_catalog import register_station

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get("METEO_MIGRATION_TEST_DSN")
LEGACY_STATIONS = "CREATE TABLE registered_stations (id TEXT PRIMARY KEY,name_key TEXT UNIQUE NOT NULL,passport TEXT NOT NULL)"


class SourceChecks(unittest.TestCase):
    def test_snapshot_contains_already_committed_wal_only(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(LEGACY_STATIONS)
                writer.execute("INSERT INTO registered_stations VALUES ('first','first','{}')")
                writer.commit()
                with source_snapshot(source) as snapshot:
                    self.assertEqual(_source_layout(snapshot), {"registered_stations"})
                    writer.execute("INSERT INTO registered_stations VALUES ('next','next','{}')")
                    writer.commit()
                    self.assertEqual(snapshot.execute("SELECT id FROM registered_stations").fetchall()[0][0], "first")
                    self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM registered_stations").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        snapshot.execute("DELETE FROM registered_stations")
                self.assertEqual(writer.execute("SELECT COUNT(*) FROM registered_stations").fetchone()[0], 2)

    def test_missing_or_unknown_source_is_rejected(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            with self.assertRaises(FileNotFoundError), source_snapshot(source):
                pass
            self.assertFalse(source.exists())
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("CREATE TABLE weather_archive (data TEXT)")
            with source_snapshot(source) as snapshot, self.assertRaisesRegex(MigrationError, "Неизвестная схема"):
                _source_layout(snapshot)
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("DROP TABLE weather_archive")
                db.execute("CREATE TABLE registered_stations (id TEXT PRIMARY KEY,passport TEXT)")
            with source_snapshot(source) as snapshot, self.assertRaisesRegex(MigrationError, "схема столбцов"):
                _source_layout(snapshot)


@unittest.skipUnless(TEST_DSN, "set METEO_MIGRATION_TEST_DSN to a dedicated empty PostgreSQL test database")
class PostgreSQLChecks(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source.sqlite3"
        # Never use the configured application database for tests.
        self.destination = psycopg.connect(TEST_DSN, autocommit=True)
        self.addCleanup(self.destination.close)
        self.schema = "test_migration_" + uuid4().hex
        self.destination.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.destination.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        self.destination.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))

    def fill_source(self, *, private_station=True):
        with patch.dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(self.source)):
            station = "kr1"
            if private_station:
                for identifier, label in (("z-order", "Поле"), ("a-order", "Без загрузок")):
                    register_station({"id": identifier, "label": label})
                station = "z-order"
            original = b"\xef\xbb\xbfdatetime;t_air\r\n2024-04-01 00:00;2,5\r\n2024-04-01 00:30;3,25\r\n"
            uploaded = store.upload_file({"filename": "source.csv", "station": station}, original, "text/csv")
            query = {"id": uploaded["id"], "station": station}
            preview = store.preview_file(query)
            store.confirm_file(dict(query, revision=preview["revision"]))
            correction = b"datetime;t_air\n2024-04-01 00:00;2,75\n2024-04-01 00:30;3,25\n"
            corrected = store.corrected_file(dict(query, revision=preview["revision"], filename="corrected.csv"), correction, "text/csv")
            store.confirm_file(dict(query, revision=corrected["revision"]))
            store.preview_file(dict(query, ranges={"t_air": [0, 3]}))
            store.upload_file({"filename": "broken.csv", "station": station}, b"\0\xffunknown\0", "text/csv")
        if private_station:
            with closing(sqlite3.connect(self.source)) as db, db:
                db.execute("UPDATE registered_stations SET rowid = rowid * 3")
            return original, correction

    def test_full_migration_preserves_bytes_rows_and_order_and_refuses_repeat(self):
        original, correction = self.fill_source()
        before = self.source.read_bytes()
        result = migrate(self.source, self.destination)
        self.assertEqual(result, {"registered_stations": 2, "files": 2, "published": 2, "file_corrections": 1})
        self.assertEqual(self.source.read_bytes(), before)
        with source_snapshot(self.source) as source:
            for table, names in COLUMNS.items():
                order = "id" if "id" in names else "file_id"
                if table == "published":
                    order += ", time"
                columns = ", ".join(f'"{name}"' for name in names)
                statement = f'SELECT {columns} FROM "{table}" ORDER BY {order}'
                self.assertEqual(self.destination.execute(statement).fetchall(), [tuple(row) for row in source.execute(statement)])
        self.assertIn(original, [row[0] for row in self.destination.execute("SELECT original FROM files")])
        self.assertEqual(self.destination.execute("SELECT original_csv FROM file_corrections").fetchone()[0], correction)
        stations = self.destination.execute("SELECT ordinal,id FROM registered_stations ORDER BY ordinal").fetchall()
        self.assertEqual(stations, [(3, "z-order"), (6, "a-order")])
        with self.assertRaisesRegex(MigrationError, "не пуста"):
            migrate(self.source, self.destination)
        self.assertEqual(self.destination.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
        next_ordinal = self.destination.execute("INSERT INTO registered_stations (id,name_key,passport) "
                                                "VALUES ('new','new','{}') RETURNING ordinal").fetchone()[0]
        self.assertEqual(next_ordinal, 7)

    def test_station_only_legacy_cli(self):
        passport = '{"id":"legacy","label":"Лес","profile":"ame60","params":[]}'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute(LEGACY_STATIONS)
            db.execute("INSERT INTO registered_stations VALUES (?,?,?)", ("legacy", "лес", passport))
        # libpq options selects only this test's own schema for the subprocess.
        environment = dict(os.environ, METEO_DATABASE_URL=TEST_DSN, PGOPTIONS=f"-c search_path={self.schema}")
        process = subprocess.run([sys.executable, "-B", str(ROOT / "migrate_observations.py"), "--source", str(self.source)],
                                 env=environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["registered_stations"], 1)
        self.assertEqual(self.destination.execute("SELECT passport FROM registered_stations").fetchone()[0], passport)
        self.assertEqual(self.destination.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_late_validation_error_rolls_back_all_target_tables(self):
        self.fill_source()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE file_corrections SET parent_revision='unresolved'")
        with self.assertRaisesRegex(MigrationError, "родительскую ревизию"):
            migrate(self.source, self.destination)
        self.assertEqual(self.destination.execute("SELECT to_regclass('files'), to_regclass('registered_stations')").fetchone(), (None, None))

    def test_earlier_no_corrections_or_registered_stations(self):
        self.fill_source(private_station=False)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DROP TABLE file_corrections")
            db.execute("DROP TABLE registered_stations")
        counts = migrate(self.source, self.destination)
        self.assertEqual(counts, {"registered_stations": 0, "files": 2, "published": 2, "file_corrections": 0})

    def test_refuses_unrelated_target_and_inconsistent_published_values(self):
        self.fill_source()
        self.destination.execute("CREATE TABLE unrelated (id integer)")
        with self.assertRaisesRegex(MigrationError, "постороннюю схему"):
            migrate(self.source, self.destination)
        self.assertIsNone(self.destination.execute("SELECT to_regclass('files')").fetchone()[0])
        self.destination.execute("DROP TABLE unrelated")
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE published SET data='{}'")
        with self.assertRaisesRegex(MigrationError, "не соответствует"):
            migrate(self.source, self.destination)
        self.assertIsNone(self.destination.execute("SELECT to_regclass('files')").fetchone()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
