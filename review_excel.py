"""Write a macro-free, interactive XLSX review using only the standard library."""

from collections import Counter
from io import BytesIO
import json
from math import isfinite
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


REVIEW_XLSX_MAX_ROWS = 60_000 + 31 * 24

GUIDE = "Управление"
ROWS = "Сроки"
ALGORITHM = "По алгоритму"
RESTORE_ALL = "Вернуть все исходные"
EXCLUDE_ALL = "Исключить все"
RESTORE = "Вернуть исходное"
EXCLUDE = "Исключить"
USE_ESTIMATE = "Использовать оценку"
ORIGINAL = "Исходное"
ESTIMATE = "Внешняя оценка"
REJECTED = "Исключено"
MISSING = "Нет значения"
NO_ORIGINAL = "Нет исходного"
NO_ESTIMATE = "Нет оценки"

BASE, TITLE, SUBTITLE, LABEL, INPUT, HEADER, RAW_HEADER, NOTE, NUMBER, EMPTY, GOOD, BAD, ESTIMATED, WRAPPED = range(14)

XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CONTENT_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml."
XML_ESCAPE = re.compile(r"_x[0-9a-fA-F]{4}_|[\x00-\x08\x0B\x0C\x0D\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")

ROW_COLUMNS = [
    ("Срок", 20), ("Режим строки", 24), ("Рабочее значение", 18), ("Источник результата", 22),
    ("Исходное значение", 18), ("Решение алгоритма", 20), ("Контроль архива", 19),
    ("Причины и исходные флаги", 58), ("Внешняя оценка", 18), ("Откуда взята оценка", 53),
    ("Исходный файл", 54), ("Номер строки", 15), ("Причины контроля архива", 54),
]

ESTIMATE_REASONS = {
    "duplicate_term": "Внешняя оценка недоступна для повторяющегося срока.",
    "subhourly": "Внешние модели дают часовые оценки. У срока есть минуты.",
    "ambiguous_local_time": "Час неоднозначен в выбранном часовом поясе.",
    "incomplete_day": "Период внешних данных для этого срока ещё не завершён.",
    "unsupported_parameter": "Для показателя нет сопоставимых внешних данных.",
    "unknown_pressure_reference": "Тип исходного давления не подтверждён как давление у поверхности.",
    "fewer_than_two_sources": "Для часа есть данные менее чем из двух внешних источников.",
}


def _column(number):
    letters = ""
    while number:
        number, digit = divmod(number - 1, 26)
        letters = chr(65 + digit) + letters
    return letters


def _text(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if len(value) > 32767 or (len(value) > 16383 and len(value.encode("utf-16-le", "surrogatepass")) > 65534):
        raise ValueError("Текст одной ячейки превышает допустимые для Excel 32 767 символов")
    # SpreadsheetML reserves its own escape syntax even inside inline strings.
    value = XML_ESCAPE.sub(
        lambda match: "_x005F_" + match[0][1:] if len(match[0]) > 1
        else f"_x{ord(match[0]):04X}_",
        value,
    )
    return escape(value)


def _cell(ref, value, style=BASE):
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">'
        f'{_text(value)}</t></is></c>'
    )


def _formula(ref, expression, value, style=BASE):
    kind = ' t="str"' if value is None or isinstance(value, str) else ""
    cached = "" if value is None else escape(str(value))
    return f'<c r="{ref}" s="{style}"{kind}><f>{escape(expression)}</f><v>{cached}</v></c>'


def _row(number, cells, height=None):
    size = f' ht="{height}" customHeight="1"' if height else ""
    return f'<row r="{number}"{size}>' + "".join(cells) + "</row>"


def _validation(target, values, title):
    return (
        f'<dataValidation type="list" errorStyle="stop" allowBlank="1" showInputMessage="1" '
        f'showErrorMessage="1" errorTitle="Режим из списка" error="Выберите один из вариантов списка." '
        f'promptTitle="{title}" prompt="Нажмите стрелку и выберите вариант." sqref="{target}">'
        f'<formula1>"{",".join(values)}"</formula1></dataValidation>'
    )


