from __future__ import annotations

import csv
import io
import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import analytics
import observation_store as store
from observations_db import connection
from recent_observations import export_recent_csv, latest_five
from station_catalog import register_station


class RecentObservationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(
            Path(directory.name) / "recent.sqlite3",
        ))
        environment.start()
        self.addCleanup(environment.stop)

    def publish(self, station, fields, records):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(fields)
        writer.writerows(records)
        file = store.upload_file({"filename": "recent.csv", "station": station}, text.getvalue().encode(), "text/csv")
        request = {"id": file["id"], "station": station}
        preview = store.preview_file(request)
        return store.confirm_file(dict(request, revision=preview["revision"]))

    def add_estimates(self, file, parameter, estimates):
        with connection(write=True) as db:
            for stamp, value in estimates.items():
                data = db.execute("SELECT data FROM published WHERE file_id=? AND time=?",
                                  (file["id"], stamp)).fetchone()[0]
                term = json.loads(data)
                term["estimates"][parameter] = {"value": value, "method": "model", "enters_working_graphs": True}
                db.execute("UPDATE published SET data=? WHERE file_id=? AND time=?",
                           (json.dumps(term), file["id"], stamp))

    def test_latest_window_uses_real_measurement_and_separates_estimates(self):
        station = register_station({"id": "recent-probe", "label": "Пост последних суток",
                                    "minimum_slots": 2})["station"]["id"]
        source = self.publish(station, ("datetime", "t_air"), (
            ("2026-05-13 00:15", 10), ("2026-05-13 01:15", 12),
            ("2026-05-14 00:15", None), ("2026-05-15 00:15", None),
            ("2026-05-16 00:15", 13), ("2026-05-16 01:15", 15), ("2026-05-16 02:15", None),
            ("2026-05-17 00:15", 0), ("2026-05-18 00:15", None),
        ))
        self.add_estimates(source, "t_air", {
            "2026-05-15 00:15": 19, "2026-05-16 02:15": 27, "2026-05-18 00:15": 7,
        })
        self.publish(station, ("datetime", "rh"), (("2026-05-20 00:15", 80),))

        result = latest_five({"station": station, "param": "t_air", "start": "2024-01-01", "end": "2024-01-02"})
        self.assertEqual((result["start"], result["end"]), ("2026-05-13", "2026-05-17"))
        first, missing, estimated, mixed, incomplete = result["rows"]
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["observed", "missing", "estimated", "mixed", "observed"])
        self.assertEqual((first["observed_n"], first["mean"], first["minimum"], first["maximum"]),
                         (2, 11, 10, 12))
        self.assertEqual(missing["missing_n"], 1)
        self.assertIsNone(missing["mean"])
        self.assertFalse(estimated["complete"])
        self.assertEqual((estimated["estimated_n"], estimated["estimated_mean"]), (1, 19))
        self.assertIsNone(estimated["mean"])
        self.assertEqual((mixed["observed_n"], mixed["mean"], mixed["estimated_mean"]), (2, 14, 27))
        self.assertEqual(mixed["estimates_by_method"], {"model": 1})
        self.assertFalse(incomplete["complete"])
        self.assertIsNone(incomplete["mean"])
        self.assertEqual(incomplete["observed_term_mean"], 0)

        name, payload = export_recent_csv(result)
        self.assertEqual(name, f"{station}_t_air_latest-five.csv")
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[2]["mean"], "")
        self.assertEqual(rows[2]["estimated_mean"], "19.0")
        self.assertEqual(rows[4]["mean"], "")
        self.assertEqual(rows[4]["observed_term_mean"], "0.0")

    def test_canonical_end_moves_back_if_new_file_clears_last_archived_values(self):
        station = "kr1"
        base = latest_five({"station": station, "param": "t_air"})
        last = base["end"]
        draft = store.from_observations({"station": station, "param": "t_air", "start": last, "end": last})
        _, _, payload = store.original_file(draft)
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
        self.publish(station, ("datetime", "t_air"), ((row["datetime"], None) for row in rows))
        with closing(analytics.database()) as db:
            expected = db.execute("SELECT MAX(day) FROM daily WHERE station=? AND param='t_air' AND n>0 AND day<?",
                                  (station, last)).fetchone()[0]
        self.assertEqual(latest_five({"station": station, "param": "t_air"})["end"], expected)
        self.assertEqual(latest_five({"station": station, "param": "rh"})["end"], last)
        self.assertEqual(analytics.latest_five_days({"station": station, "param": "t_air"})["end"], last)

    def test_direction_average_is_circular_and_has_no_linear_extremes(self):
        station = register_station({"id": "recent-wind", "label": "Ветровой пост", "minimum_slots": 2})["station"]["id"]
        self.publish(station, ("datetime", "wind_dir"),
                     (("2026-05-13 00:15", 350), ("2026-05-13 01:15", 10)))
        day = latest_five({"station": station, "param": "wind_dir"})["rows"][-1]
        self.assertTrue(day["complete"])
        self.assertAlmostEqual(day["mean"] % 360, 0)
        self.assertIsNone(day["minimum"])
        self.assertIsNone(day["maximum"])

    def test_no_facts_returns_an_empty_window(self):
        station = register_station({"id": "recent-empty", "label": "Новый пост без фактов"})["station"]["id"]
        source = self.publish(station, ("datetime", "t_air"), (("2026-05-18 00:15", None),))
        self.add_estimates(source, "t_air", {"2026-05-18 00:15": 10})
        result = latest_five({"station": station, "param": "t_air"})
        self.assertEqual((result["start"], result["end"], result["rows"]), (None, None, []))
