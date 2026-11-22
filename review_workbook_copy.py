"""Append the audit to a copy of the uploaded XLSX and annotate its source cells."""

from copy import copy
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
import posixpath
import re
from shutil import copyfileobj
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from review_excel import GUIDE, MAIN_NS, MISSING, ORIGINAL, REL_NS, ROWS, _column


MAIN = f"{{{MAIN_NS}}}"
WORKBOOK = "xl/workbook.xml"
RELATIONSHIPS = "xl/_rels/workbook.xml.rels"
TYPES = "[Content_Types].xml"
STYLE_ORDER = (
    "numFmts", "fonts", "fills", "borders", "cellStyleXfs", "cellXfs",
    "cellStyles", "dxfs", "tableStyles", "colors", "extLst",
)
WORKBOOK_AFTER_NAMES = {
    "calcPr", "oleSize", "customWorkbookViews", "pivotCaches", "smartTagPr",
    "smartTagTypes", "webPublishing", "fileRecoveryPr", "webPublishObjects", "extLst",
}
WORKSHEET_AFTER_FORMAT = {
    "conditionalFormatting", "dataValidations", "hyperlinks", "printOptions",
    "pageMargins", "pageSetup", "headerFooter", "rowBreaks", "colBreaks",
    "customProperties", "cellWatches", "ignoredErrors", "smartTags", "drawing",
    "legacyDrawing", "legacyDrawingHF", "picture", "oleObjects", "controls",
    "webPublishItems", "tableParts", "extLst",
}
TAG = re.compile(rb"""<(?:"[^"]*"|'[^']*'|[^>"'])*>""")


@dataclass
class Section:
    name: str
    start: int
    inner: int
    close: int = 0
    end: int = 0


class Document:
    """Locate XML boundaries while leaving the original markup intact."""

    def __init__(self, data, *, worksheet=False):
        self.data = data
        self.root = None
        self.children = []
        self.rules = []
        self.edits = []
        parser = expat.ParserCreate(namespace_separator="}")
        depth, child = 0, None

        def bounds(name):
            start = parser.CurrentByteIndex
            tag = TAG.match(data, start)
            if tag is None:
                raise ValueError("Для дополнения исходной книги требуется обычный XLSX с XML в UTF-8.")
            return Section(name.rsplit("}", 1)[-1], start, tag.end())

        def start(name, attributes):
            nonlocal depth, child
            depth += 1
            local = name.rsplit("}", 1)[-1]
            if depth == 1:
                self.root = bounds(name)
            elif depth == 2:
                child = bounds(name)
                self.children.append(child)
            elif worksheet and local in {"conditionalFormatting", "conditionalFormattings"}:
                raise ValueError("В исходном листе есть расширенные правила подсветки Excel. Скачайте отдельный аудит.")
            elif worksheet and local == "cfRule" and depth == 3 and child.name == "conditionalFormatting":
                rule = bounds(name)
                self.rules.append((rule.start, rule.inner, int(attributes["priority"])))

        def finish(name):
            nonlocal depth
            section = self.root if depth == 1 else child if depth == 2 else None
            if section is not None:
                if data[section.inner - 2:section.inner] == b"/>":
                    section.close = section.end = section.inner
                else:
                    section.close = parser.CurrentByteIndex
                    section.end = TAG.match(data, section.close).end()
            depth -= 1

        def disallow_doctype(*args):
            raise ValueError("XML с DTD не поддерживается в книгах Excel.")

        parser.StartElementHandler = start
        parser.EndElementHandler = finish
        parser.StartDoctypeDeclHandler = disallow_doctype
        parser.Parse(data, True)

    def get(self, name):
        return next((section for section in self.children if section.name == name), None)

    def append(self, section, xml, *, count=None):
        tag = self.data[section.start:section.inner]
        if count is not None:
            tag = _set_attr(tag, b"count", count)
        if section.end == section.inner:
            prefix = re.match(rb"<([^\s/>]+)", tag)[1]
            self.edits.append((section.start, section.end, tag[:-2] + b">" + xml + b"</" + prefix + b">"))
        else:
            if count is not None:
                self.edits.append((section.start, section.inner, tag))
            self.edits.append((section.close, section.close, xml))

    def insert_before(self, names, xml):
        at = next((section.start for section in self.children if section.name in names), self.root.close)
        self.edits.append((at, at, xml))

    def result(self):
        chunks, cursor = [], 0
        for start, end, replacement in sorted(self.edits, key=lambda edit: edit[:2]):
            chunks.extend((self.data[cursor:start], replacement))
            cursor = end
        chunks.append(self.data[cursor:])
        return b"".join(chunks)


