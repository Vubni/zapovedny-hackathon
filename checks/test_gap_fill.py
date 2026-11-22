"""Regression checks for two independent fill methods and their exported lineage."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import observation_store as store
from checks.fixture_weather import april_response
from gap_fill import station_estimates, verification_history, verify_estimates
from review_service import create_report, report_excel, report_page


FIRST = "2024-04-21 10:00"
SECOND = "2024-04-21 11:30"
CSV = (b"datetime,t_air\n2024-04-21 09:30,10\n2024-04-21 10:00,\n2024-04-21 10:30,12\n"
       b"2024-04-21 11:00,13\n2024-04-21 11:30,\n2024-04-21 12:00,15\n")
COMBINED = {"fill_external": "1", "fill_model": "1", "timezone": "Europe/Moscow"}


class GapFillChecks(unittest.TestCase):
    def setUp(self):
        offline = patch("external_fill.urlopen", side_effect=april_response)
        offline.start()
        self.addCleanup(offline.stop)

    def review(self, options):
        return create_report(dict(options, filename="station.csv"), CSV, "text/csv")

    def test_external_sources_request_field_and_clearing_separately(self):
        for station, latitude, longitude in (("kr1", 56.464310, 32.970290), ("kr2", 56.463800, 32.970370)):
            with self.subTest(station=station):
                external = self.review(dict(COMBINED, station=station))["external"]
                self.assertEqual(external["requested_coordinates"], {"latitude": latitude, "longitude": longitude})
                self.assertEqual({source["id"] for source in external["sources"]}, {"era5", "merra2"})
                for source in external["sources"]:
                    point = parse_qs(urlsplit(source["url"]).query)
                    self.assertEqual((float(point["latitude"][0]), float(point["longitude"][0])), (latitude, longitude))

    def test_review_switches_and_legacy_modes(self):
        expected = {
            "none": [(None, None), (None, None)],
            "external": [(None, None), (None, None)],
            "model": [("model", 11.0), ("model", 14.0)],
            "both": [("model", 11.0), ("model", 14.0)],
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
        self.assertEqual(cached["model"]["estimated_count"], 2)
        rows = {row["time"]: row for row in cached["rows"]}
        self.assertIsNotNone(rows[FIRST]["external_reference"]["value"])
        self.assertIsNone(rows[SECOND]["external_reference"])
        self.assertEqual(rows[FIRST]["verification"]["delta"], 0)
        with ZipFile(BytesIO(report_excel({"token": combined["token"]})[1])) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        exported = {}
        for row in sheet.findall(".//{*}row"):
            cells = {cell.get("r").rstrip("0123456789"): cell for cell in row}
            time = cells["A"].findtext("{*}is/{*}t")
            if time in (FIRST, SECOND):
                exported[time] = (cells["D"].findtext("{*}v"), float(cells["C"].findtext("{*}v")))
        self.assertEqual(exported[FIRST], ("Оценка модели", 11.0))
        self.assertEqual(exported[SECOND], ("Оценка модели", 14.0))

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

    def test_fill_scope_limits_only_external_queries_and_model_targets(self):
        csv = (b"datetime,t_air,rh\n2024-03-01 00:00,5,50\n2024-04-21 09:00,10,60\n"
               b"2024-04-21 10:00,,\n2024-04-21 11:00,12,62\n2024-06-01 00:00,8,80\n")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(Path(directory) / "observations.sqlite")
        ):
            uploaded = store.upload_file({"filename": "station.csv"}, csv, "text/csv")
            query = dict(COMBINED, id=uploaded["id"])
            with self.assertRaisesRegex(ValueError, "до 31 суток"):
                store.preview_file(query)
            scope = dict(fill_start="2024-04-21", fill_end="2024-04-21", fill_params="t_air")
            report = store.preview_file(dict(query, **scope))
            self.assertEqual(set(report["external"]), {"t_air"})
            provider = report["external"]["t_air"]
            self.assertEqual(provider["target_start"], "2024-04-21")
            self.assertEqual(provider["target_end"], "2024-04-21")
            self.assertEqual(provider["location"]["precision"], "area")
            self.assertEqual(provider["location"]["label"], "Красное")
            self.assertIn("26.07.2019", provider["location"]["reference"])
            self.assertEqual(provider["requested_coordinates"], {"latitude": 56.46431, "longitude": 32.97029})
            self.assertEqual(report["stats"]["parameters"]["rh"]["filled"], 0)
            self.assertEqual({row["time"] for row in report["timeline"]}, {"2024-03-01", "2024-04-21", "2024-06-01"})
            reviewed = create_report(dict(COMBINED, **scope, filename="station.csv"), csv, "text/csv")
            self.assertEqual((reviewed["start"], reviewed["end"]), ("2024-03-01", "2024-06-01"))
            self.assertEqual(reviewed["external"]["target_start"], "2024-04-21")
            with self.assertRaisesRegex(ValueError, "Настройки или версия изменились"):
                store.confirm_file(dict(query, revision=report["revision"], **(scope | {"fill_params": "rh"})))

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
            self.assertEqual(report["external"]["t_air"]["estimated_count"], 0)
            self.assertEqual(report["external"]["t_air"]["reference_count"], 1)
            self.assertEqual(report["model"]["t_air"]["estimated_count"], 2)
            self.assertEqual(store.get_file(request)["settings"], report["settings"])
            with self.assertRaisesRegex(ValueError, "Настройки или версия изменились"):
                store.confirm_file(dict(request, fill_model="0"))
            self.assertEqual(list(store.confirmed_rows("kr1", "2024-04-21", "2024-04-21")), [])
            xlsx = store.preview_excel(request)[1]
            with ZipFile(BytesIO(xlsx)) as archive:
                estimates = archive.read("xl/worksheets/sheet3.xml").decode()
                references = archive.read("xl/worksheets/sheet4.xml").decode()
            for method in ("Модель станции", "station_bracket_interpolation"):
                self.assertIn(method, estimates)
            self.assertIn("ERA5", references)
            published = store.confirm_file(request)
            self.assertEqual(published["confirmed"]["settings"], report["settings"])
            before = {row["datetime"]: row for row in store.confirmed_rows("kr1", "2024-04-21", "2024-04-21")}
            self.assertEqual(before[FIRST]["estimates"]["t_air"]["method"], "model")
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

    def test_adjustment_requires_useful_causal_calibration(self):
        start = datetime(2024, 4, 1)
        times = [(start + timedelta(hours=index)).isoformat(" ", timespec="minutes") for index in range(130)]
        target_index = 100
        actual = [10 + (index % 2) * 0.4 for index in range(len(times))]
        rows = [{"time": stamp, "keep": index != target_index,
                 "value": actual[index] if index != target_index else None} for index, stamp in enumerate(times)]
        target = times[target_index]

        def reference(stamp, value):
            utc = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            return {"value": value, "source": "ERA5 + MERRA-2", "usable": True,
                    "provenance": {"start_utc": utc.isoformat(), "known_after_utc": (utc + timedelta(hours=1)).isoformat(),
                                   "sources": [{"id": "era5", "value": value - 0.05},
                                               {"id": "merra2", "value": value + 0.05}]}}

        local, _ = station_estimates(rows, "t_air")
        target_refs = {target: reference(target, actual[target_index])}
        histories = [verification_history(rows, {stamp: reference(stamp, value)
                                                 for index, (stamp, value) in enumerate(zip(times, actual))
                                                 if index != target_index}, "t_air")]
        self.assertGreaterEqual(len(histories[0]), 20)
        corrected, decision = verify_estimates(local, target_refs, "t_air", history=histories[0])
        self.assertEqual(corrected[target]["method"], "model_adjusted")
        self.assertTrue(decision[target]["calibration"]["accepted"])
        self.assertLess(abs(corrected[target]["value"] - actual[target_index]), abs(local[target]["value"] - actual[target_index]))
        self.assertLess(decision[target]["calibration"]["latest_known_utc"], target_refs[target]["provenance"]["start_utc"])
        harmful = [(stamp, corrected_error, initial_error) for stamp, initial_error, corrected_error in histories[0]]
        no_past = [entry for entry in histories[0] if entry[0] >= datetime.fromisoformat(target).replace(tzinfo=timezone.utc)]
        for history in (harmful, no_past):
            unchanged, rejected = verify_estimates(local, target_refs, "t_air", history=history)
            self.assertEqual(unchanged[target]["value"], local[target]["value"])
            self.assertEqual(rejected[target]["decision"], "unvalidated_correction")


if __name__ == "__main__":
    unittest.main()
