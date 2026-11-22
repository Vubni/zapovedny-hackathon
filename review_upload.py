"""Read original station rows from CSV, JSON or a values sheet in an Excel workbook."""
import csv
import io
import json
import math
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from build_data import LIMITS

MAX_BYTES = 12 * 1024 * 1024
MAX_ROWS = 60000
MAX_XML = 96 * 1024 * 1024
CELL_REF = re.compile(r"^([A-Z]{1,3})[1-9][0-9]*$")
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOCUMENT_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class SourceRow(dict):
    """Keep physical source row numbers without adding a column to the workbook."""
    def __init__(self, values, line):
        super().__init__(values)
        self.source_line = line


def headers(values):
    names = [str(value or "").strip() for value in values]
    if "datetime" not in names or not set(names).intersection(LIMITS):
        raise ValueError("Нужны столбец datetime и хотя бы один метеопоказатель в формате кейса.")
    if "" in names or len(set(names)) != len(names):
        raise ValueError("Пустое или повторяющееся название столбца.")
    return names


def json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("В JSON повторяется имя поля.")
    return result


def csv_rows(text):
    try:
        first_line = text.partition("\n")[0]
        delimiter = max((",", ";", "\t"), key=first_line.count)
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        names = headers(next(reader, []))
        for values in reader:
            if not any(values):
                continue
            if len(names) != len(values):
                raise ValueError(f"Строка {reader.line_num}: число ячеек отличается от заголовка.")
            yield SourceRow(zip(names, values), reader.line_num)
    except csv.Error as exc:
        raise ValueError(f"Не удалось прочитать CSV: {exc}") from None


def json_rows(text):
    def bad_constant(value):
        raise ValueError(f"Недопустимое число JSON: {value}")
    items = json.loads(text, object_pairs_hook=json_object, parse_constant=bad_constant)
    if not isinstance(items, list):
        raise ValueError("Ожидается JSON-массив записей станции.")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Элемент {index}: ожидается запись станции.")
        headers(item)
        if any(isinstance(v, (list, dict, bool)) for v in item.values()):
            raise ValueError(f"Элемент {index}: вложенные объекты и логические значения недопустимы.")
        yield SourceRow(item, index)


def excel_timestamp(value, date1904):
    if isinstance(value, str):
        return value.replace("T", " ").removesuffix(":00") if re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:00", value) else value
    try:
        serial = float(value)
        seconds = round(serial * 86400)
        epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
        stamp = epoch + timedelta(seconds=seconds)
        if not math.isfinite(serial) or stamp.second:
            raise ValueError()
        return stamp.isoformat(" ", timespec="minutes")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("В столбце datetime ожидается дата и время с точностью до минуты.") from None


def xlsx_cell(cell, shared):
    kind = cell.get("t", "n")
    raw = cell.findtext(SHEET_NS + "v")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(SHEET_NS + "t"))
    if raw is None:
        if cell.find(SHEET_NS + "f") is not None:
            raise ValueError(f"Ячейка {cell.get('r')} содержит формулу без сохранённого результата. Пересчитайте книгу или вставьте значения.")
        return ""
    if kind == "s":
        index = int(raw)
        if not 0 <= index < len(shared):
            raise ValueError("Повреждён индекс строки в книге Excel.")
        return shared[index]
    if kind in ("e", "b"):
        raise ValueError(f"Ячейка {cell.get('r')}: ошибка Excel или логическое значение.")
    return float(raw) if kind == "n" else raw


def sheet_records(archive, member, shared, date1904):
    names = None
    start_col = 0
    with archive.open(member) as stream:
        for _, row in ET.iterparse(stream, events=("end",)):
            if row.tag != SHEET_NS + "row":
                continue
            line = int(row.attrib["r"])
            cells = {}
            for cell in row.iterfind(SHEET_NS + "c"):
                if not len(cell):
                    continue
                match = CELL_REF.fullmatch(cell.get("r", ""))
                if not match:
                    raise ValueError("Некорректный адрес ячейки в XLSX.")
                col = 0
                for letter in match[1]:
                    col = col * 26 + ord(letter) - 64
                if col > 256:
                    raise ValueError("Слишком много заполненных столбцов в таблице станции.")
                cells[col-1] = cell
            if cells and names is None:
                if line > 30:
                    return
                values = [xlsx_cell(cells[c], shared) if c in cells and
                          cells[c].get("t") in ("inlineStr", "s", "str") else "" for c in range(max(cells)+1)]
                if "datetime" in values and set(values).intersection(LIMITS):
                    # A station table can begin below a title or to the right of a margin.
                    filled = [i for i, value in enumerate(values) if value != ""]
                    start_col = filled[0]
                    names = headers(values[start_col:filled[-1] + 1])
                    yield None  # marks a valid data sheet even when it contains no observations
            elif cells:
                values = [xlsx_cell(cells[c], shared) if c in cells else ""
                          for c in range(start_col, start_col + len(names))]
                if any(v != "" for v in values):
                    record = dict(zip(names, values))
                    record["datetime"] = excel_timestamp(record["datetime"], date1904)
                    source_row = SourceRow(record, line)
                    source_row.column_offset = start_col
                    yield source_row
            row.clear()


def xlsx_rows(body):
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            info = archive.infolist()
            if len(info) > 2048 or sum(m.file_size for m in info) > MAX_XML:
                raise ValueError("Распакованная книга слишком велика. Выберите один исходный год.")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            config = workbook.find(SHEET_NS + "workbookPr")
            date1904 = config is not None and config.get("date1904") in ("1", "true")
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rels = {r.get("Id"):r.get("Target") for r in relationships
                    if r.get("TargetMode") != "External" and r.get("Type", "").endswith("/worksheet")}
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                with archive.open("xl/sharedStrings.xml") as stream:
                    for _, el in ET.iterparse(stream):
                        if el.tag == SHEET_NS + "si":
                            shared.append("".join(t.text or "" for t in el.iter(SHEET_NS + "t")))
                            el.clear()
            for sheet in workbook.iter(SHEET_NS + "sheet"):
                target = rels.get(sheet.get(DOCUMENT_NS + "id"))
                if not target:
                    continue
                member = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
                if not member.startswith("xl/"):
                    continue
                rows = sheet_records(archive, member, shared, date1904)
                sentinel = object()
                if next(rows, sentinel) is not sentinel:
                    for record in rows:
                        record.sheet_member = member
                        yield record
                    return
            raise ValueError("В книге нет таблицы с заголовками datetime и метеопоказателя в первых 30 строках листа.")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("Не удалось прочитать XLSX. Нужна незашифрованная книга со значениями.") from exc


def read_upload(body, filename, content_type):
    if len(body) > MAX_BYTES:
        raise ValueError("Максимальный размер файла 12 МиБ.")
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix == ".xlsx" or content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        iterator = xlsx_rows(body)
    else:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("Текстовый файл должен быть в UTF-8, книгу Excel сохраните в XLSX.") from None
        if suffix == ".json" or content_type == "application/json":
            iterator = json_rows(text)
        elif suffix == ".csv" or content_type in ("text/csv", "application/csv"):
            iterator = csv_rows(text)
        else:
            raise ValueError("Поддерживаются исходные таблицы XLSX, CSV и JSON.")
    records = []
    for record in iterator:
        if len(records) >= MAX_ROWS:
            raise ValueError("На один расчёт допускается до 60 000 исходных строк.")
        records.append(record)
    if not records:
        raise ValueError("В файле нет строк наблюдений.")
    return records
