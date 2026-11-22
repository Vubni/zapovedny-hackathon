import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean
from unittest.mock import patch

import analytics
import observation_store as store
from observations import CHARTS, DIRECTIONS, chart_data
from station_catalog import register_station


def query(stations="kr1", start="2025-01-01", end="2025-12-31", **settings):
    return chart_data(dict(stations=stations, start=start, end=end, **settings))


def series(result, station, role=None):
    return next(item for item in result["series"]
                if item["station"] == station and (role is None or item["role"] == role))


class ObservationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = str(Path(temporary.name) / "observations.sqlite3")
        patcher = patch.dict(os.environ, METEO_OBSERVATION_DB=database)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, station, fields, lines, **settings):
        csv = ("\n".join([fields, *lines]) + "\n").encode()
        file = store.upload_file({"filename": "observations.csv", "station": station}, csv, "text/csv")
        request = dict(id=file["id"], station=station, **settings)
        preview = store.preview_file(request)
        return dict(request, revision=preview["revision"])

    def test_empty_selection_and_strict_query(self):
        empty = query("", "2005-01-01", "2015-12-31")
        self.assertEqual(empty["series"], [])
        self.assertEqual(empty["notes"], ["Выберите хотя бы одну станцию"])
        self.assertEqual((empty["start"], empty["end"]), ("2005-01-01", "2015-12-31"))
        for invalid in (dict(stations="unknown"), dict(start="2025-13-01"),
                        dict(end="2024-12-31"), dict(grain="week"), dict(mode="other"),
                        dict(chart="unknown"), dict(start="0001-01-01", end="9999-12-31", grain="day")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                query(**invalid)

    def test_full_archive_keeps_calendar_and_explicit_missing_days(self):
        start, end = "2005-01-01", "2015-12-31"
        result = query("kr1,ame60", start, end, grain="day")
        modern, archive = (series(result, station)["points"] for station in ("kr1", "ame60"))
        self.assertEqual([point["x"] for point in modern], [point["x"] for point in archive])
        self.assertEqual(len(archive), 4017)
        self.assertTrue(all(point["y"] is None for point in modern))
        self.assertTrue(any(point["y"] is None for point in archive))
        self.assertTrue(any(point["y"] is not None for point in archive))
        self.assertEqual((result["start"], result["end"]), (start, end))
        gap = query("ame60", "2000-01-01", "2000-12-31")
        self.assertEqual(gap["coverage"][0]["valid_days"], 0)
        self.assertEqual(gap["coverage"][0]["total_days"], 366)

    def test_pressure_coverage_and_april_exclusion(self):
        result = query("kr1,ame60", "2024-01-01", "2024-12-31", chart="pressure")
        pressure = result["coverage"][0]
        self.assertEqual((pressure["valid_days"], pressure["total_days"]), (334, 366))
        self.assertEqual((pressure["valid_months"], pressure["total_months"]), (11, 12))
        self.assertEqual(pressure["station_valid_days"], 363)
        self.assertEqual(pressure["station_valid_months"], 12)
        self.assertIn("Апрель 2024", pressure["reason"])
        self.assertIsNone(series(result, "kr1")["points"][3]["y"])
        self.assertTrue(all(point["y"] is None for point in series(result, "ame60")["points"]))
        self.assertIn("не измеряет", result["coverage"][1]["reason"])

    def test_calendar_groups_with_complete_periods_and_extrema(self):
        result = query("kr1", "2024-02-01", "2024-02-29", chart="air_range", grain="decade")
        low = series(result, "kr1", "min")["points"]
        high = series(result, "kr1", "max")["points"]
        self.assertEqual([(point["x"], point["x_end"], point["total_days"]) for point in low],
                         [("2024-02-01", "2024-02-10", 10), ("2024-02-11", "2024-02-20", 10),
                          ("2024-02-21", "2024-02-29", 9)])
        self.assertTrue(all(a["y"] <= b["y"] for a, b in zip(low, high)))
        partial = query("kr1", "2024-02-08", "2024-02-12", grain="decade")
        self.assertTrue(all(point["y"] is None for point in series(partial, "kr1")["points"]))
        annual = query("kr1", "2024-01-01", "2025-12-31", chart="pressure", grain="year")
        points = series(annual, "kr1")["points"]
        self.assertEqual([(point["x"], point["x_end"]) for point in points],
                         [("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31")])
        self.assertLess(points[0]["used_days"], points[0]["valid_days"])

    def test_anomaly_and_short_trend(self):
        result = query(chart="air_anomaly")
        points = series(result, "kr1")["points"]
        self.assertTrue(all(point["years"] == 1 for point in points))
        self.assertEqual(len({point["baseline"] for point in points}), 1)
        self.assertAlmostEqual(sum(point["y"] * point["used_days"] for point in points), 0.0)
        two_years = query(chart="air_anomaly", baseline="2024-2025")
        self.assertEqual(series(two_years, "kr1")["years"], 2)
        short = query(chart="air_trend")
        self.assertIsNone(series(short, "kr1")["slope"])
        self.assertTrue(all(point["y"] is None for point in series(short, "kr1")["points"]))
        long = query("ame60,kr1", "1992-01-01", "2025-12-31", chart="air_trend", grain="year")
        self.assertIsNotNone(series(long, "ame60")["slope"])
        self.assertTrue(all(point["y"] is None for point in series(long, "kr1")["points"][:-4]))
        self.assertTrue(all(point["y"] is None for point in series(long, "ame60")["points"][-4:]))

    def test_comparison_uses_common_dates_and_original_hours(self):
        result = query("kr2,kr1", chart="temp_difference")
        paired = series(result, "kr2-kr1", "difference")["points"]
        for current, previous in zip(paired, analytics.comparison()["months"]):
            self.assertEqual((current["x"], current["valid_days"]), (previous["month"] + "-01", previous["pairs"]))
            self.assertAlmostEqual(current["y"], -previous["delta"])
            self.assertAlmostEqual(current["y"], current["kr2"] - current["kr1"])
        self.assertEqual(result["coverage"][-1]["valid_days"], 361)
        profile = query("kr1,kr2", chart="temp_diurnal")
        self.assertEqual(profile["axis"], "hour")
        for station in ("kr1", "kr2"):
            hours = series(profile, station)["points"]
            for point, previous in zip(hours, analytics.comparison()["hours"]):
                self.assertEqual(point["x"], f"{previous['hour']:02d}")
                self.assertEqual(point["n"], previous["pairs"])
                self.assertAlmostEqual(point["y"], previous[station])
        unavailable = query("kr1,ame60", chart="temp_difference")
        self.assertTrue(any(point["y"] is not None for point in series(unavailable, "kr1")["points"]))
        self.assertTrue(all(point["y"] is None for point in series(unavailable, "ame60")["points"]))

    def test_archived_wind_unavailable_and_valid_modes(self):
        for year, reason in ((1998, "отсутствует"), (2021, "флагам качества")):
            for mode in ("modern", "togu", "semenov"):
                with self.subTest(year=year, mode=mode):
                    result = query("ame60", f"{year}-01-01", f"{year}-12-31", chart="wind_rose", mode=mode)
                    self.assertTrue(all(point["y"] is None for item in result["series"] for point in item["points"]))
                    self.assertTrue(any(reason in note for note in result["notes"]))
        valid = query("kr1", chart="wind_rose")
        self.assertTrue(all(tuple(point["x"] for point in item["points"]) == DIRECTIONS
                            for item in valid["series"]))
        self.assertAlmostEqual(sum(point["y"] for item in valid["series"] for point in item["points"]), 100.0)

    def test_wind_sectors_speed_classes_and_calm_at_new_station(self):
        station = "wind-test"
        register_station(dict(id=station, label="Точка ветра", minimum_slots=4,
                              params=["wind_speed", "wind_dir"]))
        day = "2026-05-15"
        upload = self.upload(station, "datetime,wind_dir,wind_speed", [
            f"{day} 00:02,359,1",
            f"{day} 01:58,1,3",
            f"{day} 02:30,90,5",
            f"{day} 03:00,180,0",
        ])
        store.confirm_file(upload)
        modern = query(station, day, day, chart="wind_rose")
        self.assertAlmostEqual(modern["series"][0]["points"][0]["y"], 100 / 3)
        self.assertAlmostEqual(modern["series"][1]["points"][0]["y"], 100 / 3)
        self.assertAlmostEqual(modern["series"][2]["points"][4]["y"], 100 / 3)
        self.assertEqual(modern["calm_by_station"], {station: 25.0})
        togu = query(station, day, day, chart="wind_rose", mode="togu")
        frequency, speed = (series(togu, station, role) for role in ("frequency", "speed"))
        self.assertAlmostEqual(frequency["points"][0]["y"], 200 / 3)
        self.assertEqual(speed["points"][0]["y"], 2)
        self.assertEqual(speed["points"][4]["y"], 5)
        self.assertIsNone(speed["points"][8]["y"])
        calm = query(station, day, day, grain="day", chart="calm")
        self.assertEqual(series(calm, station)["points"][0]["y"], 25)

    def test_archive_parameter_replacement_and_restoration(self):
        day = "2024-04-01"
        raw = [row for _, row in analytics.archive_rows("kr1", 2024) if row["datetime"][:10] == day]
        first = raw[0]["datetime"]
        observed = [float(row["t_air"]) for row in raw if row["t_air"] and "t_air:" not in row["qc_flag"]]
        self.assertAlmostEqual(query("kr1", day, day, grain="day")["series"][0]["points"][0]["y"], fmean(observed))
        pressure = query("kr1", day, day, grain="day", chart="pressure")["series"][0]["points"][0]["y"]
        wind = query("kr1", day, day, grain="day", chart="wind_speed")["series"][0]["points"][0]["y"]
        initial = self.upload("kr1", "datetime,t_air,rh", [f"{first},20.0,95"])
        replacement = self.upload("kr1", "datetime,t_air", [f"{first},-100"])

        def average():
            return query("kr1", day, day, grain="day")["series"][0]["points"][0]["y"]

        self.assertAlmostEqual(average(), fmean(observed))
        store.confirm_file(initial)
        self.assertAlmostEqual(average(), fmean([20.0, *observed[1:]]))
        humidity = query("kr1", day, day, grain="day", chart="rh")["series"][0]["points"][0]["y"]
        self.assertEqual(query("kr1", day, day, grain="day", chart="pressure")["series"][0]["points"][0]["y"], pressure)
        self.assertEqual(query("kr1", day, day, grain="day", chart="wind_speed")["series"][0]["points"][0]["y"], wind)
        accepted = store.confirm_file(replacement)
        self.assertAlmostEqual(average(), fmean(observed[1:]))
        self.assertEqual(query("kr1", day, day, grain="day", chart="rh")["series"][0]["points"][0]["y"], humidity)
        store.discard_clean(dict(id=replacement["id"], version=accepted["version"]))
        self.assertAlmostEqual(average(), fmean([20.0, *observed[1:]]))

    def test_same_station_imports_before_and_after_archive(self):
        days = ("2021-12-31", "2022-01-01", "2026-01-01")
        lines = [f"{day} {hour:02d}:00,{temperature}" for day, temperature in zip(days, (15, 7, -2))
                 for hour in range(18)]
        upload = self.upload("kr1", "datetime,t_air", lines)
        store.confirm_file(upload)
        result = query("kr1", days[0], days[-1], grain="day")
        points = {point["x"]: point["y"] for point in series(result, "kr1")["points"]}
        self.assertEqual([points[day] for day in days], [15, 7, -2])
        self.assertIsNone(points["2022-01-02"])
        self.assertIsNotNone(points["2025-12-31"])

    def test_synthetic_estimate_does_not_override_archived_measurement(self):
        day = "2025-01-01"
        before = query("kr1", day, day, grain="day")
        stamp = next(row["datetime"] for _, row in analytics.archive_rows("kr1", 2025)
                     if row["t_air"] and not row["qc_flag"])
        synthetic = {"datetime": stamp, "declared": [], "synthetic": True,
                     "estimates": {"t_air": {"value": 40, "method": "model", "enters_working_graphs": True}}}
        with patch.object(store, "confirmed_rows", return_value=iter([synthetic])):
            after = query("kr1", day, day, grain="day")
        self.assertEqual(before["series"], after["series"])
        self.assertEqual(before["coverage"], after["coverage"])

    def test_model_estimates_have_separate_series_and_official_coverage(self):
        station = "temperature-test"
        register_station(dict(id=station, label="Точка температуры", minimum_slots=23, params=["t_air"]))
        origin = datetime(2026, 5, 15)
        days = origin.date().isoformat(), (origin + timedelta(days=2)).date().isoformat()
        lines = []
        for index in range(60):
            if index == 14:
                continue
            stamp = (origin + timedelta(hours=index)).isoformat(" ", timespec="minutes")
            lines.append(f"{stamp},{'' if index == 10 else index / 4}")
        upload = self.upload(station, "datetime,t_air", lines, fill="model")
        before = query(station, *days, grain="day")
        self.assertEqual(len(before["series"]), 1)
        self.assertEqual(before["coverage"][0]["valid_days"], 0)
        store.confirm_file(upload)
        after = query(station, *days, grain="day")
        self.assertEqual(after["coverage"][0]["valid_days"], 1)
        self.assertIsNone(series(after, station, "mean")["points"][0]["y"])
        overlay = series(after, station, "estimate")["points"][0]
        self.assertEqual(overlay["estimates_by_method"], {"model": 2})
        self.assertAlmostEqual(overlay["y"], fmean(index / 4 for index in range(24)))

    def test_every_chart_serializes_without_non_finite_numbers(self):
        for chart in CHARTS:
            with self.subTest(chart=chart):
                result = query("kr1,kr2,ame60", chart=chart)
                json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
