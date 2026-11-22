"""Экспериментальный контроль архивных наблюдений с независимой сводкой кейса."""

from __future__ import annotations

import calendar
import csv
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import groupby
from statistics import median
from zipfile import ZipFile

from analytics import ARCHIVE, PARAMS, difference, mean_of
from build_data import DIRECTION, LIMITS, STATIONS, archive_text, coverage, source_member


FILTERS = {
    "conflicts": "Повторяющиеся сроки исключаются целиком либо разбираются по пригодности и близким соседям.",
    "hampel": "Хэмпел: локальная медиана и 3 масштабированных MAD, с нижним порогом отклонения для показателя.",
    "spike": "Одиночный скачок: близкие по времени соседи согласуются между собой, а центральный срок отклоняется от обоих.",
    "stuck": "Залипание: длительный непрерывный ряд одинаковых значений с учётом шага прибора и штиля.",
    "buddy": "Температура Кр1 и Кр2: интерполяция по близким срокам другой станции и поправка на местную разницу.",
}
# Fields: Hampel minimum distance, spike minimum distance, exact plateau minimum hours.
PARAM_SETTINGS = {
    "t_air": (3, 3, 12),
    "rh": (10, 10, 12),
    "wind_speed": (4, 4, 12),
    "wind_gust": (6, 6, 12),
    DIRECTION: (60, 90, 12),
    "soil_05": (2, 2, 24),
    "soil_10": (2, 2, 24),
    "soil_20": (2, 2, 24),
    "pressure_hpa": (3, 3, 6),
}
CONTEXT_MINUTES = 1440
LOCAL_TIMESPAN = {"ame60": 180, "kr1": 240, "kr2": 240}
NEARBY_GAP = {"ame60": 35, "kr1": 80, "kr2": 80}
STAMP_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::00)?")
DECIMAL_COMMA = re.compile(r"[+-]?[0-9]*,[0-9]+(?:[eE][+-]?[0-9]+)?")


@dataclass
class Observation:
    stamp: int
    time: str
    line: int | None
    source: str
    original: dict
    value: float | None
    quality: str
    flags: tuple
    calm: bool
    base_keep: bool = False
    keep: bool = False
    conflict: bool = False
    official_keep: bool = False
    official_status: str = "kept"
    official_reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def note(self, code, text, *, reject=False):
        self.notes.append((code, text))
        if reject:
            self.keep = False

    def result(self):
        return {
            "time": self.time, "line": self.line, "source": self.source, "original": self.original,
            "value": self.value, "keep": self.keep, "official_keep": self.official_keep,
            "reasons": [description for _, description in self.notes] or ["Значение принято по выбранным правилам."],
            "reason_codes": [code for code, _ in self.notes], "official_reasons": self.official_reasons,
            "official_status": self.official_status, "conflict": self.conflict,
        }


def checked_date(raw):
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from None
    if parsed.isoformat() != raw:
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД")
    return parsed


def minute_bounds(start, end):
    return start.toordinal() * 1440, (end.toordinal() + 1) * 1440 - 1


