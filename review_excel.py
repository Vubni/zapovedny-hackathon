"""Write a macro-free, interactive XLSX review using only the standard library."""

from collections import Counter
from io import BytesIO
import json
from math import isclose, isfinite
import re
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

from review_filter import FILTER_LABELS


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
MODEL_ESTIMATE = "Оценка модели"
ADJUSTED_ESTIMATE = "Модель с поправкой"
REJECTED = "Исключено"
MISSING = "Нет значения"
NO_ORIGINAL = "Нет исходного"
NO_ESTIMATE = "Нет оценки"

BASE, TITLE, SUBTITLE, LABEL, INPUT, HEADER, RAW_HEADER, NOTE, NUMBER, EMPTY, GOOD, BAD, ESTIMATED, WRAPPED = range(14)
LINK = 14

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
    ("Причины и исходные флаги", 58), ("Итог оценки", 18), ("Метеосервисы · справочно", 53),
    ("Исходный файл", 54), ("Номер строки", 15), ("Причины контроля архива", 54),
]
ESTIMATE_COLUMNS = [
    ("Локальная оценка", 18), ("Поправка по сверке", 18), ("Причина решения по оценке", 57),
    ("Расчёт локальной модели", 55), ("Прежняя внешняя оценка · архив", 19),
]

ESTIMATE_REASONS = {
    "duplicate_term": "Внешняя оценка недоступна для повторяющегося срока.",
    "subhourly": "Внешние модели дают часовые оценки. У срока есть минуты.",
    "ambiguous_local_time": "Час неоднозначен в выбранном часовом поясе.",
    "incomplete_day": "Период внешних данных для этого срока ещё не завершён.",
    "unsupported_parameter": "Для показателя нет сопоставимых внешних данных.",
    "unknown_pressure_reference": "Тип исходного давления не подтверждён как давление у поверхности.",
    "fewer_than_two_sources": "Для часа есть данные менее чем из двух внешних источников.",
    "outside_parameter_range": "Внешняя оценка вне допустимого диапазона показателя.",
}

