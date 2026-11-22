"""Check the XLSX package plus real recalculation when LibreOffice is available."""

from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile

from review_excel import REVIEW_XLSX_MAX_ROWS, export_xlsx
from review_workbook_copy import attach_original

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Font, PatternFill
except ImportError:
    load_workbook = None
try:
    from PIL import Image
except ImportError:
    Image = None


def fixture():
    fields = ["datetime", "t_air", "qc_flag", "метка"]
    example_path = "kr1/2025.csv"

    def observation(hour, text, value, *, keep, official, estimate=None, line=None, label=""):
        time = f"2025-01-01 {hour:02d}:00"
        return dict(
            time=time, line=line, source=example_path if line else "",
            original=dict(zip(fields, (time, text, "t_air:range", label))) if line else {},
            value=value, keep=keep, official_keep=official, estimate=estimate,
            estimate_sources="Кр2, измерение в 03:00" if estimate is not None else "",
            reasons=["Причина по алгоритму"], official_reasons=["Причина архивного контроля"],
        )

    summary = dict(n=2, slots=24, mean=0, min=-2, max=2, valid=False, reason="Пример покрытия")
    return dict(
        station="kr1", param="t_air", start="2025-01-01", end="2025-01-01", fields=fields,
        rows=[
            observation(0, "000.00", 0, keep=True, official=True, line=17, label='=1+1'),
            observation(1, "-003.00", -3, keep=False, official=True, estimate=-2, line=18),
            observation(2, "", None, keep=False, official=False, estimate=2, line=19),
            observation(3, "", None, keep=False, official=False, estimate=3),
            observation(4, "100", 100, keep=False, official=False, line=20),
            observation(5, "", None, keep=False, official=False),
            observation(6, "7", 7, keep=True, official=True, estimate=41, line=21),
        ],
        summaries={"daily": [dict(day="2025-01-01", raw_n=5, working=summary, official=summary)]},
    )


def source_workbook(report):
    workbook = Workbook()
    data = workbook.active
    data.title = "Сроки"
    data.append(report["fields"])
    for row in report["rows"]:
        if row["line"] is not None:
            for index, field in enumerate(report["fields"], start=1):
                cell = data.cell(row["line"], index, row["original"][field])
                cell.data_type = "s"
    data["B17"].font = Font(color="123456", bold=True)
    data["B17"].number_format = "0.000"
    data.conditional_formatting.add("B17", FormulaRule(
        formula=["TRUE"], fill=PatternFill(fill_type="solid", fgColor="C5EFFF"),
    ))
    other = workbook.create_sheet("Управление")
    other["A1"] = "Исходные заметки"
    other["A2"] = "=2+2"
    other.merge_cells("A1:D1")
    body = BytesIO()
    workbook.save(body)
    workbook.close()
    return body.getvalue()


def invalid_observation(report):
    item = report["rows"][1]
    item.update(value=None, estimate=None, estimate_sources="", keep=False, official_keep=False,
                reason_codes=["invalid_number"], official_status="flagged")
    item["original"]["t_air"] = "=1+1"
    return report


