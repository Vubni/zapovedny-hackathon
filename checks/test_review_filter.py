"""Проверки экспериментального контроля качества по ZIP, SQLite и синтетическим рядам."""

import hashlib
import json
import math
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics import ARCHIVE, DATABASE, database
from review_excel import export_xlsx
from review_filter import observation, review
from review_upload import read_upload


def record(stamp, value, param="t_air", qc="", **fields):
    return {"datetime": stamp, param: value, "qc_flag": qc, **fields}


def series(first, step, values, param="t_air", **fields):
    return [record((first + timedelta(minutes=step * i)).isoformat(" ", timespec="minutes"),
                   value, param, **fields) for i, value in enumerate(values)]


def archived(station, param, start, end=None, **settings):
    return review(dict(station=station, param=param, start=start, end=end or start, **settings))


def uploaded(records, **settings):
    return review(dict(station="kr1", **settings), records=records)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReviewFilterChecks(unittest.TestCase):
    evidence = {}

    def test_base_source_validity_and_audit(self):
        first = "2030-02-01 "
        rows = [record(first + "10:00", "940", "pressure_hpa", "rh:fault", battery="024"),
                record(first + "10:30", "1050.00", "pressure_hpa"),
                record(first + "11:00", "1550", "pressure_hpa", "pressure_hpa:range"),
                record(first + "11:30", "1550.01", "pressure_hpa"),
                record(first + "12:00", "939.99", "pressure_hpa"),
                record(first + "12:30", "1110", "pressure_hpa", "pressure_hpa:range;pressure_hpa:fault"),
                record(first + "13:00", "NaN", "pressure_hpa"),
                record(first + "13:30", "  ", "pressure_hpa"),
                record(first + "14:00", None, "pressure_hpa"),
                record(first + "14:30", "-Inf", "pressure_hpa"),
                {"datetime": first + "15:00", "pressure_hpa": 1000},
                record(first + "15:30", "1060", "pressure_hpa")]
        originals = [dict(row) for row in rows]
        report = uploaded(rows, param="pressure_hpa", filters="", pressure_max=1550)
        obs = report["rows"]
        self.assertEqual([1, 2, 3, 11, 12], [row["line"] for row in obs if row["keep"]])
        self.assertEqual([1, 2, 11], [row["line"] for row in obs if row["official_keep"]])
        self.assertEqual(rows, originals)
        self.assertEqual([row["original"] for row in obs], originals)
        self.assertEqual(report["fields"], ["datetime", "pressure_hpa", "qc_flag", "battery"])
        self.assertEqual(obs[2]["reason_codes"], ["source_range_rechecked"])
        self.assertIn("pressure_hpa:range", obs[2]["reasons"][0])
        self.assertIn("fault", obs[5]["reason_codes"])
        self.assertEqual(obs[6]["value"], None)
        self.assertEqual(obs[6]["reason_codes"], ["invalid_number"])
        self.assertEqual(obs[9]["reason_codes"], ["invalid_number"])
        self.assertEqual(obs[11]["reason_codes"], ["expanded_range"])
        self.assertEqual(report["counts"]["restored"], 2)
        json.dumps(report, allow_nan=False)

    def test_input_validation(self):
        for value in (float("nan"), float("inf"), {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                uploaded([record("2025-01-01 00:00", value)])
        for bad_time in ("2025-02-29 00:00", "2025-01-01", "2025-01-01 00:00+03:00", "2025-01-01 00:00:01"):
            with self.subTest(time=bad_time), self.assertRaises(ValueError):
                uploaded([record(bad_time, 0)])
        with self.assertRaisesRegex(ValueError, "Неизвестные фильтры"):
            uploaded([record("2025-01-01 00:00", 0)], filters="fake")
        with self.assertRaisesRegex(ValueError, "Дата начала"):
            uploaded([record("2025-01-01 00:00", 0)], start="2025-01-02", end="2025-01-01")
        with self.assertRaisesRegex(ValueError, "1050 или 1550"):
            uploaded([record("2025-01-01 00:00", 0)], pressure_max=1550.1)

    def test_upload_source_row_and_time(self):
        class ExcelRow(dict):
            source_line = 32

        rows = [ExcelRow(record("2034-01-01T02:03:00", "-0.00", qc="", note="=1+1"))]
        report = uploaded(rows, source="эксперимент.xlsx", start="2033-12-31", end="2034-01-01")
        actual = report["rows"][0]
        self.assertEqual(actual["time"], "2034-01-01 02:03")
        self.assertEqual(actual["original"]["datetime"], "2034-01-01T02:03:00")
        self.assertEqual(actual["line"], 32)
        self.assertEqual(actual["source"], "эксперимент.xlsx")
        self.assertEqual(math.copysign(1, actual["value"]), -1)
        self.assertEqual(report["source_start"], "2034-01-01")
        self.assertEqual([day["raw_n"] for day in report["summaries"]["daily"]], [0, 1])

    def test_csv_decimal_comma_and_excel_source_text(self):
        body = b"datetime;t_air;qc_flag\n2024-04-01 01:00;2,5;\n2024-04-01 02:00;-0,1;\n"
        records = read_upload(body, "example.csv", "text/csv")
        report = uploaded(records, param="t_air", source="example.csv", filters="")
        rows = report["rows"]
        self.assertEqual([row["original"]["t_air"] for row in rows], ["2,5", "-0,1"])
        self.assertEqual([row["value"] for row in rows], [2.5, -0.1])
        self.assertTrue(all(row["keep"] and row["official_keep"] for row in rows))
        with ZipFile(BytesIO(export_xlsx(report))) as workbook:
            sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for address, text in (("O2", "2,5"), ("O3", "-0,1")):
            self.assertEqual(sheet.findtext(f".//x:c[@r='{address}']/x:is/x:t", namespaces=ns), text)
        for address, value in (("E2", 2.5), ("E3", -0.1)):
            self.assertEqual(float(sheet.findtext(f".//x:c[@r='{address}']/x:v", namespaces=ns)), value)

    def test_conflict_policies(self):
        first = "2030-01-01 "
        rows = [record(first + "01:00", -8), record(first + "02:00", -8.1), record(first + "02:00", 6),
                record(first + "03:00", -8.2), record(first + "07:00", 0), record(first + "07:00", 50),
                record(first + "09:00", 1), record(first + "09:00", 1),
                record(first + "18:00", 3, qc="dup_conflict")]
        all_conflicts = uploaded(rows, filters="conflicts")
        best = uploaded(rows, filters="conflicts", conflict_policy="best")
        no_filter = uploaded(rows, filters="")
        self.assertEqual(no_filter, uploaded(rows))
        self.assertEqual([row["line"] for row in all_conflicts["rows"] if row["keep"]], [1, 4])
        self.assertEqual([row["line"] for row in best["rows"] if row["keep"]], [1, 2, 4, 5])
        self.assertEqual([row["line"] for row in no_filter["rows"] if row["keep"]], [1, 2, 3, 4, 5, 7, 8, 9])
        self.assertEqual(best["rows"][1]["reason_codes"], ["conflict_selected"])
        self.assertIn("интерполяции", best["rows"][1]["reasons"][0])
        self.assertIn("conflict_off", no_filter["rows"][8]["reason_codes"])
        self.assertEqual(all_conflicts["summaries"]["daily"][0]["official"], best["summaries"]["daily"][0]["official"])
        self.assertEqual(all_conflicts["counts"]["official_kept"], no_filter["counts"]["official_kept"])

    def test_combined_hampel_spike_negative_and_real_time_gaps(self):
        first = datetime(2030, 1, 1, 8)
        temperatures = [-10.0, -9.8, -10.1, -10.2, -9.9, -23, -10, -9.9, -10.1, -10.3, -10.1]
        rows = series(first, 30, temperatures)
        report = uploaded(rows, filters="hampel,spike")
        target = report["rows"][5]
        self.assertEqual(target["reason_codes"], ["hampel", "spike"])
        self.assertFalse(target["keep"])
        self.assertTrue(target["official_keep"])
        self.assertEqual(report["counts"]["excluded"], 1)
        gap = [record("2030-01-01 00:00", 12), record("2030-01-01 02:00", -12), record("2030-01-01 04:00", 12)]
        self.assertTrue(all(row["keep"] for row in uploaded(gap, filters="spike,hampel")["rows"]))

    def test_temporal_context_at_crop_and_new_year(self):
        rows = series(datetime(2030, 12, 31, 23), 60, [-6, 32, -6])
        result = uploaded(rows, start="2031-01-01", end="2031-01-01", filters="spike")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["time"], "2031-01-01 00:00")
        self.assertIn("spike", result["rows"][0]["reason_codes"])
        self.assertTrue(result["rows"][1]["keep"])

    def test_circular_and_calm(self):
        first = datetime(2030, 1, 1, 0)
        seq = [358, 359, 360, 1, 2, 0, 359, 1, 358, 0, 1, 359, 358, 359, 1, 2, 359, 0, 1, 2]
        rows = series(first, 60, seq, "wind_dir", wind_speed="1.6")
        report = uploaded(rows, param="wind_dir", filters="spike,hampel")
        daily = report["summaries"]["daily"][0]["working"]
        self.assertEqual(report["counts"]["kept"], len(seq))
        self.assertTrue(daily["valid"])
        self.assertAlmostEqual((daily["mean"] + 180) % 360 - 180, 0, delta=0.1)
        self.assertEqual((daily["min"], daily["max"]), (None, None))
        rows[5]["wind_dir"] = 180
        rows[8].update(wind_dir=180, wind_speed=0)
        after = uploaded(rows, param="wind_dir", filters="spike,hampel")
        self.assertEqual(after["rows"][5]["reason_codes"], ["hampel", "spike"])
        self.assertEqual(after["rows"][8]["reason_codes"], ["calm"])
        self.assertTrue(after["rows"][8]["keep"])
        rows[8]["qc_flag"] = "wind_speed:fault"
        uncertain_calm = uploaded(rows, param="wind_dir", filters="spike,hampel")["rows"][8]
        self.assertNotIn("calm", uncertain_calm["reason_codes"])
        self.assertIn("spike", uncertain_calm["reason_codes"])
        opposite = series(first, 60, [0, 180] * 9, "wind_dir")
        day = uploaded(opposite, param="wind_dir")["summaries"]["daily"][0]["working"]
        self.assertTrue(day["valid"])
        self.assertIsNone(day["mean"])

    def test_stuck_continuity_and_legitimate_plateaus(self):
        first = datetime(2030, 1, 1, 0)
        rows = series(first, 60, [1000] * 13, "pressure_hpa")
        result = uploaded(rows, param="pressure_hpa", filters="stuck")
        self.assertEqual(result["counts"]["kept"], 0)
        with_gap = uploaded(rows[:6] + rows[7:], param="pressure_hpa", filters="stuck")
        self.assertEqual(with_gap["counts"]["kept"], 12)
        with_missing = list(rows)
        with_missing[6] = dict(rows[6], pressure_hpa=None)
        result = uploaded(with_missing, param="pressure_hpa", filters="stuck")
        self.assertEqual(result["counts"]["rejected"], 1)
        for param, value, fields in (("wind_speed", 0, {}), ("wind_gust", 0, {}), ("rh", 100, {}),
                                     ("wind_dir", 0, {"wind_speed": 0})):
            calm = series(first, 60, [value] * 20, param, **fields)
            self.assertEqual(uploaded(calm, param=param, filters="stuck")["counts"]["kept"], 20)

    def test_buddy_offsets_and_scope(self):
        first = datetime(2025, 5, 10, 6)
        peer = series(first - timedelta(minutes=10), 30, [10 + i * 0.1 for i in range(22)])
        main = series(first, 30, [13 + i * 0.1 for i in range(20)])
        main[10]["t_air"] -= 15

        def mock_archive(station, param, begin, end, fields, selected_years=None):
            records = main if station == "kr1" else peer
            fields.update(dict.fromkeys(records[0]))
            return (observation(row, i, station, param, station) for i, row in enumerate(records, 2))

        with patch("review_filter.archive_observations", mock_archive):
            filtered = archived("kr1", "t_air", "2025-05-10", filters="buddy")
        self.assertEqual(filtered["counts"]["excluded"], 1)
        self.assertIn("buddy", filtered["rows"][10]["reason_codes"])
        self.assertIn("+2.97", filtered["rows"][10]["reasons"][0])
        self.assertFalse(archived("kr1", "t_air", "2025-04-01", filters="buddy")["warnings"])
        self.assertIn("доступна для температуры", archived("kr1", "rh", "2025-04-01", filters="buddy")["warnings"][0])
        self.assertIn("загруженного файла", uploaded(main, filters="buddy")["warnings"][0])

    def test_slot_counts_and_equal_daily_weights(self):
        first = datetime(2032, 2, 1, 0)
        rows = series(first, 30, [0] * 35)
        rows += series(first + timedelta(days=1), 60, [10] * 18)
        rows += series(first + timedelta(days=2), 30, [25] * 34)
        rows += series(first + timedelta(days=3), 60, [0] * 72)
        report = uploaded(rows, start="2032-02-01", end="2032-02-07")
        day = [item["working"] for item in report["summaries"]["daily"]]
        self.assertEqual((day[0]["n"], day[0]["slots"], day[0]["mean"]), (35, 18, 0))
        self.assertEqual((day[1]["n"], day[1]["slots"], day[1]["mean"]), (18, 18, 10))
        self.assertEqual((day[2]["n"], day[2]["slots"], day[2]["mean"]), (34, 17, None))
        self.assertEqual(day[-1]["slots"], 0)
        self.assertTrue(report["summaries"]["monthly"][0]["partial"])
        full = [item for offset in range(29) for item in series(first + timedelta(days=offset),
                30 if offset == 0 else 60, [28] * 36 if offset == 0 else [0] * 18)]
        month = uploaded(full)["summaries"]["monthly"][0]["working"]
        self.assertAlmostEqual(month["mean"], 28 / 29)
        self.assertNotAlmostEqual(month["mean"], 28 * 36 / (36 + 18 * 28))
        self.assertEqual((month["min"], month["max"], month["mean_days"]), (0, 28, 29))
        am = review({"station": "ame60"}, series(first, 15, [0] * 72))["summaries"]["daily"][0]["working"]
        self.assertEqual((am["slots"], am["n"], am["valid"]), (72, 72, True))
        am_under = review({"station": "ame60"}, series(first, 15, [0] * 71))["summaries"]["daily"][0]["working"]
        self.assertEqual((am_under["slots"], am_under["valid"], am_under["mean"]), (71, False, None))

    def test_month_thresholds_and_missing_days(self):
        first = datetime(2032, 2, 1, 0)
        missing = {2, 3, 4, 5, 9, 12, 15, 18, 21, 25}

        def month_without(days):
            rows = [row for offset in range(29) if offset not in days
                    for row in series(first + timedelta(days=offset), 60, [1] * 18)]
            return uploaded(rows, start="2032-02-01", end="2032-02-29")

        result = month_without(missing)
        stats = result["summaries"]["monthly"][0]["working"]
        self.assertEqual((stats["valid"], stats["incomplete_days"], stats["longest_gap"]), (True, 10, 4))
        self.assertEqual(len(result["rows"]), 19 * 18)
        self.assertEqual(len(result["summaries"]["daily"]), 29)
        self.assertEqual(result["summaries"]["daily"][2]["raw_n"], 0)
        over_total = month_without(missing | {27})["summaries"]["monthly"][0]["working"]
        self.assertEqual((over_total["valid"], over_total["longest_gap"]), (False, 4))
        over_gap = month_without((missing - {25}) | {6})["summaries"]["monthly"][0]["working"]
        self.assertEqual((over_gap["valid"], over_gap["incomplete_days"], over_gap["longest_gap"]), (False, 10, 5))

    def test_archive_conflicts_and_minute_preservation(self):
        report = archived("ame60", "t_air", "1993-04-02", filters="conflicts")
        rows = [row for row in report["rows"] if row["conflict"]]
        self.assertEqual([(row["time"], row["line"]) for row in rows],
                         [("1993-04-02 16:15", 8802), ("1993-04-02 16:15", 8803)])
        self.assertTrue(all(not row["keep"] and not row["official_keep"] for row in rows))
        retained = archived("ame60", "t_air", "1993-04-02")
        self.assertEqual(retained["counts"]["restored"], 2)
        self.assertEqual(retained["counts"]["official_kept"], report["counts"]["official_kept"])
        irregular = archived("ame60", "t_air", "2018-05-01", "2018-09-01")
        off_grid = [row for row in irregular["rows"] if int(row["time"][-2:]) % 15]
        self.assertTrue(off_grid)
        self.assertTrue(all(row["time"] == row["original"]["datetime"] for row in off_grid))
        self.evidence["archive_conflict"] = {"date": "1993-04-02", "lines": [row["line"] for row in rows],
                                             "counts": report["counts"], "default_counts": retained["counts"]}
        self.evidence["off_grid_records"] = len(off_grid)

    def test_archive_pressure_official_rollups_match_sqlite(self):
        archive_before, database_before = digest(ARCHIVE), digest(DATABASE)
        report = archived("kr1", "pressure_hpa", "2024-01-01", "2024-12-31", pressure_max="1550", filters="conflicts,stuck")
        with closing(database()) as db:
            daily = db.execute("SELECT * FROM daily WHERE station='kr1' AND param='pressure_hpa' AND day BETWEEN '2024-01-01' AND '2024-12-31' ORDER BY day").fetchall()
            monthly = db.execute("SELECT * FROM monthly WHERE station='kr1' AND param='pressure_hpa' AND month BETWEEN '2024-01' AND '2024-12' ORDER BY month").fetchall()
        self.assertEqual(len(report["summaries"]["daily"]), len(daily))
        self.assertEqual(len(report["summaries"]["monthly"]), len(monthly))
        for actual, expected in zip(report["summaries"]["daily"], daily):
            self.assertEqual(actual["day"], expected["day"])
            self.assertEqual(actual["raw_n"], expected["raw_n"])
            self.assert_report_metrics(actual["official"], expected, ("slots", "n", "valid", "min", "max", "mean"))
        for actual, expected in zip(report["summaries"]["monthly"], monthly):
            self.assertEqual(actual["month"], expected["month"])
            self.assert_report_metrics(actual["official"], expected, ("valid_days", "longest_gap", "valid", "min", "max", "mean"))
        extended = archived("kr1", "pressure_hpa", "2024-04-01", "2024-04-30", pressure_max="1550")
        self.assertEqual(extended["counts"]["restored"], 1)
        restored = [(row["line"], row["time"], row["value"]) for row in extended["rows"]
                    if row["keep"] and not row["official_keep"]]
        self.assertEqual(restored, [(5643, "2024-04-28 16:37", 1100.0)])
        april = archived("kr1", "pressure_hpa", "2024-04-01", filters="stuck")
        day = april["summaries"]["daily"][0]
        self.assertEqual((day["official"]["n"], day["official"]["slots"], day["working"]["n"]), (38, 19, 0))
        self.assertAlmostEqual(day["official"]["mean"], 1005.45)
        self.assertEqual((digest(ARCHIVE), digest(DATABASE)), (archive_before, database_before))
        self.evidence["official_sqlite"] = {"days": len(daily), "months": len(monthly)}
        self.evidence["archive_pressure"] = {"extended_april": extended["counts"],
                                             "restored_observation": restored[0], "stuck_april_1": april["counts"]}
        self.evidence["source_sha256"] = {"archive": archive_before, "database": database_before}

    def test_archive_range_spanning_more_than_one_year(self):
        result = archived("kr2", "t_air", "2023-01-01", "2024-06-01")
        self.assertEqual(len(result["summaries"]["daily"]), 518)
        self.assertEqual(result["summaries"]["daily"][0]["day"], "2023-01-01")
        self.assertEqual(result["summaries"]["daily"][-1]["day"], "2024-06-01")
        self.assertEqual(result["counts"]["raw_n"], len(result["rows"]))
        self.evidence["unrestricted_period"] = {"start": result["start"], "end": result["end"], "days": 518}

    def assert_report_metrics(self, actual, expected, fields):
        for key in fields:
            if key == "mean" and actual[key] is not None and expected[key] is not None:
                self.assertAlmostEqual(actual[key], expected[key], places=10)
            else:
                self.assertEqual(actual[key], expected[key], key)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReviewFilterChecks)
    report = unittest.TextTestRunner(verbosity=2).run(suite)
    if report.wasSuccessful() and "--full-archive" in sys.argv[1:]:
        full = archived("ame60", "t_air", "1992-01-01", "2021-12-31")
        full_counts = len(full["rows"]), len(full["summaries"]["daily"]), len(full["summaries"]["monthly"])
        if full_counts != (805561, 10958, 360):
            raise AssertionError(f"Неверное покрытие полного архива АМЕ-60: {full_counts}")
        ReviewFilterChecks.evidence["full_ame60_archive"] = dict(zip(("rows", "days", "months"), full_counts))
        print(f"Полный архив АМЕ-60: {full_counts[0]} строк, {full_counts[1]} суток, {full_counts[2]} месяцев")
    if report.wasSuccessful() and "--write-results" in sys.argv[1:]:
        destination = ROOT / "results" / "review" / "filter-checks.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"tests": report.testsRun, "failures": 0, **ReviewFilterChecks.evidence},
                                          ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(not report.wasSuccessful())