def date_at_minute(stamp):
    ordinal = min(date.max.toordinal(), max(1, stamp // 1440))
    return date.fromordinal(ordinal)


def number(raw):
    if raw is None:
        return None, "missing"
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None, "missing"
        if DECIMAL_COMMA.fullmatch(raw):
            raw = raw.replace(",", ".")
    if isinstance(raw, bool):
        return None, "invalid_number"
    try:
        value = float(raw)
    except (OverflowError, TypeError, ValueError):
        return None, "invalid_number"
    return (value, "number") if math.isfinite(value) else (None, "invalid_number")


def observation(row, line, source, param, location):
    raw_stamp = row.get("datetime")
    try:
        if not isinstance(raw_stamp, str) or not STAMP_PATTERN.fullmatch(raw_stamp):
            raise ValueError
        parsed = datetime.fromisoformat(raw_stamp)
    except ValueError:
        raise ValueError(f"{location}: ожидается срок ГГГГ-ММ-ДД ЧЧ:ММ без часового пояса") from None
    stamp = (parsed.toordinal() * 24 + parsed.hour) * 60 + parsed.minute
    time = parsed.isoformat(" ", timespec="minutes")
    qc = row.get("qc_flag", "")
    if not isinstance(qc, str):
        raise ValueError(f"{location}: qc_flag должен быть строкой")
    flags = tuple(flag for part in qc.split(";") if (flag := part.strip())) if qc else ()
    value, quality = number(row.get(param))
    calm = (param == DIRECTION and number(row.get("wind_speed"))[0] == 0
            and not any(flag.startswith("wind_speed:") for flag in flags))
    return Observation(stamp, time, line, source, row, value, quality, flags, calm)


def uploaded_observations(records, source, param):
    if not isinstance(records, list) or not records:
        raise ValueError("Загрузите непустой список наблюдений")
    for index, entry in enumerate(records, 1):
        location = f"{source}, запись {index}"
        if not isinstance(entry, dict) or any(not isinstance(key, str) or not key.strip() for key in entry):
            raise ValueError(f"{location}: ожидается объект наблюдения с непустыми названиями колонок")
        for key, value in entry.items():
            if value is None or type(value) in (str, int, bool):
                continue
            if type(value) is float and math.isfinite(value):
                continue
            raise ValueError(f"{location}: поле {key!r} должно содержать конечное число, строку, логическое значение или null")
        line = getattr(entry, "source_line", index)
        yield observation(dict(entry), line, source, param, location)


def archive_observations(station, param, first, last, fields, selected_years=None):
    first_day, last_day = date_at_minute(first), date_at_minute(last)
    start, end = first_day.isoformat(), last_day.isoformat()
    with ZipFile(ARCHIVE) as archive:
        members = set(archive.namelist())
        for year in range(first_day.year, last_day.year + 1):
            member = source_member(station, year)
            if member not in members:
                continue
            with archive_text(archive, member) as stream:
                reader = csv.DictReader(stream)
                if selected_years is None or selected_years[0] <= year <= selected_years[1]:
                    fields.update(dict.fromkeys(reader.fieldnames or ()))
                for row in reader:
                    raw_date = row["datetime"][:10]
                    if start <= raw_date <= end:
                        yield observation(row, reader.line_num, member, param, f"{member}:{reader.line_num}")


def base_quality(row, param, upper):
    lower, official_upper = LIMITS[param]
    range_flag = f"{param}:range" in row.flags
    fault = f"{param}:fault" in row.flags
    unknown = [flag for flag in row.flags if flag.startswith(f"{param}:")
               and flag not in {f"{param}:range", f"{param}:fault"}]
    if row.quality == "missing":
        row.note("missing", "В исходной записи нет значения выбранного показателя.", reject=True)
    elif row.quality == "invalid_number":
        row.note("invalid_number", "Исходное значение нельзя прочитать как конечное число.", reject=True)
    elif not lower <= row.value <= upper:
        row.note("range", f"Значение {row.value:g} вне рабочего диапазона {lower}–{upper}.", reject=True)
    elif range_flag:
        row.note("source_range_rechecked", f"Сохранён исходный флаг {param}:range. Значение {row.value:g} входит в рабочий диапазон {lower}–{upper}.")
    elif row.value > official_upper:
        row.note("expanded_range", f"Значение {row.value:g} выше порога кейса {official_upper} и входит в рабочий диапазон {lower}–{upper}.")
    if fault:
        row.note("fault", f"Исходный флаг {param}:fault отмечает сбой прибора.", reject=True)
    if unknown:
        row.note("source_flag", f"В источнике стоит флаг показателя: {', '.join(unknown)}.", reject=True)
    row.base_keep = row.keep
    if row.calm and row.base_keep:
        row.note("calm", "В исходной записи скорость ветра равна нулю. Направление пропущено при поиске временных аномалий.")

    official = row.official_reasons
    if row.conflict:
        official.append("Повтор или исходный конфликт срока. Все записи этого срока исключены по правилам кейса.")
    if range_flag or fault or unknown:
        official.append("В источнике отмечен флаг выбранного показателя.")
    if row.quality == "missing":
        official.append("Нет исходного значения выбранного показателя.")
    elif row.quality == "invalid_number":
        official.append("Исходное значение нельзя прочитать как конечное число.")
    elif not lower <= row.value <= official_upper:
        official.append(f"Значение за пределами диапазона кейса {lower}–{official_upper}.")
    row.official_keep = not official
    if row.conflict:
        row.official_status = "conflict"
    elif row.official_keep:
        row.official_status = "kept"
    elif row.quality == "missing" and not (range_flag or fault or unknown):
        row.official_status = "missing"
    else:
        row.official_status = "flagged"


def prepare(rows, param, upper, *, use_references=True):
    references, conflicts = [], []
    for _, group in groupby(rows, lambda row: row.stamp):
        group = list(group)
        conflict = len(group) > 1 or any("dup_conflict" in row.flags for row in group)
        for row in group:
            row.keep = True
            row.conflict = conflict
            base_quality(row, param, upper)
        if conflict:
            conflicts.append(group)
        elif use_references and group[0].base_keep and not group[0].calm:
            references.append(group[0])
    return references, conflicts


def nearby(stamp, references, stamps, max_gap):
    before = bisect_left(stamps, stamp) - 1
    after = bisect_right(stamps, stamp)
    if (before < 0 or after >= len(stamps) or stamp - stamps[before] > max_gap
            or stamps[after] - stamp > max_gap):
        return None
    return references[before], references[after]


def resolve_conflicts(groups, references, stamps, param, station, policy):
    max_gap = NEARBY_GAP[station]
    margin = PARAM_SETTINGS[param][0] / 2
    for group in groups:
        if policy == "off":
            for row in group:
                row.note("conflict_off", "Повтор или исходный конфликт срока сохранён в журнале. Фильтр конфликтов выключен.")
            continue
        winner = None
        why = "выбран режим исключения всего срока"
        if policy == "best":
            viable = [row for row in group if row.base_keep]
            why = "нет однозначно лучшего значения среди исходных записей"
            if len(group) > 1 and len(viable) == 1:
                winner = viable[0]
                why = "это единственная запись срока, прошедшая базовую проверку"
            elif len(viable) > 1 and not any(row.calm for row in viable):
                stamp = group[0].stamp
                anchors = nearby(stamp, references, stamps, max_gap)
                if anchors:
                    before, after = anchors
                    change = difference(after.value, before.value, param)
                    if abs(change) <= 2 * margin:
                        predicted = before.value + change * (stamp - before.stamp) / (after.stamp - before.stamp)
                        ranked = sorted((abs(difference(row.value, predicted, param)), i, row)
                                        for i, row in enumerate(viable))
                        if ranked[0][0] <= margin and ranked[1][0] - ranked[0][0] >= margin:
                            winner = ranked[0][2]
                            why = f"значение ближе остальных к интерполяции двух соседних сроков ({predicted:g})"
        for row in group:
            if row is winner:
                row.note("conflict_selected", f"Из повторов сохранена одна запись: {why}.")
            elif winner is not None:
                row.note("conflict", f"Из повторов выбрана другая запись: {why}.", reject=True)
            else:
                row.note("conflict", f"Все записи конфликтного срока исключены: {why}.", reject=True)


def mark_hampel(rows, references, stamps, param, station):
    floor = PARAM_SETTINGS[param][0]
    span, max_gap = LOCAL_TIMESPAN[station], NEARBY_GAP[station]
    for row in rows:
        if not row.base_keep or row.calm:
            continue
        stamp = row.stamp
        left_end, right_start = bisect_left(stamps, stamp), bisect_right(stamps, stamp)
        lower, upper = bisect_left(stamps, stamp - span), bisect_right(stamps, stamp + span)
        left = left_end
        while left > lower and (stamp if left == left_end else stamps[left]) - stamps[left - 1] <= max_gap:
            left -= 1
        right = right_start
        while right < upper and stamps[right] - (stamp if right == right_start else stamps[right - 1]) <= max_gap:
            right += 1
        if min(left_end - left, right - right_start) < 2 or left_end - left + right - right_start < 6:
            continue
        values = [references[i].value for i in range(left, left_end)]
        values.extend(references[i].value for i in range(right_start, right))
        if param == DIRECTION:
            radians = list(map(math.radians, values))
            sin_sum, cos_sum = sum(map(math.sin, radians)), sum(map(math.cos, radians))
            if math.hypot(sin_sum, cos_sum) < len(values) * 0.5:
                continue
            seed = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
            center = (seed + median(difference(value, seed, param) for value in values)) % 360
        else:
            center = median(values)
        mad = median(abs(difference(value, center, param)) for value in values)
        threshold = max(floor, 3 * 1.4826 * mad)
        distance = abs(difference(row.value, center, param))
        if distance > threshold:
            row.note("hampel", f"Хэмпел: отклонение {distance:.2f} от локальной медианы {center:.2f} превышает порог {threshold:.2f}.", reject=True)


def mark_spikes(rows, references, stamps, param, station):
    floor, max_gap = PARAM_SETTINGS[param][1], NEARBY_GAP[station]
    for row in rows:
        if not row.base_keep or row.calm:
            continue
        anchors = nearby(row.stamp, references, stamps, max_gap)
        if not anchors:
            continue
        before, after = anchors
        anchor_difference = abs(difference(after.value, before.value, param))
        if anchor_difference > floor / 2:
            continue
        # Unwrap both the current observation and the following neighbour about the
        # preceding neighbour. Around a bearing of 180°, either sign represents a spike.
        if param == DIRECTION:
            offset = difference(after.value, before.value, param)
            center = (before.value + offset / 2) % 360
            jump = abs(difference(row.value, center, param))
            isolated = jump - anchor_difference / 2 >= floor
        else:
            left_jump, right_jump = row.value - before.value, row.value - after.value
            jump = min(abs(left_jump), abs(right_jump))
            isolated = left_jump * right_jump > 0 and jump >= floor
        if isolated:
            row.note("spike", f"Одиночный скачок: отклонение {jump:.2f}; соседние значения {before.value:g} и {after.value:g}.", reject=True)


def mark_stuck(rows, param, station):
    minimum = PARAM_SETTINGS[param][2] * 60
    gap = 20 if station == "ame60" else 75

    def mark(run):
        if run and run[-1].stamp - run[0].stamp >= minimum:
            hours = (run[-1].stamp - run[0].stamp) / 60
            reason = f"Залипание: {len(run)} одинаковых значений {run[0].value:g} за {hours:g} ч подряд."
            for member in run:
                member.note("stuck", reason, reject=True)

    run = []
    for row in rows:
        plausible_plateau = ((param in {"wind_speed", "wind_gust"} and row.value == 0)
                             or (param == "rh" and row.value == 100))
        candidate = row.base_keep and not row.conflict and not row.calm and not plausible_plateau
        if not candidate:
            mark(run)
            run = []
            continue
        if run and (row.stamp - run[-1].stamp > gap or difference(row.value, run[-1].value, param) != 0):
            mark(run)
            run = []
        run.append(row)
    mark(run)


def buddy_temperature(stamp, peers, stamps):
    index = bisect_left(stamps, stamp)
    if index < len(stamps) and stamps[index] == stamp:
        return peers[index].value
    if 0 < index < len(stamps):
        before, after = peers[index - 1], peers[index]
        if max(stamp - before.stamp, after.stamp - stamp) <= 65 and after.stamp - before.stamp <= 80:
            offset = (stamp - before.stamp) / (after.stamp - before.stamp)
            return before.value + offset * (after.value - before.value)
    return None


def mark_buddy(rows, references, peers, buddy_station, first, last):
    peer_stamps = [row.stamp for row in peers]
    pairs = []
    matched = {}
    for row in rows:
        if row.base_keep and row.stamp not in matched:
            matched[row.stamp] = buddy_temperature(row.stamp, peers, peer_stamps)
    for row in references:
        peer_value = matched[row.stamp]
        if peer_value is not None:
            pairs.append((row.stamp, row.value - peer_value))
    stamps = [stamp for stamp, _ in pairs]
    evaluated = 0
    for row in rows:
        peer = matched.get(row.stamp)
        if not row.base_keep or peer is None:
            continue
        half_window = 360
        start, stop = bisect_left(stamps, row.stamp - half_window), bisect_right(stamps, row.stamp + half_window)
        before, after = bisect_left(stamps, row.stamp), bisect_right(stamps, row.stamp)
        if before - start < 2 or stop - after < 2 or before - start + stop - after < 6:
            continue
        residuals = [pairs[index][1] for index in range(start, before)]
        residuals.extend(pairs[index][1] for index in range(after, stop))
        offset = median(residuals)
        mad = median(abs(item - offset) for item in residuals)
        threshold = max(4, 4 * 1.4826 * mad)
        error = abs(row.value - peer - offset)
        evaluated += first <= row.stamp <= last
        if error > threshold:
            row.note("buddy", f"Сверка с {buddy_station}: отклонение {error:.2f} °C после местной поправки {offset:+.2f} °C при пороге {threshold:.2f} °C.", reject=True)
    return evaluated


def day_metrics(rows, param, station, official):
    samples, slots = [], set()
    counts = Counter()
    for row in rows:
        accepted = row.official_keep if official else row.keep
        if accepted:
            samples.append(row.value)
            slots.add(row.stamp // station.slot_minutes)
        elif official:
            counts[row.official_status] += 1
        elif any(code == "conflict" for code, _ in row.notes):
            counts["conflict"] += 1
        elif row.quality == "missing":
            counts["missing"] += 1
        else:
            counts["flagged"] += 1

    n, occupied = len(samples), len(slots)
    valid = occupied >= station.minimum_slots
    units = "разных сроков" if station.slot_minutes == 1 else "разных часов"
    threshold = f"Порог покрытия — {station.minimum_slots} {units}."
    reason = (f"В исходнике нет записей за сутки. {threshold}" if not rows
              else f"Число {units} с пригодными измерениями: {occupied}. {threshold}")
    mean = mean_of(samples, param) if valid else None
    angular = param == DIRECTION
    if angular and valid and mean is None:
        reason += " Среднее направление неопределённо: результирующий вектор близок к нулю."
    return {
        "n": n, "used_n": n if valid else 0, "slots": occupied, "valid": valid,
        "mean": mean, "min": min(samples) if valid and not angular else None,
        "max": max(samples) if valid and not angular else None,
        "missing": counts["missing"], "flagged": counts["flagged"], "conflicts": counts["conflict"],
        "reason": reason,
    }


def month_metrics(days, partial, param):
    count, valid_days, longest_gap, passes = coverage([day["valid"] for day in days])
    valid = bool(passes and not partial)
    good = [day for day in days if day["valid"]]
    incomplete = count - valid_days
    reason = f"Неполных суток: {incomplete} из {count}. Самая длинная серия — {longest_gap}. Пороги — 10 за месяц и 4 подряд."
    if partial:
        reason = "Выбран фрагмент месяца, доступно покрытие выбранных суток. " + reason
    mean = mean_of((day["mean"] for day in good), param) if valid else None
    return {
        "n": sum(day["n"] for day in days), "used_n": sum(day["used_n"] for day in good) if valid else 0,
        "mean_days": sum(day["mean"] is not None for day in good) if valid else 0,
        "valid_days": valid_days, "incomplete_days": incomplete, "longest_gap": longest_gap, "valid": valid,
        "mean": mean,
        "min": min(day["min"] for day in good) if valid and param != DIRECTION else None,
        "max": max(day["max"] for day in good) if valid and param != DIRECTION else None,
        "missing": sum(day["missing"] for day in days), "flagged": sum(day["flagged"] for day in days),
        "conflicts": sum(day["conflicts"] for day in days), "reason": reason,
    }


def summaries(rows, start, end, station, param):
    daily = []
    groups = groupby(rows, lambda row: row.time[:10])
    group_day, group = next(groups, (None, ()))
    for index in range(start.toordinal(), end.toordinal() + 1):
        day = date.fromordinal(index).isoformat()
        members = []
        if day == group_day:
            members = list(group)
            group_day, group = next(groups, (None, ()))
        daily.append({
            "day": day, "period": day, "raw_n": len(members),
            "working": day_metrics(members, param, station, False),
            "official": day_metrics(members, param, station, True),
        })
    monthly = []
    for month, group in groupby(daily, lambda day: day["day"][:7]):
        days = list(group)
        year, month_number = map(int, month.split("-"))
        partial = len(days) != calendar.monthrange(year, month_number)[1]
        monthly.append({
            "month": month, "period": month, "start": days[0]["day"], "end": days[-1]["day"],
            "days": len(days), "raw_n": sum(day["raw_n"] for day in days), "partial": partial,
            "working": month_metrics([day["working"] for day in days], partial, param),
            "official": month_metrics([day["official"] for day in days], partial, param),
        })
    return {"daily": daily, "monthly": monthly}


def options(query):
    station, param = query.get("station", "kr1"), query.get("param", "t_air")
    if not isinstance(station, str) or station not in STATIONS:
        raise ValueError("Неизвестная станция")
    if param not in STATIONS[station].params:
        raise ValueError("Станция не измеряет выбранный параметр")
    raw_filters = query.get("filters", "")
    if not isinstance(raw_filters, str):
        raise ValueError("Укажите фильтры строкой через запятую")
    requested = {item.strip() for item in raw_filters.split(",") if item.strip()}
    if unknown := requested - FILTERS.keys():
        raise ValueError(f"Неизвестные фильтры: {', '.join(sorted(unknown))}")
    filters = [name for name in FILTERS if name in requested]
    pressure = str(query.get("pressure_max", "1050"))
    if pressure not in {"1050", "1550"}:
        raise ValueError("Верхний порог давления должен быть 1050 или 1550 гПа")
    policy = query.get("conflict_policy", "all")
    if policy not in ("all", "best"):
        raise ValueError("Выберите исключение конфликтных сроков или разбор повторов")
    return station, param, filters, int(pressure), policy


def review(query, records=None):
    """Roll up a reversible selection without reusing or mutating official aggregates."""
    station, param, filters, pressure, policy = options(query)
    config = STATIONS[station]
    range_upper = pressure if param == "pressure_hpa" else LIMITS[param][1]
    context = CONTEXT_MINUTES if set(filters) - {"conflicts"} or ("conflicts" in filters and policy == "best") else 0
    fields = {}
    source_kind = "archive" if records is None else "upload"
    source_start = source_end = None
    if records is None:
        start = checked_date(query["start"]) if "start" in query else date(config.years.start, 1, 1)
        end = checked_date(query["end"]) if "end" in query else date(config.years.stop - 1, 12, 31)
        if start.year not in config.years or end.year not in config.years:
            raise ValueError(f"Архив станции охватывает {config.years.start}–{config.years.stop - 1} годы")
    else:
        source = query.get("source", "Загруженный файл")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Укажите имя загруженного файла")
        uploaded = list(uploaded_observations(records, source, param))
        for row in uploaded:
            fields.update(dict.fromkeys(row.original))
        source_start = date_at_minute(min(row.stamp for row in uploaded))
        source_end = date_at_minute(max(row.stamp for row in uploaded))
        start = checked_date(query["start"]) if "start" in query else source_start
        end = checked_date(query["end"]) if "end" in query else source_end
    if start > end:
        raise ValueError("Дата начала позже даты окончания")
    first, last = minute_bounds(start, end)
    if records is None:
        rows = list(archive_observations(station, param, first - context, last + context, fields, (start.year, end.year)))
    else:
        rows = [row for row in uploaded if first - context <= row.stamp <= last + context]
    rows.sort(key=lambda row: row.stamp)
    references, conflicts = prepare(rows, param, range_upper, use_references=bool(context))
    stamps = [row.stamp for row in references]
    resolve_conflicts(conflicts, references, stamps, param, station, policy if "conflicts" in filters else "off")
    if "hampel" in filters:
        mark_hampel(rows, references, stamps, param, station)
    if "spike" in filters:
        mark_spikes(rows, references, stamps, param, station)
    if "stuck" in filters:
        mark_stuck(rows, param, station)

    warnings = []
    if "buddy" in filters:
        if param != "t_air" or station not in {"kr1", "kr2"}:
            warnings.append("Сверка с соседней станцией доступна для температуры воздуха Кр1 и Кр2.")
        elif records is not None:
            warnings.append("Для загруженного файла нет подтверждённой парной станции. Сверка с соседней станцией пропущена.")
        else:
            peer_id = "kr2" if station == "kr1" else "kr1"
            peer_rows = list(archive_observations(peer_id, param, first - context, last + context, {}))
            peer_rows.sort(key=lambda row: row.stamp)
            peer_refs, _ = prepare(peer_rows, param, range_upper)
            peer_label = {"kr1": "Кр1", "kr2": "Кр2"}[peer_id]
            if not mark_buddy(rows, references, peer_refs, peer_label, first, last):
                warnings.append("В выбранном периоде недостаточно сопоставимых соседних сроков для сверки двух станций.")
    if param == "pressure_hpa" and pressure != LIMITS[param][1]:
        warnings.append("В рабочем отборе действует экспериментальный порог 1550 гПа. Такие значения могут быть приборной ошибкой. В сводке кейса остаётся 1050 гПа.")
    if param not in fields and fields:
        warnings.append("В исходных полях выбранного периода нет этого показателя.")

    selected = [row for row in rows if first <= row.stamp <= last] if context else rows
    counts = {
        "raw_n": len(selected), "kept": sum(row.keep for row in selected),
        "official_kept": sum(row.official_keep for row in selected),
        "restored": sum(row.keep and not row.official_keep for row in selected),
        "excluded": sum(row.official_keep and not row.keep for row in selected),
    }
    counts["rejected"] = counts["raw_n"] - counts["kept"]
    counts["official_rejected"] = counts["raw_n"] - counts["official_kept"]
    if not selected:
        warnings.append("За выбранные даты в источнике нет записей наблюдений.")
    summary_rows = summaries(selected, start, end, config, param)
    label, unit = PARAMS[param]
    return {
        "station": station, "param": param, "label": label, "unit": unit,
        "start": start.isoformat(), "end": end.isoformat(), "minimum_slots": config.minimum_slots,
        "source_kind": source_kind, "source_start": source_start.isoformat() if source_start else None,
        "source_end": source_end.isoformat() if source_end else None,
        "fields": list(fields), "rows": [row.result() for row in selected],
        "summaries": summary_rows, "counts": counts, "warnings": warnings,
        "filters": filters, "settings": {"pressure_max": pressure, "conflict_policy": policy},
        "filters_method": dict(FILTERS),
    }