def _set_attr(tag, name, value):
    pattern = rb"(\s" + name + rb"""\s*=\s*)(['"])[^'"]*\2"""
    match = re.search(pattern, tag)
    value = str(value).encode("ascii")
    if match:
        return tag[:match.start()] + match[1] + b'"' + value + b'"' + tag[match.end():]
    end = -2 if tag.endswith(b"/>") else -1
    return tag[:end] + b" " + name + b'="' + value + b'"' + tag[end:]


def _element(tag, namespace, text, **attributes):
    return (
        f"<{tag} xmlns={quoteattr(namespace)}"
        + "".join(f" {key}={quoteattr(str(value))}" for key, value in attributes.items())
        + ("/>" if text is None else f">{text}</{tag}>")
    ).encode()


def _unique(base, taken, max_length=None, separator=" "):
    name, index = base, 2
    while name.casefold() in taken:
        suffix = f"{separator}{index}"
        name = (base[:max_length - len(suffix)] if max_length else base) + suffix
        index += 1
    taken.add(name.casefold())
    return name


def _member(parent, target):
    return posixpath.normpath(posixpath.join(posixpath.dirname(parent), target)).lstrip("/")


def _merge_styles(original, audit):
    if original is None:
        return audit, 0, 0
    source = Document(original)
    source_root = ET.fromstring(original)
    additions = ET.fromstring(audit)
    appended = ("fonts", "fills", "borders", "cellStyleXfs", "cellXfs", "dxfs")
    offsets = {}
    for name in appended:
        group = source_root.find(MAIN + name)
        offsets[name] = len(group) if group is not None else 0
    if offsets["cellXfs"] + len(additions.find(MAIN + "cellXfs")) > 65490:
        raise ValueError("В книге слишком много стилей Excel. Скачайте отдельный аудит.")
    for group in ("cellStyleXfs", "cellXfs"):
        for xf in additions.find(MAIN + group):
            for attribute, records in (
                ("fontId", "fonts"), ("fillId", "fills"), ("borderId", "borders"), ("xfId", "cellStyleXfs"),
            ):
                if attribute in xf.attrib:
                    xf.set(attribute, str(int(xf.get(attribute)) + offsets[records]))
    for name in appended:
        group = additions.find(MAIN + name)
        payload = b"".join(ET.tostring(child, encoding="utf-8") for child in group)
        section = source.get(name)
        if section:
            source.append(section, payload, count=offsets[name] + len(group))
        else:
            following = STYLE_ORDER[STYLE_ORDER.index(name) + 1:]
            source.insert_before(following, _element(name, MAIN_NS, payload.decode(), count=len(group)))
    return source.result(), offsets["cellXfs"], offsets["dxfs"]


def _update_audit_sheet(xml, styles, dxfs, names, *, guide):
    if styles:
        xml = re.sub(rb'(<c r="[A-Z]+[0-9]+" s=")([0-9]+)(")', lambda m: m[1] + str(int(m[2]) + styles).encode() + m[3], xml)
    if dxfs:
        xml = re.sub(rb'(<cfRule type="expression" dxfId=")([0-9]+)(")', lambda m: m[1] + str(int(m[2]) + dxfs).encode() + m[3], xml)
    replacements = {f"'{old}'!".encode(): f"'{new}'!".encode() for old, new in names.items() if old != new}
    if replacements:
        def rename_formula(match):
            value = match[0]
            for previous, current in replacements.items():
                value = value.replace(previous, current)
            return value
        xml = re.sub(rb"<f>[^<]*</f>", rename_formula, xml)
    if guide:
        for previous, current in names.items():
            if previous != current:
                xml = xml.replace(f"«{previous}»".encode(), f"«{current}»".encode())
        guide_text = f"Настройте режим книги и проверьте рабочий ряд на листе «{names[ROWS]}»."
        explanation = f"Лист «{names[ROWS]}» содержит рабочий ряд. Подсветка его исходных наблюдений действует и на исходном листе."
        xml = xml.replace(guide_text.encode(), explanation.encode(), 1)
    return xml


