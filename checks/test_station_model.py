"""Run from the project root with python3 -m unittest discover -s checks -p test_station_model.py."""

import json
import unittest
from contextlib import closing
from datetime import date
from statistics import fmean

from analytics import database
from station_model import HOLDOUT_YEARS, MODEL_FILE, evaluate_station, examples, predict


class StationModelTest(unittest.TestCase):
    def test_regression_uses_adjacent_days(self):
        values = {date.fromisoformat(day): value for day, value in [
            ("2025-01-01", 1),
            ("2025-01-02", 2),
            ("2025-01-04", 4),
            ("2025-01-05", 5),
            ("2025-01-06", 6),
        ]}
        [(day, inputs, actual)] = examples(values)
        self.assertEqual((day.isoformat(), inputs[1:3], actual), ("2025-01-06", (5, 1), 6))

    def test_holdout_does_not_affect_fit(self):
        with closing(database()) as connection:
            rows = connection.execute(
                "SELECT day, mean FROM daily WHERE station='kr1' AND param='t_air' "
                "AND valid=1 ORDER BY day"
            )
            real = {date.fromisoformat(day): mean for day, mean in rows}
        perturbed = {day: mean + 20 if day.year == 2025 else mean for day, mean in real.items()}
        original, altered = (evaluate_station("kr1", values) for values in (real, perturbed))
        self.assertEqual(original["coefficients"], altered["coefficients"])
        self.assertNotEqual(original["test"]["model_mae_c"], altered["test"]["model_mae_c"])

    def test_published_metrics_match_rolling_archive_predictions(self):
        artifact = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        from_station = {fit["id"]: fit for fit in artifact["stations"]}
        self.assertEqual(from_station.keys(), HOLDOUT_YEARS.keys())
        with closing(database()) as connection:
            for station, year in HOLDOUT_YEARS.items():
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
                        model_errors.append(abs(result["prediction_c"] - actual))
                        baseline_errors.append(abs(baseline - actual))
                    fit = from_station[station]
                    test = fit["test"]
                    self.assertEqual(test["days"], len(targets))
                    self.assertEqual((test["first_target_day"], test["last_target_day"]),
                                     (targets[0][0], targets[-1][0]))
                    self.assertLess(fit["training"]["last_target_day"], test["window_start"])
                    self.assertAlmostEqual(test["model_mae_c"], fmean(model_errors), places=11)
                    self.assertAlmostEqual(test["baseline_mae_c"], fmean(baseline_errors), places=11)

    def test_manual_inputs_match_archived_lags(self):
        archived = predict({"station": "kr1"})
        previous, latest = archived["inputs"]
        manual = predict({"station": "kr1", "mode": "manual", "day": archived["source_day"],
                          "today": str(latest["temperature_c"]).replace(".", ","),
                          "yesterday": str(previous["temperature_c"])})
        self.assertEqual(manual["inputs"], archived["inputs"])
        self.assertEqual(manual["prediction_c"], archived["prediction_c"])
        self.assertIsNone(manual["observed_c"])

    def test_invalid_inputs_and_archive_gap(self):
        for query, message in [
            ({"station": "other"}, "Неизвестная станция"),
            ({"day": "20251230"}, "ГГГГ-ММ-ДД"),
            ({"day": "2024-12-30"}, "обучение уже завершено"),
            ({"station": "kr2", "day": "2025-05-24"}, "2025-05-23"),
            ({"mode": "manual", "day": "2025-12-30", "today": "nan", "yesterday": "4"},
             "за выбранный день"),
            ({"mode": "manual", "day": "9999-12-30", "today": "4", "yesterday": "3"},
             "завершившиеся сутки"),
        ]:
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, message):
                predict(query)
        after_archive = predict({"station": "kr1", "day": "2025-12-31"})
        self.assertEqual(after_archive["target_day"], "2026-01-01")
        self.assertIsNone(after_archive["observed_c"])


if __name__ == "__main__":
    unittest.main()
