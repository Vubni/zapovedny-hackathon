"""Exercise file processing, station passports and the chart API over HTTP."""

import csv
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from queue import Empty, Queue
from statistics import fmean
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
CSV_TYPE = "text/csv"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

SERVER = """
from http.server import ThreadingHTTPServer
from server import Handler

with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
    print(http.server_port, flush=True)
    http.serve_forever()
"""


def csv_bytes(rows):
    output = StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode()


def workbook_bytes(rows):
    """Make an XLSX independently of the application's upload and export code."""
    parts = {
        "[Content_Types].xml": f"""
<Types xmlns="{TYPES_NS}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": f"""
<Relationships xmlns="{PKG_REL_NS}">
  <Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f"""
<workbook xmlns="{SHEET_NS}" xmlns:r="{REL_NS}">
  <sheets><sheet name="Observations" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": f"""
<Relationships xmlns="{PKG_REL_NS}">
  <Relationship Id="rId1" Type="{REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
    }
    worksheet = ET.Element("worksheet", xmlns=SHEET_NS)
    data = ET.SubElement(worksheet, "sheetData")
    for index, values in enumerate(rows, start=1):
        row = ET.SubElement(data, "row", r=str(index))
        for column, value in zip("ABC", values):
            cell = ET.SubElement(row, "c", r=f"{column}{index}")
            if isinstance(value, str):
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, "is"), "t").text = value
            else:
                ET.SubElement(cell, "v").text = str(value)
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(worksheet, encoding="utf-8")
    output = BytesIO()
    with ZipFile(output, "w") as workbook:
        for name, body in parts.items():
            workbook.writestr(name, body)
    return output.getvalue()


class ObservationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    @contextmanager
    def http_server(self):
        temporary = Path(self.temp.name)
        env = dict(os.environ, METEO_DATABASE_URL="", METEO_OBSERVATION_DB=str(temporary / "observations.sqlite3"))
        with (temporary / "server.log").open("w+", encoding="utf-8") as log:
            with subprocess.Popen(
                [sys.executable, "-c", SERVER], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=log, text=True,
            ) as process:
                addresses = Queue()
                Thread(target=lambda: addresses.put(process.stdout.readline().strip()), daemon=True).start()
                try:
                    try:
                        address = addresses.get(timeout=10)
                    except Empty:
                        self.fail("Temporary HTTP server did not report its address within 10 seconds")
                    if not address.isdecimal():
                        log.seek(0)
                        self.fail(f"Temporary HTTP server did not start:\n{log.read()}")
                    self.url = f"http://127.0.0.1:{address}"
                    yield
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()

    def fetch(self, path, *, query=None, payload=None, body=None, mime=None, status=200, origin=None):
        if query:
            path += "?" + urlencode(query)
        if payload is not None:
            body, mime = json.dumps(payload).encode(), "application/json"
        headers = {"Content-Type": mime} if mime else {}
        if origin is not None:
            headers["Origin"] = origin
        request = Request(self.url + path, data=body, headers=headers)
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            contents = response.read()
            self.assertEqual(response.status, status, (path, contents[:1000]))
            if path.startswith(("/api/files", "/api/stations", "/api/observations")):
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            if response.headers.get_content_type() == "application/json":
                return json.loads(contents)
            return contents

    def test_files_write_same_origin_without_sign_in(self):
        with self.http_server():
            self.assertTrue(self.fetch("/api/stations")["stations"])
            self.fetch("/files")
            self.fetch("/api/files")
            self.fetch("/api/operator/session", status=404)
            self.fetch("/api/stations", payload={}, origin="https://elsewhere.invalid", status=403)
            options = {"filename": "public.csv", "station": "kr1"}
            source = b"datetime,t_air\n2024-04-01 12:00,8\n"
            self.fetch("/api/files/upload", query=options, body=source, mime=CSV_TYPE,
                       origin="https://elsewhere.invalid", status=403)
            upload = self.fetch("/api/files/upload", query=options, body=source, mime=CSV_TYPE,
                                origin=self.url, status=201)
            self.assertEqual(self.fetch("/api/files/original", query={"id": upload["id"]}), source)
            report = self.fetch("/api/review", query={"station": "kr1", "param": "t_air"},
                                body=b"")
            self.assertNotIn(".", report["token"])
            self.fetch("/api/review", query={"token": report["token"]})

    def test_from_observations_creates_one_public_working_file(self):
        with self.http_server():
            selection = {"station": "kr1", "param": "pressure_hpa", "start": "2024-04-01", "end": "2024-04-01"}
            created = self.fetch("/api/files/from-observations", payload=selection, origin=self.url)
            existing = self.fetch("/api/files/from-observations", payload=selection)
            self.assertEqual(created["id"], existing["id"])
            self.assertEqual(created["status"], "preview")
            report = self.fetch("/api/review", query={"file_id": created["id"], "param": "pressure_hpa"}, body=b"")
            self.assertEqual((report["station"], report["start"], report["end"]),
                             ("kr1", selection["start"], selection["end"]))
            self.assertEqual(len(report["rows"]), created["rows"])

    def upload(self, name, station, data, mime=CSV_TYPE):
        return self.fetch("/api/files/upload", query={"filename": name, "station": station},
                          body=data, mime=mime, status=201)

    def chart(self, stations, start, end=None, *, chart="air", grain="day"):
        reply = self.fetch("/api/observations", query={
            "stations": stations, "chart": chart, "grain": grain,
            "start": start, "end": end or start,
        })
        self.assertEqual((reply["start"], reply["end"]), (start, end or start))
        return reply

    def values(self, stations, start, end=None, *, chart="air"):
        reply = self.chart(stations, start, end, chart=chart)
        return {series["station"]: {point["x"]: point["y"] for point in series["points"]}
                for series in reply["series"]}

    def history(self):
        return {file["id"]: file for file in self.fetch("/api/files")["files"]}

    def station(self, key, **fields):
        return self.fetch("/api/stations", payload={"id": key, "label": f"Полевая {key}", **fields},
                          status=201)["station"]

    def preview(self, file, filters=""):
        payload = {"id": file["id"], "station": file["station"], "filters": filters, "fill": "none"}
        report = self.fetch("/api/files/preview", payload=payload)
        return report, dict(payload, revision=report["revision"])

    def confirm(self, payload, status=200):
        return self.fetch("/api/files/confirm", payload=payload, status=status)

    def discard(self, file):
        return self.fetch("/api/files/discard", payload={"id": file["id"], "version": file["version"]})

    def test_registration_retry_keeps_one_geolocated_station_for_the_upload_batch(self):
        first_day, second_day = "2026-05-16", "2026-05-17"
        passport = {"id": "station-upload-retry", "label": "Пост на экваторе", "profile": "kr1",
                    "latitude": 0, "longitude": -71.123456789}
        with self.http_server():
            self.fetch("/api/stations", payload=passport, status=201)
            self.fetch("/api/stations", payload=passport, status=400)
            self.fetch("/api/stations", payload=dict(passport, id="duplicate-id", label=passport["label"].upper()),
                       status=400)
            self.fetch("/api/stations", payload=dict(passport, id="out-of-range", label="Вне диапазона",
                                                      latitude=-90.01), status=400)
            restored = [item for item in self.fetch("/api/stations")["stations"]
                        if item["label"] == passport["label"]]
            self.assertEqual(len(restored), 1)
            station = restored[0]
            for field, value in passport.items():
                self.assertEqual(station[field], value)
            self.assertEqual(station["location_precision"], "exact")
            self.assertIsNone(station["start"])

            def upload_day(day, value):
                data = csv_bytes([("datetime", "t_air"), *[
                    (f"{day} {hour:02d}:15", value) for hour in range(18)
                ]])
                uploaded = self.upload(f"{day}.csv", station["id"], data)
                self.assertEqual(uploaded["station"], station["id"])
                return self.preview(uploaded)[1]

            first, second = upload_day(first_day, 8.125), upload_day(second_day, 11.375)
            expected = {first_day: None, second_day: None}
            self.assertEqual(self.values(station["id"], first_day, second_day), {station["id"]: expected})
            self.confirm(first)
            expected[first_day] = 8.125
            self.assertEqual(self.values(station["id"], first_day, second_day), {station["id"]: expected})
            self.confirm(second)
            expected[second_day] = 11.375
            self.assertEqual(self.values(station["id"], first_day, second_day), {station["id"]: expected})
            confirmed = next(item for item in self.fetch("/api/stations")["stations"]
                             if item["id"] == station["id"])
            self.assertEqual((confirmed["start"], confirmed["end"]), (first_day, second_day))
            self.assertEqual((confirmed["latitude"], confirmed["longitude"]), (0, -71.123456789))

    def test_independent_csv_xlsx_confirmation_reprocessing_restart_and_removal(self):
        start, csv_day, xlsx_day = "2026-05-12", "2026-05-13", "2026-05-14"
        headers = ("datetime", "t_air", "rh")
        csv_rows = [(f"{csv_day} {hour:02d}:15", {5: -100, 10: 27}.get(hour, 10), 70 if hour == 5 else 65)
                    for hour in range(22)]
        csv_data = csv_bytes([headers, *csv_rows])
        xlsx_rows = [(datetime(2026, 5, 14, hour, 30), 16, 72) for hour in range(20)]
        excel_epoch = datetime(1899, 12, 30)
        xlsx_data = workbook_bytes([headers, *[
            ((stamp - excel_epoch).total_seconds() / 86400, temp, rh) for stamp, temp, rh in xlsx_rows
        ]])
        gps = {"latitude": 56.51737, "longitude": 32.87642}
        stations = "field-gps,field-no-gps"
        with self.http_server():
            located = self.station("field-gps", minimum_slots=20, **gps)
            unlocated = self.station("field-no-gps")
            self.assertEqual({key: located[key] for key in gps}, gps)
            self.assertEqual(located["location_precision"], "exact")
            self.assertEqual((unlocated["latitude"], unlocated["longitude"], unlocated["location_precision"]),
                             (None, None, "unknown"))
            self.assertFalse(located["archive"])
            self.assertFalse(unlocated["archive"])
            self.fetch("/api/stations", payload={"id": "partial-gps", "label": "Неполные координаты",
                                                "latitude": 56.5}, status=400)
            self.assertEqual(self.history(), {})
            uploaded_csv = self.upload("поляна.csv", located["id"], csv_data)
            uploaded_xlsx = self.upload("поле.xlsx", unlocated["id"], xlsx_data, XLSX_TYPE)
            self.assertEqual({item["status"] for item in self.history().values()}, {"uploaded"})
            self.assertEqual((uploaded_csv["start"], uploaded_csv["end"], uploaded_csv["rows"]), (csv_day, csv_day, 22))
            self.assertEqual((uploaded_xlsx["start"], uploaded_xlsx["end"], uploaded_xlsx["rows"]), (xlsx_day, xlsx_day, 20))

            missing = self.values(stations, start, xlsx_day)
            self.assertEqual(missing, {key: dict.fromkeys((start, csv_day, xlsx_day)) for key in stations.split(",")})
            self.confirm({"id": uploaded_csv["id"]}, status=400)
            first_preview, first_request = self.preview(uploaded_csv)
            second_preview, second_request = self.preview(uploaded_xlsx)
            self.assertEqual(second_preview["points"]["t_air"][0]["time"], f"{xlsx_day} 00:30")
            self.assertEqual(first_preview["stats"]["marked"], 1)
            self.assertEqual(first_preview["stats"]["parameters"]["rh"]["marked"], 0)
            self.assertEqual(self.values(stations, start, xlsx_day), missing)

            accepted_csv = self.confirm(first_request)
            csv_value = fmean(temp for _, temp, _ in csv_rows if temp != -100)
            self.assertEqual(self.values(stations, start, xlsx_day),
                             missing | {located["id"]: missing[located["id"]] | {csv_day: csv_value}})
            self.assertEqual(self.values(located["id"], csv_day, chart="rh")[located["id"]][csv_day],
                             fmean(rh for _, _, rh in csv_rows))
            accepted_xlsx = self.confirm(second_request)
            self.assertEqual(self.values(unlocated["id"], xlsx_day), {unlocated["id"]: {xlsx_day: 16}})
            self.assertEqual(self.history()[uploaded_xlsx["id"]]["confirmed"]["revision"], second_request["revision"])
            no_position = self.fetch("/api/files/preview", payload={"id": uploaded_xlsx["id"], "fill": "external",
                                                                    "timezone": "UTC"}, status=400)
            self.assertIn("координаты", no_position["error"])

            filtered, replacement = self.preview(uploaded_csv, filters="hampel,spike")
            marked = {point["time"]: point for point in filtered["points"]["t_air"] if point["marked"]}
            self.assertEqual(filtered["stats"]["marked"], 2)
            self.assertEqual(filtered["stats"]["parameters"]["rh"]["marked"], 0)
            self.assertEqual(marked[f"{csv_day} 05:15"]["value"], -100)
            self.assertEqual(marked[f"{csv_day} 10:15"]["value"], 27)
            self.assertEqual(len(marked[f"{csv_day} 10:15"]["reasons"]), 2)
            self.assertEqual(self.values(located["id"], csv_day), {located["id"]: {csv_day: csv_value}})
            self.confirm(first_request, status=400)
            self.confirm(dict(replacement, filters="hampel"), status=400)
            replaced = self.confirm(replacement)
            self.assertGreater(replaced["version"], accepted_csv["version"])
            expected = {located["id"]: {start: None, csv_day: 10, xlsx_day: None},
                        unlocated["id"]: {start: None, csv_day: None, xlsx_day: 16}}
            self.assertEqual(self.values(stations, start, xlsx_day), expected)

        with self.http_server():
            self.assertEqual(self.values(stations, start, xlsx_day), expected)
            station_catalog = self.fetch("/api/stations")
            by_id = {item["id"]: item for item in station_catalog["stations"]}
            self.assertEqual(station_catalog["end"], xlsx_day)
            self.assertEqual((by_id[located["id"]]["start"], by_id[located["id"]]["end"]), (csv_day, csv_day))
            self.assertEqual(by_id[located["id"]]["latitude"], gps["latitude"])
            self.assertIsNone(by_id[unlocated["id"]]["latitude"])
            self.assertEqual(self.history()[uploaded_csv["id"]]["confirmed"]["revision"], replacement["revision"])
            self.assertEqual(self.discard(replaced)["status"], "original")
            self.assertEqual(self.values(stations, start, xlsx_day), expected | {located["id"]: missing[located["id"]]})
            self.assertEqual(self.history()[uploaded_xlsx["id"]], accepted_xlsx)
            self.assertEqual(self.fetch("/api/files/original", query={"id": uploaded_csv["id"]}), csv_data)
            self.assertEqual(self.fetch("/api/files/original", query={"id": uploaded_xlsx["id"]}), xlsx_data)

            audit_request = {"file_id": uploaded_csv["id"], "param": "t_air"}
            audit = self.fetch("/api/review", query=audit_request, body=b"", mime=CSV_TYPE)
            self.assertEqual((audit["station"], audit["minimum_slots"], audit["total_rows"]),
                             (located["id"], 20, len(csv_rows)))
            self.assertEqual([row["value"] for row in audit["rows"]], [temp for _, temp, _ in csv_rows])
            self.assertTrue(audit["token"])
            self.assertNotIn(".", audit["token"])
            self.assertEqual(self.fetch("/api/review", query={"token": audit["token"]})["rows"], audit["rows"])
            book = self.fetch("/api/review.xlsx", query={"token": audit["token"]})
            with ZipFile(BytesIO(book)) as archive:
                sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
                ns = {"s": SHEET_NS}
                for cell, expected_value in (("E7", -100), ("E12", 27)):
                    value = sheet.findtext(f".//s:c[@r='{cell}']/s:v", namespaces=ns)
                    self.assertEqual(float(value), expected_value)
            self.fetch("/api/review", query=dict(audit_request, file_id="0" * 32),
                       body=b"", mime=CSV_TYPE, status=400)
            self.assertEqual(self.history()[uploaded_csv["id"]]["status"], "original")

            restored_report, restored_request = self.preview(uploaded_csv)
            self.assertEqual(restored_report["stats"]["marked"], 1)
            self.confirm(restored_request)
            self.assertEqual(self.values(located["id"], csv_day), {located["id"]: {csv_day: csv_value}})

    def test_overlapping_uploads_replace_only_their_columns_and_reveal_prior_sources(self):
        day = "2025-01-01"
        midnight, next_hour = f"{day} 00:00", f"{day} 01:00"
        headers = ("datetime", "t_air", "rh")
        with self.http_server():
            raw_query = {"station": "kr1", "day": day, "param": "t_air"}
            parameters = {"air": "t_air", "rh": "rh", "pressure": "pressure_hpa"}
            raw = {chart: self.fetch("/api/raw", query=dict(raw_query, param=param))
                   for chart, param in parameters.items()}
            original = {
                chart: {row["time"]: row["value"] for row in report["rows"] if row["status"] == "Пригодно"}
                for chart, report in raw.items()
            }
            for values in original.values():
                self.assertIn(midnight, values)
                self.assertIn(next_hour, values)

            def check_graphs(**replacements):
                for chart, values in original.items():
                    candidates = values | replacements.get(chart, {})
                    expected = fmean(value for value in candidates.values() if value is not None)
                    self.assertAlmostEqual(self.values("kr1", day, chart=chart)["kr1"][day], expected)

            csv_data = csv_bytes([headers, (midnight, 23, 65), (next_hour, 24, 66)])
            xlsx_data = workbook_bytes([headers[:2], (midnight, -100), (next_hour, "")])
            check_graphs()
            csv_file = self.upload("замер.csv", "kr1", csv_data)
            xlsx_file = self.upload("повторный-замер.xlsx", "kr1", xlsx_data, XLSX_TYPE)
            _, csv_request = self.preview(csv_file)
            report, xlsx_request = self.preview(xlsx_file)
            self.assertEqual(report["stats"]["marked"], 1)
            self.assertEqual(report["stats"]["parameters"]["t_air"]["missing"], 1)
            check_graphs()
            accepted_csv = self.confirm(csv_request)
            temperature = {midnight: 23, next_hour: 24}
            humidity = {midnight: 65, next_hour: 66}
            check_graphs(air=temperature, rh=humidity)
            accepted_xlsx = self.confirm(xlsx_request)
            check_graphs(air={midnight: None, next_hour: None}, rh=humidity)
            self.discard(accepted_xlsx)
            check_graphs(air=temperature, rh=humidity)
            self.discard(accepted_csv)
            check_graphs()
            self.assertEqual(self.fetch("/api/raw", query=raw_query), raw["air"])

    def test_chart_dates_survive_empty_and_different_station_selections(self):
        start, end = "2005-01-01", "2015-12-31"
        with self.http_server():
            empty = self.chart("", start, end, grain="month")
            self.assertEqual(empty["series"], [])
            self.assertIn("Выберите хотя бы одну станцию", empty["notes"])
            recent = self.chart("kr1", start, end, grain="month")
            old = self.chart("ame60", start, end, grain="month")
            self.assertTrue(all(point["y"] is None for point in recent["series"][0]["points"]))
            points = old["series"][0]["points"]
            self.assertEqual((points[0]["x"], points[-1]["x_end"], len(points)), (start, end, 132))
            self.assertTrue(any(point["y"] is not None for point in points))
            self.assertTrue(any(point["y"] is None for point in points))


if __name__ == "__main__":
    unittest.main()