def _annotate_source(sheet_xml, column, lines, names, dxfs):
    sheet = Document(sheet_xml, worksheet=True)
    for start, end, priority in sheet.rules:
        sheet.edits.append((start, end, _set_attr(sheet_xml[start:end], b"priority", priority + 2)))
    first, last = min(lines), max(lines)
    match = f"MATCH(ROW(),{names['rows']},0)"
    result = f"INDEX({names['result']},{match})"
    decision = f"INDEX({names['decision']},{match})"
    kept = f'IFERROR({result}="{ORIGINAL}",FALSE)'
    rejected = f'IFERROR(AND({result}<>"{ORIGINAL}",{decision}<>"{MISSING}"),FALSE)'
    rules = "".join(
        f'<cfRule type="expression" dxfId="{dxfs + index}" priority="{index + 1}" stopIfTrue="1">'
        f'<formula>{escape(formula)}</formula></cfRule>'
        for index, formula in enumerate((kept, rejected))
    )
    sheet.insert_before(WORKSHEET_AFTER_FORMAT, _element(
        "conditionalFormatting", MAIN_NS, rules, sqref=f"{column}{first}:{column}{last}",
    ))
    return sheet.result()


def _next_part(parts):
    index = 1
    while f"xl/worksheets/meteoAudit{index}.xml" in parts:
        index += 1
    member = f"xl/worksheets/meteoAudit{index}.xml"
    parts.add(member)
    return member