def _sheet_open(columns, last_row, *, frozen_rows=0, frozen_columns=0, height=18):
    dimension = f"A1:{_column(len(columns))}{last_row}"
    view = '<sheetView showGridLines="0" workbookViewId="0">'
    if frozen_rows or frozen_columns:
        visible = f"{_column(frozen_columns + 1)}{frozen_rows + 1}"
        pane = "bottomRight" if frozen_columns else "bottomLeft"
        splits = f' xSplit="{frozen_columns}"' if frozen_columns else ""
        view += (
            f'<pane{splits} ySplit="{frozen_rows}" topLeftCell="{visible}" activePane="{pane}" state="frozen"/>'
            f'<selection pane="{pane}" activeCell="{visible}" sqref="{visible}"/>'
        )
    widths = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, (_, width) in enumerate(columns, start=1)
    )
    return (
        f'{XML}<worksheet xmlns="{MAIN_NS}"><dimension ref="{dimension}"/>'
        f'<sheetViews>{view}</sheetView></sheetViews>'
        f'<sheetFormatPr defaultRowHeight="{height}"/><cols>{widths}</cols><sheetData>'
    )


def _header(number, columns, original_start=None):
    return _row(number, (
        _cell(f"{_column(index)}{number}", label, RAW_HEADER if original_start and index >= original_start else HEADER)
        for index, (label, _) in enumerate(columns, start=1)
    ), 32)


def _conditional_format(last_row):
    conditions = (
        (f"C2:D{last_row}", (
            (0, f'$D2="{ORIGINAL}"'),
            (1, f'$D2="{REJECTED}"'),
            (2, f'$D2="{ESTIMATE}"'),
            (3, f'OR($D2="{MISSING}",$D2="{NO_ORIGINAL}",$D2="{NO_ESTIMATE}")'),
        )),
        (f"E2:E{last_row}", (
            (0, f'AND(LEN($E2)>0,$D2="{ORIGINAL}")'),
            (1, f'AND(LEN($E2)>0,$D2<>"{ORIGINAL}")'),
            (3, 'LEN($E2)=0'),
        )),
    )
    priority = 0
    chunks = []
    for target, rules in conditions:
        chunks.append(f'<conditionalFormatting sqref="{target}">')
        for color, formula in rules:
            priority += 1
            chunks.append(
                f'<cfRule type="expression" dxfId="{color}" priority="{priority}" stopIfTrue="1">'
                f'<formula>{escape(formula)}</formula></cfRule>'
            )
        chunks.append("</conditionalFormatting>")
    return "".join(chunks)


def _decision(value, keep):
    if value is None:
        return MISSING, EMPTY
    return ("Оставить", GOOD) if keep else (EXCLUDE, BAD)


def _result_formula(number):
    source = f'E{number}'
    estimate = f'I{number}'
    manual = f'B{number}'
    decision = f'F{number}'
    global_mode = f"'{GUIDE}'!$B$5"
    return (
        f'IF({global_mode}="{EXCLUDE_ALL}","{REJECTED}",'
        f'IF({global_mode}="{RESTORE_ALL}",IF(LEN({source})>0,"{ORIGINAL}","{MISSING}"),'
        f'IF({manual}="{EXCLUDE}","{REJECTED}",'
        f'IF({manual}="{RESTORE}",IF(LEN({source})>0,"{ORIGINAL}","{NO_ORIGINAL}"),'
        f'IF({manual}="{USE_ESTIMATE}",IF(ISNUMBER({estimate}),"{ESTIMATE}","{NO_ESTIMATE}"),'
        f'IF({decision}="Оставить","{ORIGINAL}",IF(ISNUMBER({estimate}),"{ESTIMATE}",'
        f'IF(LEN({source})>0,"{REJECTED}","{MISSING}")))))))'
    )


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _estimate_detail(item):
    note = item.get("estimate_sources") or ""
    provenance = item.get("provenance") or {}
    if not note or not provenance or not isinstance(note, str):
        return note
    lines = [note]
    if provenance.get("time_utc"):
        lines.append(f"Срок UTC: {provenance['time_utc']}")
    elif provenance.get("start_utc"):
        lines.append(f"Начало часа UTC: {provenance['start_utc']}")
    for source in provenance.get("sources", []):
        name = source.get("model") or source.get("name")
        value = _number(source.get("value"))
        if name and value is not None:
            unit = source.get("unit") or provenance.get("unit") or ""
            lines.append(f"{name}: {value} {unit}".rstrip())
    return "\n".join(lines)