class ReviewExcelTest(unittest.TestCase):
    def test_package(self):
        workbook = export_xlsx(fixture())
        with ZipFile(BytesIO(workbook)) as package:
            self.assertIsNone(package.testzip())
            self.assertFalse(any("vba" in name.lower() or "externalLink" in name for name in package.namelist()))
            for name in package.namelist():
                ElementTree.fromstring(package.read(name))
            records = ElementTree.fromstring(package.read("xl/worksheets/sheet2.xml"))
            self.assertEqual(records.find("{*}autoFilter").attrib["ref"], "A1:Q8")
            self.assertEqual(len(records.findall("{*}conditionalFormatting")), 2)
            styles = ElementTree.fromstring(package.read("xl/styles.xml"))
            differential = styles.findall("{*}dxfs/{*}dxf/{*}fill/{*}patternFill/{*}bgColor")
            self.assertEqual([color.get("rgb") for color in differential], ["FFE4F5ED", "FFFCE8EA", "FFF0E9FA", "FFEDF0F3"])
        report = fixture()
        report["rows"] = [None] * (REVIEW_XLSX_MAX_ROWS + 1)
        self.assertGreaterEqual(REVIEW_XLSX_MAX_ROWS, 60_000 + 31 * 24)
        with self.assertRaisesRegex(ValueError, "60 744"):
            export_xlsx(report)
        report["rows"] = []
        with ZipFile(BytesIO(export_xlsx(report))) as package:
            sheet = ElementTree.fromstring(package.read("xl/worksheets/sheet2.xml"))
            self.assertEqual(sheet.find("{*}dimension").attrib["ref"], "A1:Q1")

    @unittest.skipUnless(load_workbook, "requires the development openpyxl package")
    def test_independent_reader(self):
        sample = fixture()
        minute = sample["rows"][2]
        minute["estimate_sources"] = "ERA5 + MERRA-2 · интерполяция к минуте"
        minute["provenance"] = {
            "time_utc": "2025-01-01T02:45:00+00:00", "unit": "°C",
            "sources": [{"model": "ERA5", "value": 1.125}, {"model": "MERRA-2", "value": 2.875}],
        }
        xlsx = export_xlsx(sample)
        workbook = load_workbook(BytesIO(xlsx))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames, ["Управление", "Сроки", "Сутки · снимок"])
        sheet = workbook["Сроки"]
        self.assertEqual(sheet.freeze_panes, "C2")
        self.assertEqual([c.value for c in sheet[1]][-4:], fixture()["fields"])
        self.assertEqual(sheet["Q2"].value, "=1+1")
        self.assertEqual(sheet["Q2"].data_type, "s")
        self.assertEqual(sheet["O2"].value, "000.00")
        self.assertEqual(sheet["L2"].value, 17)
        self.assertEqual(sheet["K2"].value, "kr1/2025.csv")
        self.assertEqual(sheet["M2"].value, "Причина архивного контроля")
        self.assertEqual(sheet["I3"].value, -2)
        self.assertEqual(sheet["J4"].value, "ERA5 + MERRA-2 · интерполяция к минуте\n"
                         "Срок UTC: 2025-01-01T02:45:00+00:00\nERA5: 1.125 °C\nMERRA-2: 2.875 °C")
        self.assertEqual(sheet["G3"].value, "Оставить")
        self.assertIn("Флаги архива", sheet["H3"].value)
        self.assertTrue(sheet["C3"].value.startswith("=IF("))
        self.assertIn("'Управление'!$B$5", sheet["D3"].value)
        self.assertEqual(workbook["Управление"].data_validations.dataValidation[0].sqref, "B5")
        self.assertEqual(sheet.data_validations.dataValidation[0].sqref, "B2:B8")
        self.assertEqual(workbook.calculation.calcMode, "auto")
        cached = load_workbook(BytesIO(xlsx), data_only=True)
        self.addCleanup(cached.close)
        self.assert_results(cached, [
            (0, "Исходное"), (-2, "Внешняя оценка"), (2, "Внешняя оценка"),
            (3, "Внешняя оценка"), (None, "Исключено"), (None, "Нет значения"), (7, "Исходное"),
        ], [2, 3, 1, 2])

    def assert_results(self, workbook, expected, counts):
        self.assertEqual(
            [(workbook["Сроки"][f"C{i}"].value, workbook["Сроки"][f"D{i}"].value) for i in range(2, 9)],
            expected,
        )
        self.assertEqual([workbook["Управление"][f"B{i}"].value for i in range(10, 14)], counts)
        for sheet in workbook:
            self.assertFalse(any(cell.data_type == "e" for row in sheet.iter_rows() for cell in row))

    @unittest.skipUnless(load_workbook and shutil.which("soffice"), "requires openpyxl and LibreOffice")
    def test_recalculate_all_modes(self):
        xlsx = export_xlsx(fixture())
        modes = ["Исключить все", "Вернуть все исходные", "По алгоритму"]
        with tempfile.TemporaryDirectory(prefix="review-xlsx-") as folder:
            folder = Path(folder).resolve()
            output = folder / "recalculated"
            output.mkdir()
            inputs = []
            for number, mode in enumerate(modes):
                workbook = load_workbook(BytesIO(xlsx))
                workbook["Управление"]["B5"] = mode
                for index, decision in {
                    2: "Исключить", 3: "Вернуть исходное", 4: "Вернуть исходное",
                    5: "Использовать оценку", 6: "Использовать оценку", 8: "Использовать оценку",
                }.items():
                    workbook["Сроки"][f"B{index}"] = decision
                path = folder / f"mode-{number}.xlsx"
                workbook.save(path)
                workbook.close()
                inputs.append(path)
            subprocess.run([
                shutil.which("soffice"), f"-env:UserInstallation={(folder / 'profile').as_uri()}",
                "--headless", "--convert-to", "xlsx", "--outdir", str(output), *map(str, inputs),
            ], check=True, capture_output=True, text=True, timeout=45)
            requirements = [
                ([(None, "Исключено")] * 7, [0, 0, 7, 7]),
                ([(0, "Исходное"), (-3, "Исходное"), (None, "Нет значения"), (None, "Нет значения"),
                  (100, "Исходное"), (None, "Нет значения"), (7, "Исходное")], [4, 0, 0, 3]),
                ([(None, "Исключено"), (-3, "Исходное"), (None, "Нет исходного"), (3, "Внешняя оценка"),
                  (None, "Нет оценки"), (None, "Нет значения"), (41, "Внешняя оценка")], [1, 2, 1, 4]),
            ]
            for path, mode, (expected, counts) in zip(inputs, modes, requirements):
                with self.subTest(mode=mode):
                    recalculated = load_workbook(output / path.name, data_only=True)
                    try:
                        self.assert_results(recalculated, expected, counts)
                        self.assertEqual(recalculated["Сроки"]["B3"].value, "Вернуть исходное")
                        self.assertEqual(recalculated["Сроки"]["Q2"].data_type, "s")
                        self.assertEqual(recalculated["Сроки"]["Q2"].value, "=1+1")
                    finally:
                        recalculated.close()