def _attach(original, audit, sheet_member, report, output):
    parts = set(original.namelist())
    if len(parts) != len(original.infolist()):
        raise ValueError("В исходной книге повторяются имена внутренних файлов. Скачайте отдельный аудит.")
    if any(name.startswith("_xmlsignatures/") for name in parts):
        raise ValueError("Исходная книга подписана. Скачайте отдельный аудит, чтобы сохранить подпись.")
    workbook_xml = original.read(WORKBOOK)
    links_xml = original.read(RELATIONSHIPS)
    types_xml = original.read(TYPES)
    workbook = ET.fromstring(workbook_xml)
    links = ET.fromstring(links_xml)
    calc = workbook.find(MAIN + "calcPr")
    if calc is not None and calc.get("calcMode") == "manual":
        raise ValueError("В исходной книге включён ручной пересчёт. Включите автоматический пересчёт в Excel или скачайте отдельный аудит.")
    source_tabs = list(workbook.find(MAIN + "sheets"))
    tab_names = {tab.attrib["name"].casefold() for tab in source_tabs}
    registered = {link.attrib["Id"] for link in links}
    style_member = next((_member(WORKBOOK, link.attrib["Target"]) for link in links
                         if link.attrib["Type"] == f"{REL_NS}/styles"), None)
    source_targets = {link.attrib["Id"]: _member(WORKBOOK, link.attrib["Target"]) for link in links
                      if link.attrib["Type"] == f"{REL_NS}/worksheet" and link.get("TargetMode") != "External"}
    if sheet_member not in source_targets.values():
        raise ValueError("Исходный лист отсутствует в книге Excel. Скачайте отдельный аудит.")

    added_tabs, added_links, added_types, patches = [], [], [], {}
    types_ns = ET.fromstring(types_xml).tag.split("}")[0][1:]
    links_ns = links.tag.split("}")[0][1:]

    def register(part, kind):
        link_id = _unique(f"rIdMeteoAudit{len(added_links) + 1}", registered, separator="_")
        added_links.append(_element("Relationship", links_ns, None, Id=link_id, Type=f"{REL_NS}/{kind}", Target="/" + part))
        added_types.append(_element(
            "Override", types_ns, None, PartName="/" + part,
            ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml",
        ))
        return link_id

    style_data = audit.read("xl/styles.xml")
    styles, cell_offset, dxf_offset = _merge_styles(original.read(style_member) if style_member else None, style_data)
    if not style_member:
        style_member = "xl/meteoAuditStyles.xml"
        if style_member in parts:
            raise ValueError("Не удалось добавить стили в исходную книгу. Скачайте отдельный аудит.")
        register(style_member, "styles")
    patches[style_member] = styles

    tabs = ET.fromstring(audit.read(WORKBOOK)).find(MAIN + "sheets")
    names = {tab.attrib["name"]: _unique(tab.attrib["name"], tab_names, 31) for tab in tabs}
    next_id = max(int(tab.attrib["sheetId"]) for tab in source_tabs) + 1
    for number, tab in enumerate(tabs, start=1):
        base = tab.attrib["name"]
        name = names[base]
        member = _next_part(parts)
        link_id = register(member, "worksheet")
        added_tabs.append(_element("sheet", MAIN_NS, None, name=name, sheetId=next_id,
                                   **{"xmlns:r": REL_NS, "r:id": link_id}))
        next_id += 1
        source = audit.read(f"xl/worksheets/sheet{number}.xml")
        patches[member] = _update_audit_sheet(source, cell_offset, dxf_offset, names, guide=base == GUIDE)

    source = Document(workbook_xml)
    source.append(source.get("sheets"), b"".join(added_tabs))
    observations = report["rows"]
    lines = [row["line"] for row in observations if row["line"] is not None]
    if lines and report["param"] in report["fields"]:
        column = _column(report.get("column_offset", 0) + report["fields"].index(report["param"]) + 1)
        workbook_names = workbook.find(MAIN + "definedNames")
        known = {node.attrib["name"].casefold() for node in workbook_names} if workbook_names is not None else set()
        named_ranges = {}
        additions = []
        for kind, letter in (("rows", "L"), ("result", "D"), ("decision", "F")):
            name = _unique(f"MeteoAudit_{kind}", known, separator="_")
            named_ranges[kind] = name
            location = f"'{names[ROWS]}'!${letter}$2:${letter}${len(observations) + 1}"
            additions.append(_element("definedName", MAIN_NS, escape(location), name=name))
        additions = b"".join(additions)
        existing = source.get("definedNames")
        if existing:
            source.append(existing, additions)
        else:
            source.insert_before(WORKBOOK_AFTER_NAMES, _element("definedNames", MAIN_NS, additions.decode()))
        patches[sheet_member] = _annotate_source(original.read(sheet_member), column, lines, named_ranges, dxf_offset)
    patches[WORKBOOK] = source.result()
    relationships = Document(links_xml)
    relationships.append(relationships.root, b"".join(added_links))
    patches[RELATIONSHIPS] = relationships.result()
    types = Document(types_xml)
    types.append(types.root, b"".join(added_types))
    patches[TYPES] = types.result()
    for member in original.infolist():
        if member.filename in patches:
            output.writestr(copy(member), patches.pop(member.filename))
        else:
            with original.open(member) as reader, output.open(copy(member), "w") as writer:
                copyfileobj(reader, writer)
    for member, contents in patches.items():
        output.writestr(member, contents)


def attach_original(source_bytes, sheet_member, report, audit_bytes):
    """Return an augmented copy, preserving all non-audited worksheet parts."""
    output = BytesIO()
    try:
        with ZipFile(BytesIO(source_bytes)) as original, ZipFile(BytesIO(audit_bytes)) as audit:
            with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=4) as result:
                _attach(original, audit, str(PurePosixPath(sheet_member)), report, result)
    except (BadZipFile, KeyError, TypeError, AttributeError, ET.ParseError, expat.ExpatError, OverflowError) as exc:
        raise ValueError("Не удалось дополнить исходную книгу Excel. Скачайте отдельный аудит.") from exc
    return output.getvalue()