VERIFICATION_REASONS = {
    "adjusted": "Модели согласуются между собой и с локальной оценкой. Применена ограниченная поправка.",
    "already_equal": "Локальная оценка совпала со средним по метеосервисам.",
    "no_local_estimate": "Для срока нет локальной оценки. Значения метеосервисов приведены справочно.",
    "external_unavailable": "Сверка недоступна. Сохранена локальная оценка.",
    "providers_disagree": "Расхождение метеосервисов превышает порог. Сохранена локальная оценка.",
    "local_departure": "Расхождение среднего метеосервисов с локальной оценкой превышает порог. Сохранена локальная оценка.",
    "unsupported_parameter": "Сверка для этого показателя не предусмотрена. Сохранена локальная оценка.",
    "fewer_than_two_sources": "Для сверки доступно меньше двух метеосервисов. Сохранена локальная оценка.",
    "outside_parameter_range": "Среднее метеосервисов вышло за границы показателя. Сохранена локальная оценка.",
    "insufficient_local_validation": "Для поправки недостаточно проверочных сроков модели. Сохранена локальная оценка.",
    "within_local_precision": "Поправка округляется до нуля при точности локальной модели.",
    "unvalidated_correction": "Поправка не прошла проверку на предыдущих наблюдениях станции. Сохранена локальная оценка.",
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
            (2, f'OR($D2="{MODEL_ESTIMATE}",$D2="{ADJUSTED_ESTIMATE}")'),
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


def _result_formula(number, include_estimates):
    source = f'E{number}'
    estimate = f'I{number}'
    manual = f'B{number}'
    decision = f'F{number}'
    global_mode = f"'{GUIDE}'!$B$5"
    estimate_kind = (
        f'IF(O{number}<>0,"{ADJUSTED_ESTIMATE}","{MODEL_ESTIMATE}")'
        if include_estimates else f'"{MODEL_ESTIMATE}"'
    )
    return (
        f'IF({global_mode}="{EXCLUDE_ALL}","{REJECTED}",'
        f'IF({global_mode}="{RESTORE_ALL}",IF(LEN({source})>0,"{ORIGINAL}","{MISSING}"),'
        f'IF({manual}="{EXCLUDE}","{REJECTED}",'
        f'IF({manual}="{RESTORE}",IF(LEN({source})>0,"{ORIGINAL}","{NO_ORIGINAL}"),'
        f'IF({manual}="{USE_ESTIMATE}",IF(ISNUMBER({estimate}),{estimate_kind},"{NO_ESTIMATE}"),'
        f'IF({decision}="Оставить","{ORIGINAL}",IF(ISNUMBER({estimate}),{estimate_kind},'
        f'IF(LEN({source})>0,"{REJECTED}","{MISSING}")))))))'
    )


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _external_reference(item):
    reference = item.get("external_reference")
    if reference:
        return reference
    provenance = item.get("provenance") or {}
    if provenance.get("sources") or provenance.get("kind") in {"hourly_reanalysis", "subhour_reanalysis"}:
        return {"provenance": provenance, "source": item.get("estimate_sources")}
    return {}


def _external_detail(reference):
    provenance = reference.get("provenance") or {}
    lines = []
    if provenance.get("time_utc"):
        lines.append(f"Срок UTC: {provenance['time_utc']}")
    elif provenance.get("start_utc"):
        lines.append(f"Начало часа UTC: {provenance['start_utc']}")
    for source in provenance.get("sources") or []:
        name = source.get("model") or source.get("name")
        value = _number(source.get("value"))
        if name and value is not None:
            unit = source.get("unit") or provenance.get("unit") or ""
            lines.append(f"{name}: {value} {unit}".rstrip())
    mean = _number(reference.get("value"))
    if mean is not None:
        lines.append(f"Среднее для сверки: {mean} {provenance.get('unit', '')}".rstrip())
    if not lines and reference.get("source"):
        lines.append(str(reference["source"]))
    return "\n".join(lines)


def _local_detail(provenance, fallback):
    if provenance.get("kind") != "station_model":
        return fallback or ""
    lines = ["Интерполяция по двум исходным измерениям"]
    unit = provenance.get("unit", "")
    for anchor in provenance.get("anchors") or []:
        lines.append(f"Наблюдение {anchor['time']}: {anchor['value']} {unit}".rstrip())
    if provenance.get("training_end"):
        lines.append(f"Калибровка станции: {provenance['bias']} {unit} по данным до {provenance['training_end']}")
        validation = provenance.get("validation") or {}
        if validation.get("mae") is not None:
            lines.append(f"Средняя абсолютная ошибка {validation['mae']} {unit} на {validation['test_n']} сроках")
    return "\n".join(lines)


def _estimate_trace(item):
    provenance = item.get("provenance") or {}
    verification = item.get("verification") or provenance.get("verification") or {}
    method = item.get("estimate_method")
    saved = _number(item.get("estimate"))
    local = _number(item.get("local_estimate"))
    is_model = method == "model" or (method is None and provenance.get("kind") == "station_model")
    if is_model and local is None:
        local = saved
    reference = _external_reference(item)
    historical = saved if saved is not None and local is None and method != "model_adjusted" and not is_model else None
    decision = verification.get("decision")
    reason = VERIFICATION_REASONS.get(decision, "")
    calibration = verification.get("calibration") or {}
    result = local
    delta = 0 if local is not None and decision else None
    if decision == "adjusted":
        proposed = _number(verification.get("delta"))
        limits = verification.get("limits") or {}
        caps = [cap for key in ("max_delta", "local_validation_mae") if (cap := _number(limits.get(key))) is not None]
        sources = (reference.get("provenance") or {}).get("sources") or []
        independent = {source.get("id") or source.get("model") for source in sources
                       if _number(source.get("value")) is not None}
        independent.discard(None)
        verified = (
            method == "model_adjusted" and local is not None and proposed and saved is not None
            and isclose(saved, local + proposed, rel_tol=0, abs_tol=0.00005)
            and caps and abs(proposed) <= min(caps) + 0.00005
            and len(independent) >= 2 and reference.get("usable", True)
            and calibration.get("accepted") is True
        )
        if verified:
            result, delta = saved, proposed
            weight = _number(verification.get("weight"))
            if weight is not None:
                reason += f" Вес расхождения {weight:g}, предел поправки {min(caps):g}."
        else:
            reason = "Сведения о поправке неполны. В рабочий итог допускается локальная оценка."
    if historical is not None:
        reason = "Внешняя оценка прежней версии сохранена для аудита."
        reference = reference or {"source": item.get("estimate_sources")}
    elif local is None and reference and not reason:
        reason = VERIFICATION_REASONS["no_local_estimate"]
    elif local is not None and not reason:
        reason = "Локальная оценка по измерениям станции."
    if calibration:
        count = calibration.get("sample_count", 0)
        minimum = calibration.get("minimum_sample_count")
        reason += f"\nПроверочных сроков {count}" + (f" из необходимых {minimum}." if count < (minimum or 0) else ".")
        for label, key in (("MAE", "mae"), ("RMSE", "rmse")):
            before = _number(calibration.get(f"local_{key}"))
            after = _number(calibration.get(f"adjusted_{key}"))
            if before is not None and after is not None:
                reason += f" {label} {before:g} → {after:g}."
    detail = _local_detail(provenance, item.get("estimate_sources")) if local is not None else ""
    return local, result, delta, reason, detail, reference, historical


def _write_review_rows(output, fields, observations, param, include_estimates):
    columns = ROW_COLUMNS + (ESTIMATE_COLUMNS if include_estimates else [])
    original_start = len(columns) + 1
    columns.extend((field, max(12, min(29, len(field) + 5))) for field in fields)
    addresses = [_column(i) for i in range(original_start, len(columns) + 1)]
    last_row = len(observations) + 1
    output.write(_sheet_open(columns, last_row, frozen_rows=1, frozen_columns=2, height=24).encode())
    output.write(_header(1, columns, original_start).encode())
    counts = Counter()
    for index, item in enumerate(observations, start=2):
        raw = item.get("original") or {}
        value = _number(item.get("value"))
        local, estimate, delta, verification_reason, local_detail, reference, historical = _estimate_trace(item)
        raw_value = raw.get(param)
        original = value
        if original is None and isinstance(raw_value, str) and raw_value.strip():
            original = raw_value
        keep = bool(item["keep"]) and value is not None
        external_detail = _external_detail(reference)
        if keep:
            kind, result = ORIGINAL, value
        elif estimate is not None:
            kind, result = (ADJUSTED_ESTIMATE if delta else MODEL_ESTIMATE), estimate
        else:
            kind, result = (REJECTED if original is not None else MISSING), None
        counts[kind] += 1
        if historical is not None:
            counts["historical_estimates"] += 1
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
        result_kind = f'OR(D{index}="{MODEL_ESTIMATE}",D{index}="{ADJUSTED_ESTIMATE}")'
        cells = [
            _cell(f"A{index}", item["time"]),
            _cell(f"B{index}", ALGORITHM, INPUT),
            _formula(f"C{index}", f'IF(D{index}="{ORIGINAL}",E{index},IF({result_kind},I{index},""))', result, NUMBER),
            _formula(f"D{index}", _result_formula(index, include_estimates), kind),
            _cell(f"E{index}", original, NUMBER),
            _cell(f"F{index}", algorithm, decision_style),
            _cell(f"G{index}", official, official_style),
            _cell(f"H{index}", "\n".join(reasons), WRAPPED),
            _cell(f"I{index}", estimate, ESTIMATED if estimate is not None else EMPTY),
            _cell(f"J{index}", external_detail, WRAPPED),
            _cell(f"K{index}", item["source"]),
            _cell(f"L{index}", item["line"]),
            _cell(f"M{index}", official_reasons, WRAPPED),
        ]
        if include_estimates:
            cells.extend((
                _cell(f"N{index}", local, NUMBER),
                _cell(f"O{index}", delta, NUMBER),
                _cell(f"P{index}", verification_reason, WRAPPED),
                _cell(f"Q{index}", local_detail, WRAPPED),
                _cell(f"R{index}", historical, NUMBER),
            ))
        cells.extend(
            _cell(f"{col}{index}", str(raw[field]) if raw.get(field) is not None else None)
            for col, field in zip(addresses, fields)
        )
        lines = max(
            sum(max(1, (len(line) + 54) // 55) for line in detail.split("\n"))
            for detail in ("\n".join(reasons), external_detail, official_reasons, verification_reason, str(local_detail))
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


def _sources(report):
    providers = {}
    groups = [(report.get("external") or {}).get("sources") or []]
    for item in report["rows"]:
        reference = _external_reference(item)
        if sources := (reference.get("provenance") or {}).get("sources"):
            groups.append(sources)
    for sources in groups:
        for source in sources:
            label = source.get("model") or source.get("name")
            url = source.get("url")
            if label and isinstance(url, str):
                if source.get("mode") == "snapshot":
                    label += " · сохранённый снимок"
                providers.setdefault(url, label)
    return providers


def _safe_link(url):
    try:
        target = urlsplit(url)
        return (
            target.scheme == "https"
            and target.netloc.lower() in {"archive-api.open-meteo.com", "power.larc.nasa.gov"}
            and not any(ord(char) <= 32 for char in url)
        )
    except ValueError:
        return False


def _write_guide(output, report, counts):
    count_labels = [
        (10, "Исходных в рабочем ряду", [ORIGINAL], GOOD),
        (11, "Оценок в рабочем ряду", [MODEL_ESTIMATE, ADJUSTED_ESTIMATE], ESTIMATED),
        (12, "Исключено", [REJECTED], BAD),
        (13, "Строк без рабочего значения", [REJECTED, MISSING, NO_ORIGINAL, NO_ESTIMATE], EMPTY),
    ]
    notes = [
        ("Как изменить результат", HEADER),
        ("В B5 действует общий режим. «Вернуть все исходные» включает отсечённые числа и непустой исходный текст. Пропуски остаются пустыми.", NOTE),
        ("«Исключить все» очищает рабочие значения, включая расчётные оценки. Общий режим имеет приоритет над решениями по строкам.", NOTE),
        ("Верните «По алгоритму» в B5, чтобы снова применить решения в колонке B листа «Сроки». По умолчанию там действует алгоритм.", NOTE),
        ("Пример. Для исключённого наблюдения выберите «Вернуть исходное» в его строке. Чтобы отменить эту правку, снова выберите там «По алгоритму».", NOTE),
        ("«Использовать оценку» берёт итог модели из колонки I. По алгоритму этот итог применяется, когда исходник отсутствует или исключён.", NOTE),
        ("Как прочитать таблицу", HEADER),
        ("Зелёный — используется исходное наблюдение. Красный — исключено. Лиловый — расчётная оценка. Серый — отсутствующее значение.", NOTE),
        ("Файл и номер строки указывают на источник. Исходные столбцы файла сохранены как текст. Для добавленных сроков исходные поля пусты.", NOTE),
        ("Решение алгоритма и контроль архива показывают состояние при выгрузке. Причины объясняют решение алгоритма и приводят исходные флаги.", NOTE),
    ]
    settings = report.get("settings") or {}
    parameter = report["param"]
    ranges = settings.get("ranges", {})
    if parameter in ranges:
        minimum, maximum = ranges[parameter]
        unit = report.get("unit", "")
        notes.extend([
            ("Настройки контроля", HEADER),
            (f"Рабочие числовые границы {minimum}–{maximum} {unit}.", NOTE),
        ])
        filters = report.get("filters", ())
        for method in filters:
            label = FILTER_LABELS.get(method, method)
            if method == "conflicts":
                mode = "выбирается одна запись" if settings.get("conflict_policy") == "best" else "исключаются все записи срока"
                notes.append((f"{label} · {mode}.", NOTE))
                continue
            threshold = settings.get("thresholds", {}).get(method, {}).get(parameter)
            if threshold is not None:
                suffix = "ч" if method == "stuck" else unit
                notes.append((f"{label} · порог {threshold} {suffix}.", NOTE))
    snapshots = report.get("summaries") or {}
    if snapshots.get("daily") or snapshots.get("monthly"):
        notes.append(("Снимки рассчитаны при выгрузке по отобранным наблюдениям. Расчётные оценки и ручные правки отражаются на листе «Сроки» и в счётчиках «Управление».", NOTE))
    external = report.get("external") or {}
    providers = _sources(report)
    if external or providers:
        notes.append(("Сверка оценки с метеосервисами", HEADER))
        notes.append(("Локальная оценка и поправка указаны в N и O. Колонка P объясняет решение. Срок UTC и значения метеосервисов показаны в J справочно.", NOTE))
        notes.append(("Поправка допускается после проверки на предыдущих измерениях станции. В колонке P приведены ошибки до и после поправки по проверочным срокам.", NOTE))
        if external.get("timezone"):
            notes.append((f"Часовой пояс исходных сроков для сопоставления: {external['timezone']}.", NOTE))
    if counts["historical_estimates"]:
        notes.extend(
            [("Совместимость с прежними расчётами", HEADER),
             ("Колонка R хранит прежние внешние оценки для аудита. Рабочий итог использует измерения и допустимую локальную модель.", NOTE)]
        )
    if report.get("warnings"):
        notes.append(("Замечания при выгрузке", HEADER))
        notes.extend((warning, NOTE) for warning in report["warnings"])
    merges = ["A1:F1", "A2:F2", "A9:F9"]
    columns = [("", width) for width in (37, 26, 16, 16, 16, 16)]
    output.write(_sheet_open(columns, 14 + len(notes) + len(providers)).encode())
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
    links = {}
    for index, (url, provider) in enumerate(providers.items(), start=15 + len(notes)):
        address = f"A{index}"
        merges.append(f"{address}:F{index}")
        if _safe_link(url):
            links[address] = url
        style = LINK if address in links else NOTE
        output.write(_row(index, [_cell(address, f"{provider} · {url}", style)], 45).encode())
    output.write((
        f'</sheetData><mergeCells count="{len(merges)}">'
        + "".join(f'<mergeCell ref="{target}"/>' for target in merges)
        + '</mergeCells><dataValidations count="1">'
        + _validation("B5", [ALGORITHM, RESTORE_ALL, EXCLUDE_ALL], "Режим для всех сроков")
        + '</dataValidations>'
    ).encode())
    if links:
        hyperlinks = "".join(
            f'<hyperlink ref="{cell}" xmlns:r="{REL_NS}" r:id="rIdLink{index}"/>'
            for index, cell in enumerate(links, start=1)
        )
        output.write(f"<hyperlinks>{hyperlinks}</hyperlinks>".encode())
    output.write(b"</worksheet>")
    return links.values()


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
        (len(fonts), 6, True, ""),
    ]
    font_xml = "".join(
        f'<font>{"<b/>" if bold else ""}<sz val="{size}"/><color rgb="FF{color}"/><name val="Arial"/><family val="2"/></font>'
        for color, size, bold in fonts
    )
    font_xml += '<font><u/><sz val="10"/><color rgb="FF174B86"/><name val="Arial"/><family val="2"/></font>'
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
        f'{XML}<styleSheet xmlns="{MAIN_NS}"><fonts count="{len(fonts) + 1}">{font_xml}</fonts>'
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
    include_model = bool(report.get("fill_model"))
    include_estimates = include_model or bool(report.get("external")) or any(
        row and any(row.get(key) is not None for key in ("estimate", "local_estimate", "external_reference", "verification"))
        for row in observations
    )
    limit = REVIEW_XLSX_MAX_ROWS + 60_000 * include_model
    if len(observations) > limit:
        raise ValueError(f"Книга вмещает до {limit:,} строк. Выберите более короткий период.".replace(",", " "))
    if len(fields) + len(ROW_COLUMNS) + len(ESTIMATE_COLUMNS) * include_estimates > 16384:
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
            counts = _write_review_rows(part, fields, observations, report["param"], include_estimates)
        with workbook.open("xl/worksheets/sheet1.xml", "w") as part:
            links = _write_guide(part, report, counts)
        if links:
            relationships = "".join(
                f'<Relationship Id="rIdLink{index}" Type="{REL_NS}/hyperlink" Target={quoteattr(url)} TargetMode="External"/>'
                for index, url in enumerate(links, start=1)
            )
            workbook.writestr("xl/worksheets/_rels/sheet1.xml.rels", (
                f'{XML}<Relationships xmlns="{PACKAGE_NS}">{relationships}</Relationships>'
            ))
        for index, (_, rows, monthly) in enumerate(additional, start=3):
            with workbook.open(f"xl/worksheets/sheet{index}.xml", "w") as part:
                _write_snapshot(part, rows, monthly)
    return output.getvalue()
