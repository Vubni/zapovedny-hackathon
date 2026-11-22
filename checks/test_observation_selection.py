"""Selected observation rows can be reviewed without a separate upload."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import observation_store as store
from observations_db import connection
from analytics import ARCHIVE
from observations import archive_rows, chart_data, clean_archive
from review_upload import read_upload
from station_catalog import register_station


class ObservationSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(
            Path(directory.name) / "selection.sqlite3",
        ))
        environment.start()
        self.addCleanup(environment.stop)

    def imported_terms(self, file):
        name, kind, body = store.original_file(file)
        return read_upload(body, name, kind)

    def confirm(self, file):
        return store.confirm_file({"id": file["id"], "station": file["station"],
                                   "revision": file["preview"]["revision"]})

    def test_archive_draft_preserves_blank_terms_and_other_parameters(self):
        query = {"station": "kr1", "param": "pressure_hpa", "start": "2024-04-15", "end": "2024-04-15"}
        chart = {"stations": "kr1", "chart": "rh", "grain": "day", "start": query["start"], "end": query["end"]}
        humidity = chart_data(chart)["series"]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(store.from_observations, [query] * 3))
        file = results[0]
        self.assertEqual([item["id"] for item in results], [file["id"]] * 3)
        self.assertEqual([item["preview"]["revision"] for item in results], [file["preview"]["revision"]] * 3)
        self.assertEqual(len(store.list_files()["files"]), 1)
        self.assertEqual(file["status"], "preview")
        self.assertEqual(file["provenance"]["kind"], "observations")
        self.assertEqual(file["provenance"]["flagged"], file["rows"])
        self.assertEqual(file["provenance"]["empty"], 0)
        self.assertEqual(file["provenance"]["sources"], {"archive": file["rows"]})
        self.assertEqual(list(store.confirmed_rows("kr1", query["start"], query["end"])), [])
        terms = self.imported_terms(file)
        self.assertEqual(len(terms), file["rows"])
        self.assertTrue(all(set(term) == {"datetime", "pressure_hpa", "qc_flag", "origin", "origin_line",
                                          "origin_decision"} for term in terms))
        self.assertTrue(all(term["pressure_hpa"] and term["origin"].startswith("archive:") for term in terms))
        preview = store.get_file({"id": file["id"], "revision": file["preview"]["revision"]})
        self.assertEqual(preview["stats"]["parameters"]["pressure_hpa"]["marked"], file["rows"])
        self.assertEqual(preview["file"]["provenance"], file["provenance"])

        published = self.confirm(file)
        self.assertEqual(chart_data(chart)["series"], humidity)
        self.assertEqual(store.from_observations(query), published)
        self.assertEqual(len(store.list_files()["files"]), 1)

    def test_real_archive_outlier_and_qc_flag_survive_draft_and_exports(self):
        day, stamp = "2024-04-28", "2024-04-28 16:37"
        query = {"station": "kr1", "param": "pressure_hpa", "start": day, "end": day}
        chart = {"stations": "kr1", "chart": "pressure", "grain": "day", "start": day, "end": day}
        before = chart_data(chart)
        file = store.from_observations(query)
        request = {"id": file["id"], "revision": file["preview"]["revision"]}
        terms = {term["datetime"]: term for term in self.imported_terms(file)}
        self.assertEqual((terms[stamp]["pressure_hpa"], terms[stamp]["qc_flag"]), ("1100", "pressure_hpa:range"))
        draft = store.get_file(request)
        point = next(item for item in draft["points"]["pressure_hpa"] if item["time"] == stamp)
        self.assertEqual((point["value"], point["qc_flag"], point["marked"]), (1100, "pressure_hpa:range", True))
        self.assertIn("Исходный флаг: pressure_hpa:range.", point["reasons"])
        with ZipFile(BytesIO(store.preview_excel(request)[1])) as workbook:
            audit = workbook.read("xl/worksheets/sheet2.xml")
            self.assertIn(stamp.encode(), audit)
            self.assertIn(b"1100", audit)
            self.assertIn(b"pressure_hpa:range", audit)
        self.assertEqual(chart_data(chart), before)
        self.confirm(file)
        published = next(row for row in store.confirmed_rows("kr1", day, day) if row["datetime"] == stamp)
        self.assertNotIn("pressure_hpa", published)
        self.assertEqual(chart_data(chart), before)

    def test_conflicting_archive_duplicate_sensors_are_kept_but_not_published(self):
        day, stamp = "1993-04-02", "1993-04-02 16:15"
        query = {"station": "ame60", "param": "wind_speed", "start": day, "end": day}
        chart = {"stations": "ame60", "chart": "wind_speed", "grain": "day", "start": day, "end": day}
        before = chart_data(chart)
        file = store.from_observations(query)
        selected = [item for item in self.imported_terms(file) if item["datetime"] == stamp]
        self.assertEqual([item["wind_speed"] for item in selected], ["0.0", "3.2"])
        self.assertEqual([item["qc_flag"] for item in selected], ["dup_conflict"] * 2)
        self.assertEqual(len({item["origin_line"] for item in selected}), 2)
        self.confirm(file)
        self.assertFalse(any(row["datetime"] == stamp for row in store.confirmed_rows("ame60", day, day)))
        self.assertEqual(chart_data(chart), before)

    def test_confirmed_observations_keep_origin_and_exclude_estimates(self):
        station = register_station({"id": "model-source", "label": "Пост обзора", "minimum_slots": 1})["station"]["id"]
        first = datetime(2024, 4, 1)
        csv = ["datetime,t_air,rh"]
        for index in range(60):
            if index == 14:
                continue
            stamp = (first + timedelta(hours=index)).isoformat(" ", timespec="minutes")
            csv.append(f"{stamp},{'' if index == 10 else index / 4},64")
        file = store.upload_file({"station": station, "filename": "Пост.csv"}, "\n".join(csv).encode(), "text/csv")
        request = {"id": file["id"], "station": station, "fill": "model"}
        report = store.preview_file(request)
        original = store.confirm_file(dict(request, revision=report["revision"]))
        start, end = "2024-04-01", "2024-04-03"
        previous = {row["datetime"]: row for row in store.confirmed_rows(station, start, end)}
        self.assertIn("t_air", previous["2024-04-01 10:00"]["estimates"])
        self.assertTrue(previous["2024-04-01 14:00"]["synthetic"])

        selected = store.from_observations({"station": station, "param": "t_air", "start": start, "end": end})
        terms = {term["datetime"]: term for term in self.imported_terms(selected)}
        self.assertEqual(selected["provenance"]["sources"], {"confirmed": 59})
        self.assertEqual(selected["provenance"]["measurements"], 58)
        self.assertEqual(selected["provenance"]["empty"], 1)
        self.assertNotIn("2024-04-01 14:00", terms)
        self.assertEqual(terms["2024-04-01 10:00"]["t_air"], "")
        self.assertEqual(terms["2024-04-01 09:00"]["origin"], f"confirmed:{file['id']}@{original['version']}")
        self.assertEqual(float(terms["2024-04-01 09:00"]["t_air"]), 2.25)

        recalculated = store.preview_file({"id": selected["id"], "station": station, "filters": "spike"})
        self.assertEqual(recalculated["file"]["provenance"], selected["provenance"])
        store.confirm_file({"id": selected["id"], "station": station, "filters": "spike",
                            "revision": recalculated["revision"]})
        rows = {row["datetime"]: row for row in store.confirmed_rows(station, start, end)}
        self.assertNotIn("t_air", rows["2024-04-01 10:00"])
        self.assertEqual(rows["2024-04-01 10:00"]["rh"], 64)
        self.assertEqual(rows["2024-04-01 09:00"]["t_air"], 2.25)
        self.assertTrue(rows["2024-04-01 14:00"]["synthetic"])

    def test_combined_selection_uses_latest_declared_parameter_only(self):
        station, day = "kr1", "2024-04-15"
        with ZipFile(ARCHIVE) as archive:
            terms = ((row["datetime"], clean_archive(row, ("t_air",))) for row in
                     archive_rows(archive, station, 2024, day, day))
            first, second = [(stamp, values["t_air"]) for stamp, values in terms if values and "t_air" in values][:2]

        def publish(parameter, stamp, value):
            original = f"datetime,{parameter}\n{stamp},{value}\n".encode()
            file = store.upload_file({"station": station, "filename": f"{parameter}.csv"}, original, "text/csv")
            report = store.preview_file({"id": file["id"], "station": station})
            return self.confirm(report["file"])

        replaced = publish("t_air", first[0], 18.125)
        publish("rh", second[0], 73)
        file = store.from_observations({"station": station, "param": "t_air", "start": day, "end": day})
        source = {term["datetime"]: term for term in self.imported_terms(file)}
        self.assertEqual(float(source[first[0]]["t_air"]), 18.125)
        self.assertEqual(source[first[0]]["origin"], f"confirmed:{replaced['id']}@{replaced['version']}")
        self.assertEqual(float(source[second[0]]["t_air"]), second[1])
        self.assertTrue(source[second[0]]["origin"].startswith("archive:"))
        self.assertEqual(file["provenance"]["sources"]["confirmed"], 1)

    def test_rejected_published_reading_keeps_raw_sensor_and_previous_reason(self):
        station = register_station({"id": "excluded-source", "label": "Исключённый датчик"})["station"]["id"]
        day = "2024-04-15"
        csv = ("datetime,t_air,rh\n2024-04-15 00:15,10,63\n2024-04-15 01:15,37,64\n"
               "2024-04-15 02:15,10,66\n")
        source = store.upload_file({"filename": "Датчик.csv", "station": station}, csv.encode(), "text/csv")
        settings = {"id": source["id"], "station": station, "filters": "spike"}
        draft = store.preview_file(settings)
        store.confirm_file(dict(settings, revision=draft["revision"]))
        file = store.from_observations({"station": station, "param": "t_air", "start": day, "end": day})
        source_row = self.imported_terms(file)[1]
        self.assertEqual(source_row["t_air"], "37")
        self.assertEqual(source_row["qc_flag"], "")
        self.assertIn("spike", source_row["origin_decision"])
        report = store.get_file({"id": file["id"]})
        reading = report["points"]["t_air"][1]
        self.assertEqual((reading["value"], reading["marked"]), (37, True))
        self.assertTrue(any("Ранее исключено" in reason for reason in reading["reasons"]))
        original = list(store.confirmed_rows(station, day, day))
        self.assertNotIn("t_air", original[1])
        self.confirm(file)
        self.assertEqual(list(store.confirmed_rows(station, day, day)), original)

    def test_changed_independent_source_creates_snapshot_but_own_publication_does_not(self):
        station = register_station({"id": "fingerprint", "label": "Пост версий", "minimum_slots": 1})["station"]["id"]
        day = "2024-04-15"
        selection = {"station": station, "param": "t_air", "start": day, "end": day}

        def publish(parameter, value):
            csv = f"datetime,{parameter}\n{day} 00:15,{value}\n"
            file = store.upload_file({"filename": "Датчик.csv", "station": station}, csv.encode(), "text/csv")
            report = store.preview_file({"id": file["id"], "station": station})
            return self.confirm(report["file"])

        publish("t_air", 7)
        file = store.from_observations(selection)
        revision = file["preview"]["revision"]
        request = {"id": file["id"], "revision": revision}
        exported = store.draft_csv(request)[1]
        edited = exported.replace(b";7.0", b";9.125")
        correction = store.corrected_file(dict(request, filename="Исправлено.csv"), edited, "text/csv")
        self.assertEqual(store.from_observations(selection)["preview"]["revision"], correction["revision"])
        saved = self.confirm(correction["file"])
        self.assertEqual(store.from_observations(selection), saved)
        publish("rh", 66)
        self.assertEqual(store.from_observations(selection), saved)
        changed = publish("t_air", 20)
        newer = store.from_observations(selection)
        self.assertNotEqual(newer["id"], saved["id"])
        self.assertNotEqual(newer["provenance"]["fingerprint"], saved["provenance"]["fingerprint"])
        original = self.imported_terms(newer)[0]
        self.assertEqual(float(original["t_air"]), 20)
        self.assertEqual(original["origin"], f"confirmed:{changed['id']}@{changed['version']}")
        self.assertEqual(store.file_record(saved)["confirmed"]["revision"], saved["confirmed"]["revision"])
        self.assertEqual(store.from_observations(selection), newer)

    def test_changed_archive_reading_creates_new_snapshot_even_if_still_flagged(self):
        day = "2024-04-28"
        query = {"station": "kr1", "param": "pressure_hpa", "start": day, "end": day}

        def copy_archive_values(reading):
            yield SimpleNamespace(time=f"{day} 16:37", line=100, source="kr1.csv",
                                  original={"pressure_hpa": reading, "qc_flag": "pressure_hpa:range"})

        with patch("observation_selection.archive_observations", side_effect=lambda *_: copy_archive_values("1100")):
            first = store.from_observations(query)
        with patch("observation_selection.archive_observations", side_effect=lambda *_: copy_archive_values("1101")):
            second = store.from_observations(query)
            self.assertEqual(store.from_observations(query), second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([self.imported_terms(file)[0]["pressure_hpa"] for file in (first, second)], ["1100", "1101"])

    def test_pristine_legacy_draft_refreshes_in_place(self):
        day = "2024-04-28"
        query = {"station": "kr1", "param": "pressure_hpa", "start": day, "end": day}
        file = store.from_observations(query)
        legacy = [{**{key: term[key] for key in ("datetime", "origin")}, "pressure_hpa": None}
                  for term in self.imported_terms(file)]
        for_update = {key: value for key, value in file["provenance"].items() if key != "fingerprint"}
        header = {"fields": ["datetime", "pressure_hpa", "origin"], "provenance": for_update}
        with connection(write=True) as db:
            db.execute("UPDATE files SET source=?, columns=?, original=? WHERE id=?", (
                store._pack(list(enumerate(legacy, start=2))), json.dumps(header),
                b"datetime,pressure_hpa,origin\n", file["id"],
            ))
        refreshed = store.from_observations(query)
        self.assertEqual(refreshed["id"], file["id"])
        self.assertNotEqual(refreshed["preview"]["revision"], file["preview"]["revision"])
        self.assertEqual(refreshed["provenance"]["fingerprint"], file["provenance"]["fingerprint"])
        self.assertEqual(store.from_observations(query), refreshed)

    def test_empty_range_or_unmeasured_parameter_does_not_create_file(self):
        query = {"station": "kr1", "param": "t_air", "start": "2000-01-01", "end": "2000-01-01"}
        with self.assertRaisesRegex(ValueError, "нет исходных сроков"):
            store.from_observations(query)
        with self.assertRaisesRegex(ValueError, "который измеряет"):
            store.from_observations(dict(query, param="soil_05"))
        with self.assertRaisesRegex(ValueError, "Дата начала"):
            store.from_observations(dict(query, end="1999-12-31"))
        self.assertEqual(store.list_files()["files"], [])
