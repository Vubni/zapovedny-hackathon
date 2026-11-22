"""Regression checks for two independent fill methods and their exported lineage."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import observation_store as store
from gap_fill import station_estimates
from review_service import create_report, report_excel, report_page


FIRST = "2024-04-21 10:00"
SECOND = "2024-04-21 11:30"
CSV = (b"datetime,t_air\n2024-04-21 09:30,10\n2024-04-21 10:00,\n2024-04-21 10:30,12\n"
       b"2024-04-21 11:00,13\n2024-04-21 11:30,\n2024-04-21 12:00,15\n")
COMBINED = {"fill_external": "1", "fill_model": "1", "timezone": "Europe/Moscow"}


class GapFillChecks(unittest.TestCase):
    def review(self, options):
        return create_report(dict(options, filename="station.csv"), CSV, "text/csv")

    def test_review_switches_and_legacy_modes(self):
        expected = {
            "none": [(None, None), (None, None)],
            "external": [("external", 7.74), (None, None)],
            "model": [("model", 11.0), ("model", 14.0)],
            "both": [("external", 7.74), ("model", 14.0)],
        }
        base = self.review({"fill": "none"})

        def result(report):
            rows = {row["time"]: row for row in report["rows"]}
            return [(rows[stamp].get("estimate_method"), rows[stamp].get("estimate")) for stamp in (FIRST, SECOND)]

        for mode, external, model in (("none", "0", "0"), ("external", "1", "0"),
                                      ("model", "0", "1"), ("both", "1", "1")):
            with self.subTest(mode=mode):
                options = {"fill_external": external, "fill_model": model}
                if external == "1":
                    options["timezone"] = COMBINED["timezone"]
                report = self.review(options)
                self.assertEqual(report["fill"], mode)
                self.assertEqual(result(report), expected[mode])
                self.assertEqual(report["summaries"], base["summaries"])
                self.assertEqual(report["counts"], base["counts"])
                if mode != "both":
                    self.assertEqual(result(self.review(dict(fill=mode, timezone=COMBINED["timezone"]))), expected[mode])

        combined = self.review(COMBINED)
        cached = report_page({"token": combined["token"], "offset": 0})
        self.assertEqual(cached["rows"], combined["rows"])
        self.assertEqual(cached["model"]["estimated_count"], 1)
        with ZipFile(BytesIO(report_excel({"token": combined["token"]})[1])) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        exported = {}
        for row in sheet.findall(".//{*}row"):
            cells = {cell.get("r").rstrip("0123456789"): cell for cell in row}
            time = cells["A"].findtext("{*}is/{*}t")
            if time in (FIRST, SECOND):
                exported[time] = (cells["D"].findtext("{*}v"), cells["J"].findtext("{*}is/{*}t"),
                                  cells["N"].findtext("{*}is/{*}t"))
        self.assertEqual(exported[FIRST][::2], ("Внешняя оценка", "По данным метеосервисов"))
        self.assertEqual(exported[SECOND][::2], ("Оценка модели", "По модели станции"))
        self.assertIn("Наблюдение 2024-04-21 11:00", exported[SECOND][1])

    def test_unavailable_or_out_of_range_external_falls_back(self):
        for options in ({"sources": "era5"}, {"ranges": {"t_air": [10, 15]}}):
            with self.subTest(options=options):
                report = self.review(dict(COMBINED, **options))
                rows = {row["time"]: row for row in report["rows"]}
                self.assertEqual(rows[FIRST]["estimate_method"], "model")
                self.assertEqual(rows[FIRST]["estimate"], 11.0)
                self.assertEqual(rows[SECOND]["estimate"], 14.0)
                self.assertIsNone(rows[FIRST]["value"])
                self.assertFalse(rows[FIRST]["keep"])

    def test_file_preview_xlsx_confirmation_and_correction_preserve_methods(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(Path(directory) / "observations.sqlite")
        ):
            uploaded = store.upload_file({"filename": "station.csv"}, CSV, "text/csv")
            query = dict(COMBINED, id=uploaded["id"])
            report = store.preview_file(query)
            request = dict(query, revision=report["revision"])
            self.assertEqual(report["settings"]["fill"], "both")
            self.assertEqual(report["stats"]["filled"], 2)
            self.assertEqual(report["external"]["t_air"]["estimated_count"], 1)
            self.assertEqual(report["model"]["t_air"]["estimated_count"], 1)
            self.assertEqual(store.get_file(request)["settings"], report["settings"])
            with self.assertRaisesRegex(ValueError, "Настройки или версия изменились"):
                store.confirm_file(dict(request, fill_model="0"))
            self.assertEqual(list(store.confirmed_rows("kr1", "2024-04-21", "2024-04-21")), [])
            xlsx = store.preview_excel(request)[1]
            with ZipFile(BytesIO(xlsx)) as archive:
                estimates = archive.read("xl/worksheets/sheet3.xml").decode()
            for method in ("По данным метеосервисов", "По модели станции", "station_bracket_interpolation"):
                self.assertIn(method, estimates)
            published = store.confirm_file(request)
            self.assertEqual(published["confirmed"]["settings"], report["settings"])
            before = {row["datetime"]: row for row in store.confirmed_rows("kr1", "2024-04-21", "2024-04-21")}
            self.assertEqual(before[FIRST]["estimates"]["t_air"]["method"], "external")
            self.assertEqual(before[SECOND]["estimates"]["t_air"]["method"], "model")
            corrected_csv = CSV.replace(b"2024-04-21 10:00,\n", b"2024-04-21 10:00,11.25\n")
            corrected = store.corrected_file(dict(request, filename="fixed.csv"), corrected_csv, "text/csv")
            self.assertEqual(corrected["settings"], report["settings"])
            self.assertEqual(corrected["external"]["t_air"]["estimated_count"], 0)
            self.assertEqual(corrected["model"]["t_air"]["estimated_count"], 1)
            store.confirm_file(dict(request, revision=corrected["revision"]))
            after = {row["datetime"]: row for row in store.confirmed_rows("kr1", "2024-04-21", "2024-04-21")}
            self.assertEqual(after[FIRST]["t_air"], 11.25)
            self.assertEqual(after[FIRST]["estimates"], {})
            self.assertEqual(after[SECOND]["estimates"], before[SECOND]["estimates"])

    def test_model_type_and_training_cutoff(self):
        first = datetime(2024, 4, 21)
        terms = []
        for index in range(50):
            stamp = (first + timedelta(hours=index)).isoformat(" ", timespec="minutes")
            value = None if index in (4, 29) else index / 4
            terms.append({"time": stamp, "keep": value is not None, "value": value})
        estimates, _ = station_estimates(terms, "t_air")
        modified = [dict(point, value=(20 + index % 2)) if index > 30 else point
                    for index, point in enumerate(terms)]
        different_future, _ = station_estimates(modified, "t_air")
        early, later = (terms[index]["time"] for index in (4, 29))
        self.assertEqual(estimates[early], different_future[early])
        self.assertEqual(estimates[later], different_future[later])
        self.assertIsNone(estimates[early]["provenance"]["training_end"])
        self.assertLess(estimates[later]["provenance"]["training_end"], later)
        self.assertGreaterEqual(estimates[later]["provenance"]["training_n"], 10)
        self.assertEqual(station_estimates(terms, "wind_dir")[1]["reason"], "unsupported_direction")


if __name__ == "__main__":
    unittest.main()
