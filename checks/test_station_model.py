"""Check archived benchmarks, chronological transfer and the confirmed-file workflow."""

import json
import os
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import fmean
from tempfile import TemporaryDirectory
from unittest.mock import patch

from analytics import database
from observation_store import confirm_file, preview_file, upload_file
from station_catalog import register_station
from station_model import MODEL_FILE, examples, features, fit_before, model_catalog, predict


class StationModelTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, METEO_DATABASE_URL="",
                         METEO_OBSERVATION_DB=str(Path(temporary.name) / "observations.sqlite"))
        env.start()
        self.addCleanup(env.stop)

    def test_training_uses_adjacent_days(self):
        values = {date.fromisoformat(day): value for day, value in [
            ("2025-01-01", 1), ("2025-01-02", 2), ("2025-01-04", 4),
            ("2025-01-05", 5), ("2025-01-06", 6),
        ]}
        [(day, season_and_trend, today, actual)] = examples(values, None)
        self.assertEqual((day.isoformat(), season_and_trend, today, actual), ("2025-01-06", (1, 0), 5, 6))

    def test_seasonal_phase_tracks_hemisphere_and_vanishes_without_location(self):
        spring = date(2026, 4, 1)
        autumn = date(2026, 10, 1)
        north = features(spring, 10, 8, 56.52)
        south = features(spring, 10, 8, -56.52)
        self.assertGreater(north[1], 0)
        self.assertEqual(north, (south[0], -south[1]))
        self.assertLess(features(autumn, 10, 8, 56.52)[1], 0)
        self.assertEqual(features(spring, 10, 8, None), (2, 0))
        self.assertEqual(features(spring, 10, 8, 0), (2, 0))

    def test_post_cutoff_and_excluded_station_do_not_affect_fit(self):
        with closing(database()) as connection:
            rows = connection.execute(
                "SELECT station, day, mean FROM daily WHERE station IN ('kr1','kr2') "
                "AND param='t_air' AND valid=1 ORDER BY station,day"
            )
            groups = {"kr1": {}, "kr2": {}}
            for station, day, mean in rows:
                groups[station][date.fromisoformat(day)] = mean
        cutoff = date(2025, 1, 1)
        original = {key: list(examples(readings, 56.52)) for key, readings in groups.items()}
        perturbed = {key: list(examples({
            day: mean + 20 if day >= cutoff or key == "kr2" else mean
            for day, mean in readings.items()
        }, 56.52)) for key, readings in groups.items()}
        self.assertEqual(fit_before(original, cutoff, "kr2"), fit_before(perturbed, cutoff, "kr2"))
        pooled_perturbed = {**perturbed, "kr2": original["kr2"]}
        self.assertEqual(fit_before(original, cutoff), fit_before(pooled_perturbed, cutoff))

    def test_published_metrics_match_rolling_archive_predictions(self):
        model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        self.assertEqual({test["station"] for test in model["tests"]}, {"ame60", "kr1", "kr2"})
        with closing(database()) as connection:
            for test in model["tests"]:
                station, year = test["station"], test["window_start"][:4]
                with self.subTest(station=station):
                    targets = connection.execute(
                        """SELECT target.day, source.day AS source_day, target.mean, source.mean
                           FROM daily AS target
                           JOIN daily AS source ON (source.station, source.param, source.day)
                             = (target.station, target.param, date(target.day, '-1 day'))
                           JOIN daily AS previous ON (previous.station, previous.param, previous.day)
                             = (target.station, target.param, date(target.day, '-2 day'))
                           WHERE target.station=? AND target.param='t_air' AND target.day LIKE ?
                             AND target.valid AND source.valid AND previous.valid
                           ORDER BY target.day""",
                        (station, f"{year}-%"),
                    ).fetchall()
                    model_errors, baseline_errors = [], []
                    for target, source, actual, baseline in targets:
                        result = predict({"station": station, "day": source})
                        self.assertEqual((result["target_day"], result["baseline_c"], result["observed_c"]),
                                         (target, baseline, actual))
                        self.assertLessEqual(result["fit"]["training_through"], source)
                        model_errors.append(abs(result["prediction_c"] - actual))
                        baseline_errors.append(abs(baseline - actual))
                    self.assertEqual(test["days"], len(targets))
                    self.assertEqual((test["first_target_day"], test["last_target_day"]),
                                     (targets[0][0], targets[-1][0]))
                    self.assertAlmostEqual(test["model_mae_c"], fmean(model_errors), places=11)
                    self.assertAlmostEqual(test["baseline_mae_c"], fmean(baseline_errors), places=11)

    def test_transfer_reports_donor_stations_and_absent_contemporaneous_training(self):
        report = {row["station"]: row for row in model_catalog()["transfer_tests"]}
        self.assertEqual(report["ame60"]["days"], 0)
        self.assertIn("отсутствуют", report["ame60"]["reason"])
        for key in ("kr1", "kr2"):
            test = report[key]
            self.assertNotIn(key, test["training"]["stations"])
            self.assertLess(test["training"]["last_target_day"], test["window_start"])
            self.assertGreater(test["days"], 0)

    def test_new_station_forecasts_with_manual_or_confirmed_observations(self):
        key = "test-model-station"
        register_station({"id": key, "label": "Полевая станция без координат"})
        station_meta = lambda: next(s for s in model_catalog()["stations"] if s["id"] == key)
        self.assertIsNone(station_meta()["archive"])
        self.assertFalse(station_meta()["has_observations"])
        with self.assertRaisesRegex(ValueError, "двое последовательных суток"):
            predict({"station": key})

        manual = predict({"station": key, "mode": "manual", "day": "2026-05-17",
                          "today": "11,375", "yesterday": "8.125"})
        self.assertEqual(manual["target_day"], "2026-05-18")
        self.assertEqual(manual["fit"], {"kind": "transfer", "training_through": "2025-12-31"})
        self.assertTrue(all(item["origin"] == "manual" for item in manual["inputs"]))

        def confirmable(days, name):
            rows = ["datetime,t_air"]
            rows.extend(f"{day} {hour:02d}:15,{temperature}"
                        for day, temperature in days for hour in range(18))
            uploaded = upload_file({"filename": name, "station": key}, ("\n".join(rows) + "\n").encode(), "text/csv")
            request = {"id": uploaded["id"], "station": key, "filters": "", "fill": "none"}
            return {**request, "revision": preview_file(request)["revision"]}

        gaps = confirmable([("2026-05-16", 8.125), ("2026-05-18", 13)], "gap.csv")
        confirm_file(gaps)
        self.assertIsNone(station_meta()["archive"])
        self.assertTrue(station_meta()["has_observations"])
        with self.assertRaisesRegex(ValueError, "2026-05-17"):
            predict({"station": key, "day": "2026-05-18"})

        consecutive = confirmable([("2026-05-17", 11.375)], "next.csv")
        self.assertIsNone(station_meta()["archive"])  # A draft adds no usable days.
        confirm_file(consecutive)
        self.assertEqual(station_meta()["archive"]["default_source_day"], "2026-05-18")
        archive = predict({"station": key, "day": "2026-05-17"})
        self.assertEqual(archive["target_day"], "2026-05-18")
        self.assertEqual(archive["prediction_c"], manual["prediction_c"])
        self.assertEqual(archive["observed_c"], 13)
        self.assertEqual([item["temperature_c"] for item in archive["inputs"]], [8.125, 11.375])
        self.assertTrue(all(item["origin"] == "confirmed" for item in archive["inputs"]))

    def test_invalid_inputs_and_archive_gap(self):
        for query, message in [
            ({"station": "other"}, "Неизвестная станция"),
            ({"day": "20251230"}, "ГГГГ-ММ-ДД"),
            ({"station": "kr2", "day": "2025-05-24"}, "2025-05-23"),
            ({"mode": "manual", "day": "2025-12-30", "today": "nan", "yesterday": "4"},
             "за выбранный день"),
            ({"mode": "manual", "day": "9999-12-30", "today": "4", "yesterday": "3"},
             "завершившиеся сутки"),
        ]:
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, message):
                predict(query)
        early = predict({"station": "ame60", "day": "1992-07-02"})
        self.assertEqual(early["fit"]["kind"], "baseline")
        self.assertEqual(early["prediction_c"], early["baseline_c"])

    def test_completed_days_use_reporting_and_confirmed_source_timezones(self):
        key = "test-model-calendar"
        register_station({"id": key, "label": "Проверка календаря прогноза"})

        def publish(name, days, zone=None):
            rows = ["datetime,t_air", *(
                f"{day} {hour:02d}:15,{temperature}"
                for day, temperature in days for hour in range(18)
            )]
            body = ("\n".join(rows) + "\n").encode()
            file = upload_file({"filename": name, "station": key}, body, "text/csv")
            request = {"id": file["id"], "station": key, "filters": "", "fill": "none"}
            if zone:
                request["timezone"] = zone
            confirm_file({**request, "revision": preview_file(request)["revision"]})

        publish("default.csv", [("2026-11-19", 1), ("2026-11-20", 2), ("2026-11-21", 3)])
        manual = {"mode": "manual", "day": "2026-11-21", "today": "3", "yesterday": "2"}
        before_moscow_midnight = datetime(2026, 11, 21, 20, 59, tzinfo=timezone.utc)
        with patch("station_model.utc_now", return_value=before_moscow_midnight):
            with self.assertRaisesRegex(ValueError, "Europe/Moscow"):
                predict({**manual, "station": "kr1"})
        moment = datetime(2026, 11, 21, 21, 30, tzinfo=timezone.utc)
        with patch("station_model.utc_now", return_value=moment):
            stations = {entry["id"]: entry for entry in model_catalog()["stations"]}
            self.assertEqual((stations["kr1"]["calendar_timezone"], stations["kr1"]["last_complete_day"]),
                             ("Europe/Moscow", "2026-11-21"))
            self.assertEqual((stations[key]["calendar_timezone"], stations[key]["last_complete_day"]),
                             ("UTC", "2026-11-20"))
            self.assertEqual(predict({**manual, "station": "kr1"})["target_day"], "2026-11-22")
            with self.assertRaisesRegex(ValueError, "UTC"):
                predict({**manual, "station": key})
            with self.assertRaisesRegex(ValueError, "завершившиеся сутки"):
                predict({"station": key, "day": "2026-11-21"})
            self.assertEqual(stations[key]["archive"]["last_source_day"], "2026-11-20")
            self.assertIsNone(predict({"station": key})["observed_c"])

            publish("moscow.csv", [("2026-11-21", 4)], "Europe/Moscow")
            stations = {entry["id"]: entry for entry in model_catalog()["stations"]}
            self.assertEqual(stations[key]["archive"]["last_source_day"], "2026-11-21")
            self.assertEqual(stations[key]["last_complete_day"], "2026-11-20")
            self.assertEqual(predict({"station": key})["baseline_c"], 4)

        utc_midnight = datetime(2026, 11, 22, tzinfo=timezone.utc)
        with patch("station_model.utc_now", return_value=utc_midnight):
            self.assertEqual(predict({**manual, "station": key})["target_day"], "2026-11-22")


if __name__ == "__main__":
    unittest.main()