def _write_review_rows(output, fields, observations, param):
    columns = ROW_COLUMNS + [(field, max(12, min(29, len(field) + 5))) for field in fields]
    addresses = [_column(i) for i in range(len(ROW_COLUMNS) + 1, len(columns) + 1)]
    last_row = len(observations) + 1
    output.write(_sheet_open(columns, last_row, frozen_rows=1, frozen_columns=2, height=24).encode())
    output.write(_header(1, columns, len(ROW_COLUMNS) + 1).encode())
    counts = Counter()
    for index, item in enumerate(observations, start=2):
        raw = item.get("original") or {}
        value, estimate = _number(item.get("value")), _number(item.get("estimate"))
        raw_value = raw.get(param)
        original = value
        if original is None and isinstance(raw_value, str) and raw_value.strip():
            original = raw_value
        keep = bool(item["keep"]) and value is not None
        estimate_source = _estimate_detail(item)
        if keep:
            kind, result = ORIGINAL, value
        elif estimate is not None:
            kind, result = ESTIMATE, estimate
        else:
            kind, result = (REJECTED if original is not None else MISSING), None
        counts[kind] += 1
        algorithm, decision_style = _decision(original, keep)
        official, official_style = _decision(value, item["official_keep"])
        if item.get("official_status") in {"conflict", "flagged"}:
            official, official_style = EXCLUDE, BAD
        reasons = [
            "За этот час в исходном файле нет наблюдений." if reason == "created_hour" else reason
            for reason in item["reasons"]
        ]
        if message := ESTIMATE_REASONS.get(item.get("estimate_reason")):
            reasons.append(message)
        official_reasons = "\n".join(item.get("official_reasons") or [])
        if raw.get("qc_flag"):
            reasons.append(f"Флаги архива: {raw['qc_flag']}")
        cells = [
            _cell(f"A{index}", item["time"]),
            _cell(f"B{index}", ALGORITHM, INPUT),
            _formula(f"C{index}", f'IF(D{index}="{ORIGINAL}",E{index},IF(D{index}="{ESTIMATE}",I{index},""))', result, NUMBER),
            _formula(f"D{index}", _result_formula(index), kind),
            _cell(f"E{index}", original, NUMBER),
            _cell(f"F{index}", algorithm, decision_style),
            _cell(f"G{index}", official, official_style),
            _cell(f"H{index}", "\n".join(reasons), WRAPPED),
            _cell(f"I{index}", estimate, ESTIMATED if estimate is not None else EMPTY),
            _cell(f"J{index}", estimate_source, WRAPPED),
            _cell(f"K{index}", item["source"]),
            _cell(f"L{index}", item["line"]),
            _cell(f"M{index}", official_reasons, WRAPPED),
        ]
        cells.extend(
            _cell(f"{col}{index}", str(raw[field]) if raw.get(field) is not None else None)
            for col, field in zip(addresses, fields)
        )
        lines = max(
            sum(max(1, (len(line) + 54) // 55) for line in detail.split("\n"))
            for detail in ("\n".join(reasons), str(estimate_source), official_reasons)
        )
        height = min(144, 8 + 12 * lines) if lines > 1 else None
        output.write(_row(index, cells, height).encode())
    output.write(b"</sheetData>")
    if observations:
        output.write((
            f'<autoFilter ref="A1:{_column(len(columns))}{last_row}"/>'
            + _conditional_format(last_row)
            + '<dataValidations count="1">'
            + _validation(f"B2:B{last_row}", [ALGORITHM, RESTORE, EXCLUDE, USE_ESTIMATE], "Решение по сроку")
            + '</dataValidations>'
        ).encode())
    output.write(b"</worksheet>")
    return counts


def _write_guide(output, report, counts):
    count_labels = [
        (10, "Исходных в рабочем ряду", [ORIGINAL], GOOD),
        (11, "Внешних оценок в рабочем ряду", [ESTIMATE], ESTIMATED),
        (12, "Исключено", [REJECTED], BAD),
        (13, "Строк без рабочего значения", [REJECTED, MISSING, NO_ORIGINAL, NO_ESTIMATE], EMPTY),
    ]
    notes = [
        ("Как изменить результат", HEADER),
        ("В B5 действует общий режим. «Вернуть все исходные» включает отсечённые числа и непустой исходный текст. Пропуски остаются пустыми.", NOTE),
        ("«Исключить все» очищает рабочие значения, включая внешние оценки. Общий режим имеет приоритет над решениями по строкам.", NOTE),
        ("Верните «По алгоритму» в B5, чтобы снова применить решения в колонке B листа «Сроки». По умолчанию там действует алгоритм.", NOTE),
        ("Пример. Для исключённого наблюдения выберите «Вернуть исходное» в его строке. Чтобы отменить эту правку, снова выберите там «По алгоритму».", NOTE),
        ("«Использовать оценку» подставляет её из колонки I, если она есть. По алгоритму оценка используется, когда исходник отсутствует или исключён.", NOTE),
        ("Как прочитать таблицу", HEADER),
        ("Зелёный — используется исходное наблюдение. Красный — исключено. Лиловый — внешняя оценка. Серый — отсутствующее значение.", NOTE),
        ("Файл и номер строки указывают на источник. Исходные столбцы файла сохранены как текст. Для добавленных сроков исходные поля пусты.", NOTE),
        ("Решение алгоритма и контроль архива показывают состояние при выгрузке. Причины объясняют решение алгоритма и приводят исходные флаги.", NOTE),
    ]
    snapshots = report.get("summaries") or {}
    if snapshots.get("daily") or snapshots.get("monthly"):
        notes.append(("Снимки рассчитаны при выгрузке по отобранным наблюдениям. Внешние оценки и ручные правки отражаются на листе «Сроки» и в счётчиках «Управление».", NOTE))
    external = report.get("external") or {}
    if external:
        notes.append(("Внешние данные", HEADER))
        notes.append((f"Часовой пояс исходных сроков для сопоставления: {external.get('timezone', '—')}. Колонка J содержит срок UTC и значения внешних моделей.", NOTE))
        notes.extend(
            (f"{source['model']} · {source['url']}", NOTE)
            for source in (external.get("sources") or [])
            if source.get("model") and source.get("url")
        )
    if report.get("warnings"):
        notes.append(("Замечания при выгрузке", HEADER))
        notes.extend((warning, NOTE) for warning in report["warnings"])
    merges = ["A1:F1", "A2:F2", "A9:F9"]
    columns = [("", width) for width in (37, 26, 16, 16, 16, 16)]
    output.write(_sheet_open(columns, 14 + len(notes)).encode())
    descriptions = {
        1: ("Аудит метеонаблюдений", TITLE, 36),
        2: ("Настройте режим книги и проверьте рабочий ряд на листе «Сроки».", SUBTITLE, 25),
        9: ("Рабочий ряд по текущему режиму", HEADER, 23),
    }
    for index in (1, 2):
        label, style, height = descriptions[index]
        output.write(_row(index, [_cell(f"A{index}", label, style)], height).encode())
    param = report.get("label") or report["param"]
    if report.get("unit"):
        param = f"{param} ({report['unit']})"
    details = [
        (4, "Станция", report.get("station_label") or report["station"], LABEL),
        (5, "Режим книги", ALGORITHM, INPUT),
        (6, "Параметр", param, LABEL),
        (7, "Период", f"{report['start']} — {report['end']}", LABEL),
        (8, "Строк в книге", len(report["rows"]), NUMBER),
    ]
    for index, label, value, style in details:
        output.write(_row(index, [_cell(f"A{index}", label, LABEL), _cell(f"B{index}", value, style)], 23).encode())
    label, style, height = descriptions[9]
    output.write(_row(9, [_cell("A9", label, style)], height).encode())
    last = max(2, len(report["rows"]) + 1)
    for index, label, kinds, style in count_labels:
        expression = "+".join(f'COUNTIF(\'{ROWS}\'!$D$2:$D${last},"{kind}")' for kind in kinds)
        cells = [_cell(f"A{index}", label, LABEL), _formula(f"B{index}", expression, sum(counts[kind] for kind in kinds), style)]
        output.write(_row(index, cells, 23).encode())
    for index, (note, style) in enumerate(notes, start=15):
        merges.append(f"A{index}:F{index}")
        height = 23 if style == HEADER else max(34, min(144, 12 * (2 + len(note) // 140)))
        output.write(_row(index, [_cell(f"A{index}", note, style)], height).encode())
    output.write((
        f'</sheetData><mergeCells count="{len(merges)}">'
        + "".join(f'<mergeCell ref="{target}"/>' for target in merges)
        + '</mergeCells><dataValidations count="1">'
        + _validation("B5", [ALGORITHM, RESTORE_ALL, EXCLUDE_ALL], "Режим для всех сроков")
        + '</dataValidations></worksheet>'
    ).encode())


def _write_snapshot(output, rows, monthly):
    period = [("Начало", 15, "start"), ("Конец", 15, "end")] if monthly else [("Сутки", 15, "day")]
    metrics = [
        ("Пригодных суток", 15, "valid_days"), ("Неполных дней подряд", 17, "longest_gap"),
    ] if monthly else [("Пригодных интервалов", 15, "slots")]
    metrics = [
        ("Принят", 11, "valid"), ("Пригодных наблюдений", 17, "n"), *metrics,
        ("Среднее", 14, "mean"), ("Минимум", 14, "min"), ("Максимум", 14, "max"), ("Причина", 44, "reason"),
    ]
    first = period + [("Исходных строк", 16, "raw_n")]
    columns = [(label, width) for label, width, _ in first]
    columns += [(label, width) for _ in range(2) for label, width, _ in metrics]
    left_start = _column(len(first) + 1)
    right_start = _column(len(first) + len(metrics) + 1)
    last_col = _column(len(columns))
    last_row = len(rows) + 3
    output.write(_sheet_open(columns, last_row, frozen_rows=3, frozen_columns=1).encode())
    output.write(_row(1, [_cell("A1", "Снимок при выгрузке. Рабочие значения после ручных правок видны на листе «Сроки».", NOTE)], 26).encode())
    output.write(_row(2, [_cell(f"{left_start}2", "Рабочий ряд при выгрузке", SUBTITLE),
                          _cell(f"{right_start}2", "Официальный ряд", SUBTITLE)], 24).encode())
    output.write(_header(3, columns).encode())
    for index, item in enumerate(rows, start=4):
        cells = [_cell(f"{_column(col)}{index}", item.get(key)) for col, (_, _, key) in enumerate(first, start=1)]
        col = len(first)
        for name in ("working", "official"):
            group = item.get(name) or {}
            for _, _, key in metrics:
                col += 1
                value = group.get(key)
                style = WRAPPED if key == "reason" else NUMBER
                if isinstance(value, bool):
                    value, style = ("Да", GOOD) if value else ("Нет", BAD)
                cells.append(_cell(f"{_column(col)}{index}", value, style))
        output.write(_row(index, cells, 32).encode())
    output.write((
        f'</sheetData><autoFilter ref="A3:{last_col}{last_row}"/><mergeCells count="3">'
        f'<mergeCell ref="A1:{last_col}1"/><mergeCell ref="{left_start}2:{_column(len(first) + len(metrics))}2"/>'
        f'<mergeCell ref="{right_start}2:{last_col}2"/></mergeCells></worksheet>'
    ).encode())


def _styles():
    palette = [
        ("1A6249", "E4F5ED"), ("9E2431", "FCE8EA"), ("573B87", "F0E9FA"), ("536374", "EDF0F3"),
    ]
    fonts = [
        ("1D2C37", 10, False), ("FFFFFF", 22, True), ("264236", 11, True),
        ("FFFFFF", 10, True), ("344558", 10, True), *[(fg, 10, True) for fg, _ in palette],
    ]
    fills = ["124736", "DDEBE3", "FFF1C4", "2B3E51", "F5F8F7", *[bg for _, bg in palette]]
    # fontId, fillId, wrapText, horizontal alignment. The first two fill IDs are built-ins.
    cell_styles = [
        (0, 0, False, ""), (1, 2, False, ""), (2, 3, True, ""), (4, 0, False, ""),
        (0, 4, False, ""), (3, 2, True, ""), (3, 5, True, ""), (0, 6, True, ""),
        (0, 0, False, "right"), (8, 10, False, ""), (5, 7, False, ""),
        (6, 8, False, ""), (7, 9, False, ""), (0, 0, True, ""),
    ]
    font_xml = "".join(
        f'<font>{"<b/>" if bold else ""}<sz val="{size}"/><color rgb="FF{color}"/><name val="Arial"/><family val="2"/></font>'
        for color, size, bold in fonts
    )
    def fill_xml(color):
        return f'<fill><patternFill patternType="solid"><fgColor rgb="FF{color}"/><bgColor indexed="64"/></patternFill></fill>'

    styles_xml = "".join(
        f'<xf numFmtId="0" fontId="{font}" fillId="{fill}" borderId="0" xfId="0" applyAlignment="1">'
        f'<alignment vertical="center" wrapText="{int(wrap)}"'
        + (f' horizontal="{align}"' if align else "") + '/></xf>'
        for font, fill, wrap, align in cell_styles
    )
    differential = "".join(
        f'<dxf><font><color rgb="FF{fg}"/></font>'
        f'<fill><patternFill><bgColor rgb="FF{bg}"/></patternFill></fill></dxf>'
        for fg, bg in palette
    )
    return (
        f'{XML}<styleSheet xmlns="{MAIN_NS}"><fonts count="{len(fonts)}">{font_xml}</fonts>'
        f'<fills count="{len(fills) + 2}"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        + "".join(map(fill_xml, fills)) + '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(cell_styles)}">{styles_xml}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        f'<dxfs count="{len(palette)}">{differential}</dxfs></styleSheet>'
    )


def _write_package(workbook, names):
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{number}" r:id="rId{number}"/>'
        for number, name in enumerate(names, start=1)
    )
    sheet_types = "".join(
        f'<Override PartName="/xl/worksheets/sheet{number}.xml" ContentType="{CONTENT_MAIN}worksheet+xml"/>'
        for number in range(1, len(names) + 1)
    )
    sheet_links = "".join(
        f'<Relationship Id="rId{number}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{number}.xml"/>'
        for number in range(1, len(names) + 1)
    )
    workbook.writestr("[Content_Types].xml", (
        f'{XML}<Types xmlns="{CONTENT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{CONTENT_MAIN}sheet.main+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{CONTENT_MAIN}styles+xml"/>'
        f'{sheet_types}</Types>'
    ))
    workbook.writestr("_rels/.rels", (
        f'{XML}<Relationships xmlns="{PACKAGE_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    ))
    workbook.writestr("xl/workbook.xml", (
        f'{XML}<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
        f'<bookViews><workbookView activeTab="0"/></bookViews><sheets>{sheets}</sheets>'
        '<calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1"/></workbook>'
    ))
    workbook.writestr("xl/_rels/workbook.xml.rels", (
        f'{XML}<Relationships xmlns="{PACKAGE_NS}">{sheet_links}'
        f'<Relationship Id="rId{len(names) + 1}" Type="{REL_NS}/styles" Target="styles.xml"/></Relationships>'
    ))
    workbook.writestr("xl/styles.xml", _styles())


def export_xlsx(report):
    """Return an editable XLSX, with archive cells written as non-executable text."""
    observations = report["rows"]
    fields = report["fields"]
    if len(observations) > REVIEW_XLSX_MAX_ROWS:
        raise ValueError(f"Книга вмещает до {REVIEW_XLSX_MAX_ROWS:,} строк. Выберите более короткий период.".replace(",", " "))
    if len(fields) + len(ROW_COLUMNS) > 16384:
        raise ValueError("Исходный файл содержит слишком много столбцов для одной книги Excel")
    snapshots = report.get("summaries") or {}
    additional = [
        (name, snapshots[key], key == "monthly")
        for key, name in (("daily", "Сутки · снимок"), ("monthly", "Месяцы · снимок"))
        if snapshots.get(key)
    ]
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=4) as workbook:
        _write_package(workbook, [GUIDE, ROWS, *[name for name, _, _ in additional]])
        with workbook.open("xl/worksheets/sheet2.xml", "w") as part:
            counts = _write_review_rows(part, fields, observations, report["param"])
        with workbook.open("xl/worksheets/sheet1.xml", "w") as part:
            _write_guide(part, report, counts)
        for index, (_, rows, monthly) in enumerate(additional, start=3):
            with workbook.open(f"xl/worksheets/sheet{index}.xml", "w") as part:
                _write_snapshot(part, rows, monthly)
    return output.getvalue()
