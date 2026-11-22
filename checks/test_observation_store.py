import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch
from zipfile import ZipFile
from io import BytesIO
from xml.etree import ElementTree as ET

import observation_store as store
from review_excel import BAD, GOOD
from review_upload import read_upload
from station_catalog import catalog, register_station


def csv(*lines, fields="datetime,t_air,rh"):
    return ("\n".join((fields, *lines)) + "\n").encode()


class ObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = str(Path(self.directory.name) / ".local" / "test.sqlite3")
        patcher = patch.dict(os.environ, METEO_OBSERVATION_DB=database)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, body, name="weather.csv", station="kr1"):
        return store.upload_file({"filename": name, "station": station}, body, "application/octet-stream")

    def review(self, file, **settings):
        query = dict(id=file["id"], station=file["station"], **settings)
        report = store.preview_file(query)
        return report, dict(query, revision=report["revision"])

    def rows(self, station="kr1"):
        return list(store.confirmed_rows(station, date(2024, 4, 1), "2024-04-30"))

    def assert_timeline_fills(self, report, expected):
        timeline = report["timeline"]
        self.assertEqual({entry["time"]: entry["filled_parameters"] for entry in timeline}, expected)
        for entry in timeline:
            self.assertEqual(sum(entry["filled_parameters"].values()), entry["filled"])
        self.assertEqual(sum(entry["filled"] for entry in timeline), report["stats"]["filled"])
        for param, stats in report["stats"]["parameters"].items():
            self.assertEqual(sum(entry["filled_parameters"].get(param, 0) for entry in timeline), stats["filled"])

    def test_second_file_and_workbook_lifecycle(self):
        self.assertEqual(store.list_files()["files"], [])
        self.assertFalse(Path(os.environ["METEO_OBSERVATION_DB"]).exists())
        first_bytes = csv("2024-04-01 00:00,-100,70", "2024-04-01 01:00,8,66")
        first = self.upload(first_bytes, "поляна.csv")
        report, request = self.review(first)
        self.assertEqual(report["stats"]["marked"], 1)
        self.assertEqual(report["stats"]["parameters"]["rh"]["kept"], 2)
        self.assertEqual([param["id"] for param in report["params"]], ["t_air", "rh"])
        self.assertEqual(store.get_file(request)["revision"], report["revision"])
        self.assertEqual(self.rows(), [])
        _, workbook = store.preview_excel(request)
        with ZipFile(BytesIO(workbook)) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertEqual(sheet.find(".//s:c[@r='B2']", ns).get("s"), str(BAD))
            self.assertEqual(sheet.find(".//s:c[@r='C2']", ns).get("s"), str(GOOD))
            self.assertEqual(sheet.find(".//s:c[@r='C2']/s:v", ns).text, "70.0")
        second = self.upload(workbook, "поле.xlsx", "kr2")
        self.assertEqual(second["rows"], 2)
        _, second_request = self.review(second)
        self.assertIsNone(store.list_files()["files"][0]["confirmed"])
        original = store.confirm_file(request)
        self.assertEqual(store.confirm_file(request)["version"], original["version"])
        store.confirm_file(second_request)
        self.assertNotIn("t_air", self.rows()[0])
        self.assertEqual(self.rows()[0]["rh"], 70.0)
        self.assertEqual(len(self.rows("kr2")), 2)

        report, request_new = self.review(first, filters="spike")
        self.assertEqual(report["file"]["confirmed"]["revision"], request["revision"])
        self.assertEqual(self.rows()[1]["t_air"], 8.0)
        with self.assertRaises(ValueError):
            store.confirm_file(request)
        with self.assertRaises(ValueError):
            store.confirm_file(dict(request_new, filters="hampel"))
        replaced = store.confirm_file(request_new)
        self.assertEqual(replaced["version"], 2)
        with self.assertRaises(ValueError):
            store.discard_clean(dict(request_new, version=original["version"]))
        discarded = store.discard_clean(dict(request_new, version=replaced["version"]))
        self.assertEqual(discarded["status"], "original")
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows("kr2")), 2)
        self.assertEqual(store.original_file(request), ("поляна.csv", "text/csv; charset=utf-8", first_bytes))
        self.assertEqual(store.original_file(second_request)[2], workbook)
        _, again = self.review(first)
        self.assertEqual(store.confirm_file(again)["version"], 4)

    def test_newest_file_replaces_its_declared_parameter_including_rejects(self):
        first = self.upload(csv("2024-04-01 00:15,5,90", "2024-04-01 00:30,6,91"))
        second = self.upload(csv("2024-04-01 00:15,-100", fields="datetime,t_air"))
        _, first_request = self.review(first)
        _, second_request = self.review(second)
        store.confirm_file(first_request)
        published = store.confirm_file(second_request)
        rejected, good = self.rows()
        self.assertEqual(rejected, {
            "datetime": "2024-04-01 00:15", "synthetic": False, "estimates": {},
            "declared": ["t_air", "rh"], "rh": 90.0,
        })
        self.assertEqual(good["t_air"], 6.0)
        self.assertEqual(store.confirmed_days("kr1", "2024-04-01", "2024-04-02"), {"2024-04-01"})
        self.assertEqual(store.date_bounds("kr1"), ("2024-04-01", "2024-04-01"))
        self.assertEqual(store.all_date_bounds(), {"kr1": ("2024-04-01", "2024-04-01")})
        store.discard_clean({"id": second["id"], "version": published["version"]})
        self.assertEqual(self.rows()[0]["t_air"], 5.0)

    def test_disjoint_csv_and_legacy_declarations_merge_at_the_same_time(self):
        first = self.upload(csv("2024-04-01 00:15,5", fields="datetime,t_air"))
        second = self.upload(csv("2024-04-01 00:15,93", fields="datetime,rh"))
        _, first_request = self.review(first)
        _, second_request = self.review(second)
        store.confirm_file(first_request)
        store.confirm_file(second_request)
        for include_meta in (True, False):
            with store._database(write=True) as db:
                source = json.loads(db.execute("SELECT data FROM published WHERE file_id=?", (first["id"],)).fetchone()[0])
                source.pop("declared", None)
                db.execute("UPDATE published SET data=? WHERE file_id=?", (json.dumps(source), first["id"]))
                if not include_meta:
                    summary = json.loads(db.execute("SELECT confirmed_meta FROM files WHERE id=?",
                                                    (first["id"],)).fetchone()[0])
                    summary.pop("params")
                    db.execute("UPDATE files SET confirmed_meta=? WHERE id=?", (json.dumps(summary), first["id"]))
            result = self.rows()[0]
            self.assertEqual((result["t_air"], result["rh"], result["declared"]), (5, 93, ["t_air", "rh"]))

    def test_error_keeps_original_and_does_not_affect_valid_queue(self):
        source = csv("bad,8,66")
        invalid = self.upload(source, "ошибка.csv")
        valid = self.upload(csv("2024-04-02 04:00,8,66"))
        self.assertEqual(invalid["status"], "error")
        self.assertIn("срок", invalid["error"])
        self.assertEqual(store.original_file(invalid)[2], source)
        self.assertEqual(valid["status"], "uploaded")
        with self.assertRaises(ValueError):
            store.preview_file({"id": invalid["id"]})

    def test_one_cell_two_filters_and_primary_flags(self):
        source = csv("2024-04-01 00:00,10,65,", "2024-04-01 01:00,10,65,",
                     "2024-04-01 02:00,10,65,", "2024-04-01 03:00,27,65,",
                     "2024-04-01 04:00,10,65,", "2024-04-01 05:00,10,65,",
                     "2024-04-01 06:00,10,65,rh:range",
                     "2024-04-01 07:00,10,65,dup_conflict", fields="datetime,t_air,rh,qc_flag")
        file = self.upload(source)
        report, request = self.review(file, filters="hampel,spike")
        self.assertEqual(report["stats"]["marked"], 4)
        anomaly = report["points"]["t_air"][3]
        self.assertTrue(anomaly["marked"])
        self.assertEqual(len(anomaly["reasons"]), 2)
        store.confirm_file(request)
        self.assertEqual(self.rows()[3]["rh"], 65.0)
        self.assertNotIn("rh", self.rows()[6])
        self.assertNotIn("t_air", self.rows()[7])

    def test_pressure_threshold_preserves_source_flags(self):
        file = self.upload(csv(
            "2024-04-01 00:00,1100,", "2024-04-01 01:00,1600,",
            "2024-04-01 02:00,1100,pressure_hpa:range", "2024-04-01 03:00,1000,pressure_hpa:fault",
            "2024-04-01 04:00,990,", fields="datetime,pressure_hpa,qc_flag",
        ))
        primary, _ = self.review(file)
        self.assertEqual(primary["settings"]["pressure_max"], "1050")
        self.assertEqual(primary["stats"]["marked"], 4)
        with self.assertRaisesRegex(ValueError, "1050 или 1550"):
            self.review(file, pressure_max="1500")
        extended, request = self.review(file, pressure_max="1550")
        self.assertEqual(extended["stats"]["marked"], 3)
        self.assertTrue(any("1550 гПа" in warning for warning in extended["warnings"]))
        with self.assertRaisesRegex(ValueError, "Настройки или версия"):
            store.confirm_file(dict(request, pressure_max="1050"))
        store.confirm_file(request)
        self.assertEqual([row.get("pressure_hpa") for row in self.rows()], [1100, None, None, None, 990])
        _, workbook = store.preview_excel(request)
        with ZipFile(BytesIO(workbook)) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertEqual(sheet.find(".//s:c[@r='B2']", ns).get("s"), str(GOOD))
            self.assertEqual(sheet.find(".//s:c[@r='B2']/s:v", ns).text, "1100.0")
            self.assertEqual(sheet.find(".//s:c[@r='B3']", ns).get("s"), str(BAD))

    def test_legacy_pressure_settings_survive_confirmation_and_retry(self):
        file = self.upload(csv("2024-04-01 00:00,1100", fields="datetime,pressure_hpa"))
        _, request = self.review(file)

        def strip_pressure_max(column):
            with store._database(write=True) as db:
                summary = json.loads(db.execute(f"SELECT {column} FROM files WHERE id=?", (file["id"],)).fetchone()[0])
                del summary["settings"]["pressure_max"]
                db.execute(f"UPDATE files SET {column}=? WHERE id=?", (json.dumps(summary), file["id"]))

        strip_pressure_max("draft_meta")
        self.assertEqual(store.get_file(request)["settings"]["pressure_max"], "1050")
        accepted = store.confirm_file(request)
        self.assertEqual(accepted["confirmed"]["settings"]["pressure_max"], "1050")
        self.assertNotIn("pressure_hpa", self.rows()[0])
        strip_pressure_max("confirmed_meta")
        self.assertEqual(store.confirm_file(request)["version"], 1)
        self.assertEqual(store.list_files()["files"][0]["settings"]["pressure_max"], "1050")

    def test_numeric_limits_saved_confirmed_and_exported(self):
        original = csv(
            "2024-04-01 00:00,-7,50,0,1099.5,", "2024-04-01 01:00,-8.1,92,12,1099.6,",
            "2024-04-01 02:00,10.25,68,10,1099.5,pressure_hpa:range",
            fields="datetime,t_air,rh,wind_speed,pressure_hpa,qc_flag",
        )
        file = self.upload(original)
        limits = {"t_air": [-8, 10.25], "rh": [0, 90], "wind_speed": [0, 10], "pressure_hpa": [940, 1099.5]}
        options = {"ranges": limits, "thresholds": {"spike": {"t_air": 4.75}}, "filters": "spike"}
        preview, request = self.review(file, **options)
        settings = preview["settings"]
        for param, expected in limits.items():
            self.assertEqual(settings["ranges"][param], expected)
        self.assertEqual(settings["thresholds"]["spike"]["t_air"], 4.75)
        self.assertEqual(preview["stats"]["marked"], 5)
        self.assertEqual(store.get_file(request)["settings"], settings)
        with self.assertRaisesRegex(ValueError, "Настройки или версия"):
            store.confirm_file(dict(request, ranges={**limits, "rh": [0, 100]}))
        with self.assertRaisesRegex(ValueError, "Настройки или версия"):
            store.confirm_file(dict(request, thresholds={"spike": {"t_air": 5}}))
        store.confirm_file(request)
        rows = self.rows()
        self.assertEqual([row.get("pressure_hpa") for row in rows], [1099.5, None, None])
        self.assertEqual([row.get("t_air") for row in rows], [-7, None, 10.25])
        self.assertEqual(store.original_file(file)[2], original)
        _, xlsx = store.preview_excel(request)
        with ZipFile(BytesIO(xlsx)) as workbook:
            self.assertIn("Настройки контроля".encode(), workbook.read("xl/workbook.xml"))
            table = ET.fromstring(workbook.read("xl/worksheets/sheet4.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for address, value in {"C2": -8, "D2": 10.25, "F2": 4.75, "D5": 1099.5}.items():
            self.assertEqual(float(table.findtext(f".//s:c[@r='{address}']/s:v", namespaces=ns)), value)

    def test_numeric_ranges_apply_to_csv_corrections(self):
        file = self.upload(csv("2024-04-01 00:00,-7", fields="datetime,t_air"))
        _, request = self.review(file, ranges={"t_air": [-8.25, 10.25]})
        upload = dict(request, filename="correction.csv")
        outside = csv("2024-04-01 00:00,10.26", fields="datetime,t_air")
        with self.assertRaisesRegex(ValueError, "-8.25–10.25"):
            store.corrected_file(upload, outside, "text/csv")
        inside = csv("2024-04-01 00:00,10.25", fields="datetime,t_air")
        corrected = store.corrected_file(upload, inside, "text/csv")
        self.assertEqual(corrected["settings"]["ranges"]["t_air"], [-8.25, 10.25])
        store.confirm_file(dict(request, revision=corrected["revision"]))
        self.assertEqual(self.rows()[0]["t_air"], 10.25)

    def test_retired_filter_draft_remains_readable_and_confirmable(self):
        file = self.upload(csv("2024-04-01 00:00,-7,60"))
        initial, request = self.review(file)
        with store._database(write=True) as db:
            entry = db.execute("SELECT draft_meta FROM files WHERE id=?", (file["id"],)).fetchone()
            summary = json.loads(entry[0])
            summary["settings"]["filters"] = ["buddy"]
            for key in ("ranges", "thresholds", "pressure_max"):
                del summary["settings"][key]
            db.execute("UPDATE files SET draft_meta=? WHERE id=?", (json.dumps(summary), file["id"]))
        saved = store.get_file(request)
        self.assertEqual(saved["settings"]["filters"], ["buddy"])
        self.assertEqual(saved["settings"]["ranges"]["t_air"], [-50, 45])
        self.assertEqual(saved["points"], initial["points"])
        self.assertNotIn("buddy", [item["id"] for item in store.list_files()["filters"]])
        with self.assertRaisesRegex(ValueError, "фильтры"):
            self.review(file, filters="buddy")
        confirm = dict(request, filters="buddy")
        self.assertEqual(store.confirm_file(confirm)["version"], 1)
        self.assertEqual(store.confirm_file(confirm)["version"], 1)
        self.assertEqual(self.rows()[0]["t_air"], -7)

    def test_unflagged_duplicates_are_audited_and_resolved_only_if_requested(self):
        file = self.upload(csv("2024-04-01 00:00,-100,50", "2024-04-01 00:00,5,65", "2024-04-01 00:00,7,80"))
        report, initial = self.review(file)
        self.assertEqual(report["stats"]["marked"], 1)
        self.assertTrue(any("первую пригодную" in text for text in report["warnings"]))
        self.assertEqual([point["value"] for point in report["points"]["rh"]], [50, 65, 80])
        store.confirm_file(initial)
        result = self.rows()[0]
        self.assertEqual((result["t_air"], result["rh"]), (5, 50))
        _, workbook = store.preview_excel(initial)
        with ZipFile(BytesIO(workbook)) as archive:
            self.assertIn("Повторы сроков".encode(), archive.read("xl/workbook.xml"))
            self.assertIn(b"<v>80.0</v>", archive.read("xl/worksheets/sheet4.xml"))
        strict, final = self.review(file, filters="conflicts")
        self.assertEqual(strict["stats"]["marked"], 6)
        store.confirm_file(final)
        self.assertEqual(self.rows()[0]["declared"], ["t_air", "rh"])
        self.assertNotIn("t_air", self.rows()[0])
        self.assertNotIn("rh", self.rows()[0])

    def test_model_separates_estimates_from_observations_and_validates(self):
        origin = datetime(2024, 4, 1)
        lines = []
        for index in range(60):
            stamp = (origin + timedelta(hours=index)).isoformat(" ", timespec="minutes")
            if index == 14:
                continue
            lines.append(f"{stamp},{'' if index == 10 else index / 4},{150 if index == 30 else 64}")
        file = self.upload(csv(*lines))
        report, request = self.review(file, fill="model")
        self.assertEqual(report["stats"]["parameters"]["t_air"]["filled"], 2)
        self.assert_timeline_fills(report, {
            "2024-04-01": {"t_air": 2, "rh": 1}, "2024-04-02": {"rh": 1}, "2024-04-03": {},
        })
        self.assertEqual(report["timeline"][1]["parameters"], {"rh": 1})
        self.assertGreaterEqual(report["model"]["t_air"]["test_n"], 3)
        self.assertEqual(report["model"]["t_air"]["mae"], 0)
        store.confirm_file(request)
        by_time = {row["datetime"]: row for row in self.rows()}
        original_gap = by_time["2024-04-01 10:00"]
        synthetic_gap = by_time["2024-04-01 14:00"]
        self.assertFalse(original_gap["synthetic"])
        self.assertTrue(synthetic_gap["synthetic"])
        self.assertNotIn("t_air", original_gap)
        self.assertEqual(original_gap["estimates"]["t_air"]["value"], 2.5)
        self.assertEqual(synthetic_gap["estimates"]["t_air"]["value"], 3.5)
        measured = self.upload(csv("2024-04-01 14:00,21,50"))
        _, measured_request = self.review(measured)
        store.confirm_file(measured_request)
        _, new_model_request = self.review(file, fill="model")
        store.confirm_file(new_model_request)
        at_fourteen = next(row for row in self.rows() if row["datetime"] == synthetic_gap["datetime"])
        self.assertFalse(at_fourteen["synthetic"])
        self.assertEqual(at_fourteen["t_air"], 21)

    def test_external_requires_explicit_zone_and_keeps_source_confidence(self):
        file = self.upload(csv("2024-04-15 14:00,8,65", "2024-04-15 15:00,,105", "2024-04-15 16:00,8,65"))
        with self.assertRaisesRegex(ValueError, "часовой пояс"):
            self.review(file, fill="external")
        report, request = self.review(file, fill="external", timezone="Europe/Moscow")
        self.assert_timeline_fills(report, {"2024-04-15": {"t_air": 1, "rh": 1}})
        self.assertEqual(report["timeline"][0]["parameters"], {"rh": 1})
        self.assertEqual(store.get_file(request)["timeline"], report["timeline"])
        point = report["points"]["t_air"][1]
        self.assertIsNotNone(point["estimate"])
        self.assertEqual(point["estimate"]["confidence"]["source_count"], 2)
        self.assertIn("ERA5", point["estimate"]["source"])
        store.confirm_file(request)
        self.assertNotIn("t_air", self.rows()[1])
        self.assertTrue(self.rows()[1]["estimates"]["t_air"]["enters_working_graphs"])

    def test_corrected_csv_preserves_original_parent_and_confirmation_boundary(self):
        original = csv("2024-04-01 00:00,7.123456789012345,61", "2024-04-02 01:00,8,66")
        file = self.upload(original, "Станция.csv")
        initial, parent = self.review(file, filters="hampel")
        store.confirm_file(parent)
        name, exported = store.draft_csv(parent)
        self.assertEqual(name, "Станция-draft.csv")
        self.assertIn(b"7.123456789012345;61.0", exported)
        edited = (
            "datetime;rh;t_air\n"
            "02.04.2024 01:00;;8\n"
            "2024-04-01 00:00;61;7,987654321098765\n"
        ).encode("utf-8-sig")
        upload = dict(parent, filename="Точные исправления.csv")
        report = store.corrected_file(upload, edited, "text/csv")
        self.assertEqual(report["manual"]["parent_revision"], initial["revision"])
        self.assertEqual(report["manual"]["parameters"], {"t_air": 1, "rh": 1})
        self.assertEqual(report["settings"], initial["settings"])
        self.assertNotEqual(report["revision"], initial["revision"])
        self.assertEqual(report["file"]["version"], 1)
        self.assertEqual([row["t_air"] for row in self.rows()], [7.123456789012345, 8])
        self.assertEqual(self.rows()[1]["rh"], 66)
        self.assertEqual(report["timeline"][0]["manual_parameters"], {"t_air": 1})
        self.assertEqual(report["timeline"][1]["manual"], 1)
        self.assertIsNone(report["points"]["rh"][1]["manual_value"])
        with self.assertRaisesRegex(ValueError, "Версия черновика изменилась"):
            store.corrected_file(upload, edited, "text/csv")
        corrected = dict(parent, revision=report["revision"])
        self.assertIn(b"7.987654321098765", store.draft_csv(corrected)[1])
        store.confirm_file(corrected)
        self.assertEqual([row["t_air"] for row in self.rows()], [7.987654321098765, 8])
        self.assertNotIn("rh", self.rows()[1])
        self.assertEqual(store.original_file(file)[2], original)
        with store._database() as db:
            lineage = db.execute("SELECT * FROM file_corrections WHERE file_id=?", (file["id"],)).fetchone()
            self.assertEqual(lineage["parent_revision"], initial["revision"])
            self.assertEqual(lineage["original_csv"], edited)
            self.assertEqual(store._unpack(lineage["parent_result"])["rows"][0]["t_air"], 7.123456789012345)

        recalculated = store.preview_file(parent)
        self.assertNotIn("manual", recalculated)
        self.assertEqual(recalculated["points"]["t_air"][0]["value"], 7.123456789012345)
        self.assertEqual(self.rows()[0]["t_air"], 7.987654321098765)

    def test_correction_replaces_only_its_automatic_estimate(self):
        file = self.upload(csv("2024-04-15 14:00,8,65", "2024-04-15 15:00,-100,", "2024-04-15 16:00,8,65"))
        parent, request = self.review(file, fill="external", timezone="Europe/Moscow")
        self.assertEqual(parent["stats"]["filled"], 2)
        _, working = store.draft_csv(request)
        self.assertIn(b"2024-04-15 15:00;;", working)
        edited = working.replace(b"2024-04-15 15:00;;", b"2024-04-15 15:00;8.25;")
        report = store.corrected_file(dict(request, filename="corrected.csv"), edited, "text/csv")
        self.assertEqual(report["stats"]["marked"], 1)
        self.assert_timeline_fills(report, {"2024-04-15": {"rh": 1}})
        point = report["points"]["t_air"][1]
        self.assertEqual(point["value"], -100)
        self.assertTrue(point["marked"])
        self.assertTrue(point["manual"])
        self.assertEqual(point["manual_value"], 8.25)
        self.assertIsNone(point["estimate"])
        self.assertEqual(self.rows(), [])
        store.confirm_file(dict(request, revision=report["revision"]))
        self.assertEqual(self.rows()[1]["t_air"], 8.25)
        self.assertNotIn("t_air", self.rows()[1]["estimates"])
        self.assertIn("rh", self.rows()[1]["estimates"])

    def test_correction_rejects_invalid_number_changed_schema_or_missing_dates(self):
        file = self.upload(csv("2024-04-01 00:00,5,60", "2024-04-01 01:00,6,61"))
        parent, request = self.review(file)
        request["filename"] = "corrected.csv"
        _, exported = store.draft_csv(request)
        bad_inputs = (
            (exported, "нет изменений"),
            (exported.replace(b"5.0", b"55"), "введите число"),
            (exported.replace(b"01:00", b"00:00"), "повторяющийся или новый срок"),
            (exported.splitlines(keepends=True)[0] + exported.splitlines(keepends=True)[1], "отсутствуют сроки"),
            (exported.replace(b";rh", b";wind_speed"), "Столбцы исправленного CSV"),
        )
        for content, error in bad_inputs:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                store.corrected_file(request, content, "text/csv")
            self.assertEqual(store.get_file(request)["revision"], parent["revision"])
        self.assertEqual(self.rows(), [])

    def test_repeat_correction_keeps_manual_points_outside_base_sample(self):
        source = csv(*(f"2024-04-01 {hour:02d}:00,10,60" for hour in range(18)))
        file = self.upload(source)
        with patch.object(store, "POINT_BUDGET", 9):
            parent, request = self.review(file)
            sampled = {point["time"] for point in parent["points"]["t_air"]}
            stamp = next(f"2024-04-01 {hour:02d}:00" for hour in range(18)
                         if f"2024-04-01 {hour:02d}:00" not in sampled)
            _, exported = store.draft_csv(request)
            first_body = exported.replace(f"{stamp};10.0;".encode(), f"{stamp};11.25;".encode())
            first_query = dict(request, filename="correction.csv")
            first = store.corrected_file(first_query, first_body, "text/csv")
            point = next(point for point in first["points"]["t_air"] if point.get("manual"))
            self.assertEqual((point["time"], point["value"], point["previous_value"], point["manual_value"]),
                             (stamp, 10, 10, 11.25))
            second_body = first_body.replace(f"{stamp};11.25;".encode(), f"{stamp};12.375;".encode())
            second = store.corrected_file(dict(first_query, revision=first["revision"]), second_body, "text/csv")
            points = [point for point in second["points"]["t_air"] if point.get("manual")]
            self.assertEqual(len(points), 1)
            self.assertEqual((points[0]["value"], points[0]["previous_value"], points[0]["manual_value"]),
                             (10, 11.25, 12.375))
            self.assertEqual(second["manual"]["parent_revision"], first["revision"])
            self.assertEqual((second["manual"]["changed"], second["manual"]["applied"]), (1, 1))

    def test_new_station_profile_and_independent_geographic_identity(self):
        station = register_station({"id": "test-field", "label": "Пункт проверки", "profile": "ame60"})["station"]
        self.assertEqual(store.list_files()["files"], [])
        file = self.upload(csv("2024-04-02 04:15,5.5", fields="datetime,soil_10"), station=station["id"])
        report, request = self.review(file, filters="hampel")
        self.assertEqual(report["params"][0]["id"], "soil_10")
        store.confirm_file(request)
        self.assertEqual(self.rows(station["id"])[0]["soil_10"], 5.5)
        published = next(item for item in catalog() if item["id"] == station["id"])
        self.assertEqual(published["start"], "2024-04-02")
        with self.assertRaisesRegex(ValueError, "координаты"):
            self.review(file, fill="external", timezone="UTC")

    def test_external_uses_only_the_registered_station_coordinates(self):
        station = register_station({
            "id": "test-north", "label": "Северный пункт", "profile": "kr2",
            "latitude": 64.321, "longitude": 31.543,
        })["station"]
        file = self.upload(csv("2024-04-02 04:15,", fields="datetime,t_air"), station=station["id"])

        def fetch(report, request):
            report["external"] = {"requested_coordinates": {
                "latitude": request["latitude"], "longitude": request["longitude"],
            }}

        with patch.object(store, "apply_estimates", side_effect=fetch) as external:
            report, _ = self.review(file, fill="external", timezone="UTC", latitude="1", longitude="2")
        self.assertEqual(external.call_count, 1)
        self.assertEqual(report["external"]["t_air"]["requested_coordinates"],
                         {"latitude": 64.321, "longitude": 31.543})

    def test_newer_preview_wins_when_older_request_finishes_last(self):
        file = self.upload(csv("2024-04-01 00:00,5,90"))
        started, resume = Event(), Event()
        review = store._review

        def wait_for_second(source, fields, filename, settings, *args):
            if settings["filters"] == ["spike"]:
                started.set()
                if not resume.wait(5):
                    raise RuntimeError("Второй расчёт не завершён")
            return review(source, fields, filename, settings, *args)

        with patch.object(store, "_review", side_effect=wait_for_second), ThreadPoolExecutor() as executor:
            running = executor.submit(self.review, file, filters="spike")
            self.assertTrue(started.wait(5))
            try:
                report, accepted = self.review(file, filters="hampel")
            finally:
                resume.set()
            with self.assertRaisesRegex(ValueError, "новый расчёт"):
                running.result()
        self.assertEqual(store.get_file(accepted)["revision"], report["revision"])
        self.assertEqual(store.confirm_file(accepted)["version"], 1)

    def test_atomic_replace_leaves_previous_version_on_failed_write(self):
        file = self.upload(csv("2024-04-01 00:00,5,90"))
        _, initial = self.review(file)
        store.confirm_file(initial)
        _, pending = self.review(file, filters="spike")
        with store._database(write=True) as db:
            db.execute("CREATE TRIGGER test_failure BEFORE INSERT ON published "
                       "BEGIN SELECT RAISE(ABORT, 'test replace failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.confirm_file(pending)
        record = store.list_files()["files"][0]
        self.assertEqual(record["confirmed"]["revision"], initial["revision"])
        self.assertEqual(record["preview"]["revision"], pending["revision"])
        self.assertEqual(self.rows()[0]["t_air"], 5)

    def test_xlsx_formula_like_original_writes_as_text(self):
        file = self.upload(csv("2024-04-01 00:00,=1+1,65"))
        report, request = self.review(file)
        self.assertEqual(report["stats"]["marked"], 1)
        _, workbook = store.preview_excel(request)
        self.assertEqual(read_upload(workbook, "clean.xlsx", "")[0]["t_air"], "")
        with ZipFile(BytesIO(workbook)) as archive:
            detail = archive.read("xl/worksheets/sheet2.xml")
        self.assertIn(b"=1+1", detail)
        self.assertNotIn(b"<f>", detail)


if __name__ == "__main__":
    unittest.main()
