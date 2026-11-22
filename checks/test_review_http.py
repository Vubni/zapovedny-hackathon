"""Exercise the same HTTP endpoints used by the review form and Excel downloads."""
from datetime import datetime
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from server import Handler, XLSX_TYPE

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.utils.datetime import CALENDAR_MAC_1904
except ImportError:
    Workbook = None


class QuietHandler(Handler):
    def log_message(self, format, *args):
        pass


class ReviewHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = "http://127.0.0.1:" + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.thread.join()
        cls.http.server_close()

    def fetch(self, path, query, body=None, content_type="text/csv"):
        req = Request(self.root + path + "?" + urlencode(query), data=body,
                      headers={"Content-Type": content_type})
        try:
            response = urlopen(req, timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            kind = response.headers.get_content_type()
            contents = response.read()
            return response.status, (json.loads(contents) if kind == "application/json" else contents)

    def test_consistent_pages_download_and_expiry(self):
        params = dict(station="kr1", param="pressure_hpa", start="2024-04-01", end="2024-04-05")
        status, report = self.fetch("/api/review", params, b"")
        self.assertEqual(status, 200)
        self.assertEqual((report["counts"]["raw_n"], report["total_rows"], len(report["rows"])), (240, 240, 100))
        self.assertFalse(report["original_workbook"])
        token = {"token": report["token"]}
        pages = [report["rows"]]
        for offset in (100, 200):
            status, page = self.fetch("/api/review", dict(token, offset=offset))
            self.assertEqual((status, page["counts"]), (200, report["counts"]))
            pages.append(page["rows"])
        lines = [row["line"] for page in pages for row in page]
        self.assertEqual(len(set(lines)), 240)
        status, body = self.fetch("/api/review.xlsx", token)
        self.assertEqual(status, 200)
        with ZipFile(BytesIO(body)) as book:
            self.assertIsNone(book.testzip())
            self.assertIn(b'A1:U241', book.read("xl/worksheets/sheet2.xml"))
        self.assertEqual(self.fetch("/api/review-original.xlsx", token)[0], 400)
        self.assertEqual(self.fetch("/api/review", dict(token, offset=240))[0], 400)
        self.assertEqual(self.fetch("/api/review.xlsx", {"token": "expired"})[0], 410)

    def test_bad_upload_and_timezone(self):
        params = dict(station="kr1", param="t_air", filename="station.csv")
        for body in (b"", b"other\n9", b"datetime,t_air\n2024-02-35 08:00,1.1"):
            with self.subTest(body=body):
                self.assertEqual(self.fetch("/api/review", params, body)[0], 400)
        body = b"datetime;t_air\n2024-04-01 05:02;1,75"
        self.assertEqual(self.fetch("/api/review", dict(params, fill="1"), body)[0], 400)
        _, report = self.fetch("/api/review", params, body)
        self.assertEqual((report["rows"][0]["original"]["t_air"], report["rows"][0]["value"]), ("1,75", 1.75))

    def test_multiyear_period_depends_on_available_rows(self):
        params = dict(station="kr1", param="t_air", start="2022-01-01", end="2025-12-31")
        status, report = self.fetch("/api/review", params, b"")
        self.assertEqual(status, 200)
        self.assertEqual(len(report["summaries"]["monthly"]), 48)
        self.assertGreater(report["total_rows"], 20000)
        status, message = self.fetch("/api/review", dict(params, station="ame60", start="1992-01-01",
                                                        end="2021-12-31"), b"")
        self.assertEqual(status, 400)
        self.assertIn("60 000", message["error"])

    @unittest.skipUnless(Workbook, "requires openpyxl to verify the original workbook independently")
    def test_offset_table_excel_dates_and_original_sheet(self):
        original = Workbook()
        original.epoch = CALENDAR_MAC_1904
        original.active.title = "Описание"
        original.active["A1"] = "datetime"
        original.active["A2"] = "=2+2"
        data = original.create_sheet("Данные")
        data["B3"], data["C3"], data["D3"] = "datetime", "t_air", "метка"
        for n, value in ((5, 7.5), (9, 88)):
            data.cell(n, 2, datetime(2024, 4, 1, 7, n))
            data.cell(n, 3, value)
            data.cell(n, 4, "проверка")
        buf = BytesIO()
        original.save(buf)
        original.close()
        status, report = self.fetch("/api/review", dict(station="kr1", param="t_air", filename="field.xlsx"),
                                    buf.getvalue(), XLSX_TYPE)
        self.assertEqual((status, report["original_workbook"]), (200, True))
        self.assertEqual([(r["time"], r["line"], r["keep"]) for r in report["rows"]],
                         [("2024-04-01 07:05", 5, True), ("2024-04-01 07:09", 9, False)])
        status, body = self.fetch("/api/review-original.xlsx", {"token": report["token"]})
        self.assertEqual(status, 200)
        with ZipFile(buf) as source, ZipFile(BytesIO(body)) as copy:
            self.assertEqual(source.read("xl/worksheets/sheet1.xml"), copy.read("xl/worksheets/sheet1.xml"))
        book = load_workbook(BytesIO(body))
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames[:2], ["Описание", "Данные"])
        self.assertEqual(book["Данные"]["C9"].value, 88)
        self.assertEqual(len(book["Данные"].conditional_formatting["C5:C9"]), 2)
