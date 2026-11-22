"""Calendar boundaries and independent chart reports for disjoint observation windows."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean
from unittest.mock import patch

from observations import MAX_INTERVALS, chart_data, query_intervals


class ObservationIntervalsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "observations.sqlite3"
        patcher = patch.dict(os.environ, METEO_OBSERVATION_DB=str(database))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normalization_merges_overlaps_adjacent_dates_and_repeated_ranges(self):
        selected = query_intervals({
            "intervals": "2024-03-15:2024-03-16,2024-02-28:2024-03-01,2024-03-02:2024-03-03,"
                         "2024-02-29:2024-03-02,2024-03-15:2024-03-16",
            "start": "ignored",
        })
        self.assertEqual(selected, ((date(2024, 2, 28), date(2024, 3, 3)),
                                    (date(2024, 3, 15), date(2024, 3, 16))))
        self.assertEqual(query_intervals({"start": "9999-12-31", "end": "9999-12-31"}),
                         ((date.max, date.max),))
        self.assertEqual(query_intervals({"intervals": "9999-12-31:9999-12-31,9999-12-30:9999-12-30"}),
                         ((date(9999, 12, 30), date.max),))

    def test_validation_and_limit(self):
        invalid = ("", " ", None, [], "2024-01-01", "2024-01-01:", "2024-01-01/2024-01-02",
                   "2024-01-02:2024-01-01", "2023-02-29:2023-03-01", "20240101:2024-01-02",
                   "2024-01-01:2024-01-02,", "2024-01-01:2024-01-02:2024-01-03")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                query_intervals({"intervals": value})
        point = "2024-01-01:2024-01-01"
        self.assertEqual(len(query_intervals({"intervals": ",".join([point] * MAX_INTERVALS)})), 1)
        with self.assertRaisesRegex(ValueError, str(MAX_INTERVALS)):
            query_intervals({"intervals": ",".join([point] * (MAX_INTERVALS + 1))})

    def test_legacy_and_normalized_single_interval_contract(self):
        init = {"stations": "kr1", "chart": "wind_rose", "grain": "month"}
        legacy = chart_data(dict(init, start="2025-04-01", end="2025-04-05"))
        self.assertNotIn("intervals", legacy)
        self.assertTrue(all("interval" not in item for item in legacy["series"]))
        self.assertIn("calm_by_station", legacy)
        result = chart_data(dict(init, intervals="2025-04-03:2025-04-05,2025-04-01:2025-04-03"))
        self.assertEqual(result.pop("intervals"), [{
            "id": "2025-04-01:2025-04-05", "start": "2025-04-01", "end": "2025-04-05",
            "days": 5, "label": "01.04.2025 — 05.04.2025",
        }])
        self.assertEqual(result, legacy)

    def test_daily_series_coverage_and_stats_skip_middle_dates(self):
        query = {"stations": "kr1", "chart": "air_range", "grain": "day"}
        result = chart_data(dict(query, intervals="2025-07-15:2025-07-17,2025-04-01:2025-04-02"))
        self.assertEqual((result["start"], result["end"]), ("2025-04-01", "2025-07-17"))
        self.assertEqual(len(result["series"]), 4)
        self.assertEqual(len({item["id"] for item in result["series"]}), 4)
        self.assertEqual({row["base_id"] for row in result["series"]}, {"kr1:t_air:min", "kr1:t_air:max"})
        for interval in result["intervals"]:
            first = date.fromisoformat(interval["start"])
            expected = [(first + timedelta(days=offset)).isoformat() for offset in range(interval["days"])]
            series = [row for row in result["series"] if row["interval"] == interval["id"]]
            self.assertEqual(len(series), 2)
            for row in series:
                self.assertEqual(row["id"], row["base_id"] + "@" + interval["id"])
                self.assertTrue(row["label"].endswith(interval["label"]))
                self.assertEqual([point["x"] for point in row["points"]], expected)
            coverage = next(row for row in result["coverage"] if row["interval"] == interval["id"])
            stats = next(row for row in result["statistics"] if row["interval"] == interval["id"])
            self.assertEqual((coverage["total_days"], coverage["valid_days"], stats["total_days"], stats["valid_days"]),
                             (interval["days"],) * 4)
            alone = chart_data(dict(query, start=interval["start"], end=interval["end"]))
            self.assertEqual([row["points"] for row in series], [row["points"] for row in alone["series"]])
            self.assertEqual({k: stats[k] for k in alone["statistics"][0]}, alone["statistics"][0])

    def test_month_and_year_points_belong_to_the_actual_window(self):
        query = {"stations": "kr1", "chart": "air", "intervals": "2025-04-03:2025-04-07,2025-04-09:2025-04-15"}
        for grain in ("decade", "month", "year"):
            with self.subTest(grain=grain):
                result = chart_data(dict(query, grain=grain))
                for series in result["series"]:
                    self.assertTrue(all(series["interval_start"] <= point["x"] <= point["x_end"] <= series["interval_end"]
                                        for point in series["points"]))
                self.assertEqual([series["points"][0]["x"] for series in result["series"]],
                                 ["2025-04-03", "2025-04-09"])
                self.assertEqual([row["total_days"] for row in result["coverage"]], [5, 7])
                self.assertEqual([row["valid_days"] for row in result["statistics"]], [5, 7])

    def test_statistics_use_valid_days_and_original_extremes_across_grains(self):
        settings = {"stations": "kr1", "start": "2024-04-01", "end": "2024-04-03"}
        daily = chart_data(dict(settings, chart="air", grain="day"))
        extrema = chart_data(dict(settings, chart="air_range", grain="day"))
        monthly = chart_data(dict(settings, chart="air_range", grain="month"))
        self.assertEqual(daily["statistics"], monthly["statistics"])
        self.assertTrue(all(point["y"] is None for item in monthly["series"] for point in item["points"]))
        stats = monthly["statistics"][0]
        days = daily["series"][0]["points"]
        self.assertEqual(stats["valid_days"], len(days))
        self.assertEqual(stats["mean"], fmean(day["y"] for day in days))
        self.assertEqual(stats["min"], min(point["y"] for point in extrema["series"][0]["points"]))
        self.assertEqual(stats["max"], max(point["y"] for point in extrema["series"][1]["points"]))
        self.assertIn(stats["min_day"], [day["x"] for day in days])
        self.assertIn(stats["max_day"], [day["x"] for day in days])

    def test_trend_excludes_unselected_years(self):
        options = {"stations": "ame60", "chart": "air_trend", "grain": "year"}
        result = chart_data(dict(options, intervals="2009-01-01:2009-12-31,2016-01-01:2016-12-31"))
        self.assertEqual([row["total_days"] for row in result["coverage"]], [365, 366])
        self.assertTrue(all(item["slope"] is None and item["trend_months"] == 0 for item in result["series"]))
        self.assertTrue(all(point["y"] is None for item in result["series"] for point in item["points"]))
        continuous = chart_data(dict(options, start="2009-01-01", end="2016-12-31"))
        self.assertIsNotNone(continuous["series"][0]["slope"])

    def test_profiles_roses_calm_and_difference_follow_their_intervals(self):
        selected = "2025-04-01:2025-04-03,2025-04-08:2025-04-10"
        settings = {"stations": "kr1,kr2", "grain": "day"}
        modes = (("temp_diurnal", "modern"), ("wind_rose", "modern"), ("wind_rose", "togu"),
                 ("wind_rose", "semenov"), ("wind_freq", "modern"), ("calm", "modern"),
                 ("temp_difference", "modern"))
        for chart, mode in modes:
            with self.subTest(chart=chart, mode=mode):
                options = dict(settings, chart=chart, mode=mode)
                result = chart_data(dict(options, intervals=selected))
                for interval in result["intervals"]:
                    alone = chart_data(dict(options, start=interval["start"], end=interval["end"]))
                    series = [row for row in result["series"] if row["interval"] == interval["id"]]
                    self.assertEqual([item["base_id"] for item in series], [item["id"] for item in alone["series"]])
                    self.assertEqual([item["points"] for item in series], [item["points"] for item in alone["series"]])
                    if "calm_by_station" in alone:
                        self.assertEqual(result["calm_by_interval"][interval["id"]], alone["calm_by_station"])
                        self.assertNotIn("calm_by_station", result)

    def test_empty_selection_missing_values_and_budget(self):
        selection = "2000-01-01:2000-01-02,2000-01-10:2000-01-11"
        empty = chart_data({"intervals": selection})
        self.assertEqual((empty["series"], empty["coverage"], empty["statistics"]), ([], [], []))
        self.assertEqual((empty["start"], empty["end"], len(empty["intervals"])), ("2000-01-01", "2000-01-11", 2))
        self.assertEqual(empty["notes"], ["Выберите хотя бы одну станцию"])
        options = {"stations": "kr1", "grain": "day"}
        missing = chart_data(dict(options, intervals=selection))
        self.assertEqual([row["valid_days"] for row in missing["coverage"]], [0, 0])
        self.assertEqual([row["mean"] for row in missing["statistics"]], [None, None])
        self.assertEqual([point["y"] for item in missing["series"] for point in item["points"]], [None] * 4)
        with patch("observations.MAX_POINTS", 7):
            chart_data(dict(options, start="2000-01-01", end="2000-01-02"))
            with self.assertRaisesRegex(ValueError, "Слишком много точек"):
                chart_data(dict(options, intervals=selection))
        distant = "1992-01-01:1992-01-02,2025-01-01:2025-01-02"
        with patch("observations.MAX_POINTS", 8):
            self.assertEqual(len(chart_data(dict(options, intervals=distant))["series"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