@unittest.skipUnless(load_workbook, "requires the development openpyxl package")
class OriginalWorkbookTest(unittest.TestCase):
    def setUp(self):
        report = invalid_observation(fixture())
        self.original = source_workbook(report)
        self.annotated = attach_original(
            self.original, "xl/worksheets/sheet1.xml", report, export_xlsx(report),
        )

    def test_keep_original_tabs_and_styles(self):
        workbook = load_workbook(BytesIO(self.annotated))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames, ["Сроки", "Управление", "Управление 2", "Сроки 2", "Сутки · снимок"])
        self.assertEqual(workbook["Сроки"]["B17"].value, "000.00")
        self.assertEqual(workbook["Сроки"]["B17"].number_format, "0.000")
        self.assertEqual(workbook["Сроки"]["B17"].font.color.rgb, "00123456")
        self.assertEqual(workbook["Сроки"]["B18"].value, "=1+1")
        self.assertEqual(workbook["Сроки"]["B18"].data_type, "s")
        self.assertEqual(workbook["Сроки 2"]["E3"].value, "=1+1")
        self.assertEqual(workbook["Сроки 2"]["E3"].data_type, "s")
        self.assertEqual(workbook["Сроки 2"]["F3"].value, "Исключить")
        self.assertEqual(workbook["Управление"]["A2"].value, "=2+2")
        self.assertIn("'Управление 2'!$B$5", workbook["Сроки 2"]["D2"].value)
        source_sheet = workbook["Сроки"]
        new_rules = source_sheet.conditional_formatting["B17:B21"]
        self.assertEqual([rule.priority for rule in new_rules], [1, 2])
        self.assertEqual(source_sheet.conditional_formatting["B17"][0].priority, 3)
        self.assertEqual(workbook.defined_names["MeteoAudit_rows"].attr_text, "'Сроки 2'!$L$2:$L$8")
        with ZipFile(BytesIO(self.original)) as before, ZipFile(BytesIO(self.annotated)) as after:
            self.assertEqual(before.read("xl/worksheets/sheet2.xml"), after.read("xl/worksheets/sheet2.xml"))
            xml_source = ElementTree.fromstring(before.read("xl/worksheets/sheet1.xml")).find("{*}sheetData")
            xml_copy = ElementTree.fromstring(after.read("xl/worksheets/sheet1.xml")).find("{*}sheetData")
            self.assertEqual(ElementTree.tostring(xml_source), ElementTree.tostring(xml_copy))

    @unittest.skipUnless(Image and shutil.which("soffice") and shutil.which("pdftoppm"), "requires LibreOffice, pdftoppm and Pillow")
    def test_original_colours_follow_the_decisions(self):
        with tempfile.TemporaryDirectory(prefix="review-xlsx-colour-") as folder:
            folder = Path(folder).resolve()
            inputs = []
            for label in ("default", "manual", "mass"):
                workbook = load_workbook(BytesIO(self.annotated))
                active = workbook["Сроки"]
                for sheet in workbook:
                    if sheet != active:
                        sheet.sheet_state = "hidden"
                active.print_area = "B17:B18"
                if label != "default":
                    workbook["Сроки 2"]["B3"] = "Вернуть исходное"
                if label == "mass":
                    workbook["Управление 2"]["B5"] = "Исключить все"
                path = folder / f"{label}.xlsx"
                workbook.save(path)
                workbook.close()
                inputs.append(path)
            subprocess.run([
                shutil.which("soffice"), f"-env:UserInstallation={(folder / 'profile').as_uri()}",
                "--headless", "--convert-to", "pdf", "--outdir", str(folder), *map(str, inputs),
            ], check=True, capture_output=True, text=True, timeout=45)
            colours = [(228, 245, 237), (252, 232, 234)]
            for path, expected in zip(inputs, ((True, True), (True, False), (False, True))):
                with self.subTest(mode=path.stem):
                    subprocess.run([shutil.which("pdftoppm"), "-f", "1", "-l", "1", "-r", "80", "-singlefile",
                                    "-png", str(path.with_suffix(".pdf")), str(path.with_suffix(""))],
                                   capture_output=True, check=True, timeout=10)
                    with Image.open(path.with_suffix(".png")) as rendered:
                        pixels = rendered.convert("RGB").getcolors(maxcolors=1_000_000)
                    present = [
                        sum(count for count, colour in pixels if max(abs(a - b) for a, b in zip(colour, target)) <= 4) > 100
                        for target in colours
                    ]
                    self.assertEqual(tuple(present), expected)

    @unittest.skipUnless(shutil.which("soffice"), "requires LibreOffice")
    def test_recalculate_returned_text_without_executing_it(self):
        cases = [
            ("По алгоритму", None, None, "Исключено"),
            ("По алгоритму", "Вернуть исходное", "=1+1", "Исходное"),
            ("Вернуть все исходные", None, "=1+1", "Исходное"),
            ("Исключить все", "Вернуть исходное", None, "Исключено"),
        ]
        with tempfile.TemporaryDirectory(prefix="review-invalid-xlsx-") as folder:
            folder = Path(folder).resolve()
            output = folder / "recalculated"
            output.mkdir()
            inputs = []
            for index, (mode, manual, _, _) in enumerate(cases):
                workbook = load_workbook(BytesIO(self.annotated))
                workbook["Управление 2"]["B5"] = mode
                if manual:
                    workbook["Сроки 2"]["B3"] = manual
                path = folder / f"invalid-{index}.xlsx"
                workbook.save(path)
                workbook.close()
                inputs.append(path)
            subprocess.run([
                shutil.which("soffice"), f"-env:UserInstallation={(folder / 'profile').as_uri()}",
                "--headless", "--convert-to", "xlsx", "--outdir", str(output), *map(str, inputs),
            ], check=True, capture_output=True, text=True, timeout=45)
            for path, (mode, manual, value, kind) in zip(inputs, cases):
                with self.subTest(mode=mode, manual=manual):
                    workbook = load_workbook(output / path.name, data_only=True)
                    try:
                        rows = workbook["Сроки 2"]
                        self.assertEqual(rows["C3"].value, value)
                        self.assertEqual(rows["D3"].value, kind)
                        self.assertEqual(rows["C7"].value, None)
                        self.assertEqual(workbook["Сроки"]["B18"].value, "=1+1")
                        self.assertEqual(workbook["Сроки"]["B18"].data_type, "s")
                    finally:
                        workbook.close()


if __name__ == "__main__":
    unittest.main()
