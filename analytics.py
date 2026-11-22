"""Read-only analyses of the reserve's calendar aggregates and source observations."""
import calendar
import csv
import io
import sqlite3
import zipfile
from collections import defaultdict
from contextlib import closing
from datetime import date
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from statistics import fmean

from build_data import circular_angle, circular_sums, source_member

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "data" / "meteo_hack_dataset.zip"
DATABASE = ROOT / "results" / "weather.sqlite"

PARAMS = {
    "t_air": ("Температура воздуха", "°C"),
    "rh": ("Относительная влажность", "%"),
    "wind_speed": ("Скорость ветра", "м/с"),
    "wind_gust": ("Порывы ветра", "м/с"),
    "wind_dir": ("Направление ветра", "°"),
    "soil_05": ("Температура почвы · 5 см", "°C"),
    "soil_10": ("Температура почвы · 10 см", "°C"),
    "soil_20": ("Температура почвы · 20 см", "°C"),
    "pressure_hpa": ("Атмосферное давление", "гПа"),
}
COMMON = tuple(PARAMS)[:5]
STATIONS = {
    "ame60": ("АМЕ-60", 1992, 2021, COMMON + ("soil_05", "soil_10", "soil_20")),
    "kr1": ("Кр1 · поляна", 2022, 2025, COMMON + ("pressure_hpa",)),
    "kr2": ("Кр2 · поле", 2022, 2025, COMMON + ("pressure_hpa",)),
}
MONTH_NAMES = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь")


def metadata():
    return {"stations": [
        {"id": key, "label": label, "start": f"{first}-01-01", "end": f"{last}-12-31",
         "params": [{"id": p, "label": PARAMS[p][0], "unit": PARAMS[p][1]} for p in params]}
        for key, (label, first, last, params) in STATIONS.items()
    ]}


def database():
    connection = sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def selection(query):
    station = query.get("station", "kr1")
    if station not in STATIONS:
        raise ValueError("Неизвестная станция")
    param = query.get("param", "t_air")
    if param not in STATIONS[station][3]:
        raise ValueError("Станция не измеряет выбранный параметр")
    return station, param


def check_date(text, station):
    try:
        value = date.fromisoformat(text)
        if value.isoformat() != text:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from None
    _, first, last, _ = STATIONS[station]
    if not first <= value.year <= last:
        raise ValueError(f"Архив станции охватывает {first}–{last} годы")
    return value


def mean_of(values, param):
    numbers = [v for v in values if v is not None]
    if not numbers:
        return None
    if param != "wind_dir":
        return fmean(numbers)
    return circular_angle(*circular_sums(numbers), len(numbers))


def difference(value, reference, param):
    if value is None or reference is None:
        return None
    delta = value - reference
    return (delta + 180) % 360 - 180 if param == "wind_dir" else delta


def bounds(day, grain):
    if grain == "day":
        return day, day
    prefix = day[:8]
    month_end = calendar.monthrange(int(day[:4]), int(day[5:7]))[1]
    if grain == "month":
        return prefix + "01", prefix + str(month_end)
    block = min((int(day[8:]) - 1) // 10, 2)
    return prefix + ("01", "11", "21")[block], prefix + ("10", "20", str(month_end))[block]


def maximum_gap(days):
    return max((sum(1 for _ in seq) for valid, seq in groupby(days, lambda d: bool(d["valid"]))
                if not valid), default=0)


def seasonal_slope(months):
    """Least-squares slope after centering years and observations within each calendar month."""
    strata = defaultdict(list)
    for row in months:
        if row["valid"] and row["mean"] is not None:
            strata[row["month"][5:]].append((int(row["month"][:4]), row["mean"]))
    num = den = 0.0
    count = 0
    for observations in strata.values():
        if len(observations) < 3:
            continue
        center_x, center_y = map(fmean, zip(*observations))
        for x, y in observations:
            num += (x - center_x) * (y - center_y)
            den += (x - center_x) ** 2
        count += len(observations)
    return (num / den if den else None), count


def extremes(days, key, reducer):
    return reducer((d[key] for d in days if d[key] is not None), default=None)


def analyze(query):
    station, param = selection(query)
    _, first, last, _ = STATIONS[station]
    start = check_date(query.get("start", f"{first}-01-01"), station).isoformat()
    end = check_date(query.get("end", f"{last}-12-31"), station).isoformat()
    grain = query.get("grain", "month")
    if start > end or grain not in {"day", "dekad", "month"}:
        raise ValueError("Проверьте порядок дат и шаг таблицы")
    try:
        base_start, base_end = int(query.get("base_start", first)), int(query.get("base_end", last))
    except (TypeError, ValueError):
        raise ValueError("Введите целые годы для периода сравнения") from None
    if not first <= base_start <= base_end <= last:
        raise ValueError(f"Выберите годы сравнения в диапазоне {first}–{last}")

    with closing(database()) as con:
        days = con.execute("SELECT * FROM daily WHERE station=? AND param=? AND day BETWEEN ? AND ? ORDER BY day",
                           (station, param, start, end)).fetchall()
        month_rows = con.execute("SELECT * FROM monthly WHERE station=? AND param=? ORDER BY month",
                                 (station, param)).fetchall()
    months = {row["month"]: row for row in month_rows}
    references = defaultdict(list)
    for row in month_rows:
        if row["valid"] and row["mean"] is not None and base_start <= int(row["month"][:4]) <= base_end:
            references[row["month"][5:]].append(row["mean"])

    rows, admitted = [], []
    for (lo, hi), group in groupby(days, lambda d: bounds(d["day"], grain)):
        members = list(group)
        good = [d for d in members if d["valid"]]
        clipped_start, clipped_end = members[0]["day"], members[-1]["day"]
        whole = (clipped_start, clipped_end) == (lo, hi)
        gap = maximum_gap(members)
        month_key = lo[:7]
        mon = months[month_key]
        valid = bool(good) and whole and (grain != "month" or bool(mon["valid"]))
        count = len(members)
        if not whole:
            reason = "Выбран фрагмент календарного периода. Для расчёта откройте период целиком или выберите сутки."
        elif grain == "day":
            d = members[0]
            threshold, unit = (72, "сроков") if station == "ame60" else (18, "часов")
            reason = f"{d['slots']} {unit} с пригодным значением при пороге {threshold}. Покрытие станции — {d['station_slots']}."
        elif grain == "month":
            reason = (f"Неполных суток {count-len(good)} из {count}, самая длинная серия — {gap}. "
                      "Допустимо до 10 за месяц и до 4 подряд.")
        else:
            reason = f"В среднее входят {len(good)} из {count} суток декады."
        label = lo if grain == "day" else f"{MONTH_NAMES[int(lo[5:7])-1].capitalize()} {lo[:4]}"
        if grain == "dekad":
            label = f"{lo[8:]}–{hi[8:]} · {label}"
        reference = references[lo[5:7]] if grain == "month" else []
        baseline = mean_of(reference, param)
        mean = (mon["mean"] if grain == "month" else mean_of((d["mean"] for d in good), param)) if valid else None
        if (grain == "day" and param == "pressure_hpa" and valid
                and members[0]["min"] == members[0]["max"]):
            reason += " Все пригодные значения давления одинаковы. Стоит сверить исходные сроки."
        if valid:
            admitted.extend(good)
        rows.append({
            "period": month_key if grain == "month" else lo, "start": clipped_start, "end": clipped_end,
            "label": label, "mean": mean, "min": extremes(good,"min",min) if valid else None,
            "max": extremes(good,"max",max) if valid else None, "valid": valid,
            "valid_days": len(good), "days": count, "longest_gap": gap,
            "n": sum(d["n"] for d in members), "flagged": sum(d["flagged"] for d in members),
            "missing": sum(d["missing"] for d in members), "conflicts": sum(d["conflicts"] for d in members),
            "baseline": baseline, "years": len(reference), "anomaly": difference(mean, baseline, param),
            "reason": reason,
        })
    trend, trend_n = (None, 0) if param == "wind_dir" else seasonal_slope(
        r for r in month_rows if start <= r["month"]+"-01" and bounds(r["month"]+"-01","month")[1] <= end)
    unit = PARAMS[param][1]
    return {
        "station": station, "param": param, "start": start, "end": end, "grain": grain, "unit": unit,
        "label": PARAMS[param][0], "base_start": base_start, "base_end": base_end, "rows": rows,
        "stats": {
            "mean": mean_of((d["mean"] for d in admitted),param),
            "min": extremes(admitted,"min",min), "max": extremes(admitted,"max",max),
            "valid_days": sum(d["valid"] for d in days), "used_days": len(admitted), "days": len(days),
            "years": len({d["day"][:4] for d in admitted if d["mean"] is not None}),
            "valid_periods": sum(r["valid"] for r in rows), "periods": len(rows),
            "flagged": sum(d["flagged"] for d in days), "n": sum(d["n"] for d in admitted),
            "trend": trend, "trend_unit": f"{unit}/год", "trend_n": trend_n,
        },
    }


def archive_rows(station, year):
    with zipfile.ZipFile(ARCHIVE) as archive:
        try:
            member = archive.open(source_member(station, year))
        except KeyError:
            return
        with member, io.TextIOWrapper(member, encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            for row in reader:
                yield reader.line_num, row


def source_status(row, param):
    flags = row["qc_flag"].split(";")
    if "dup_conflict" in flags:
        return "Конфликт срока"
    if any(flag.partition(":")[0] == param for flag in flags):
        return "Флаг параметра"
    return "Пригодно" if row.get(param, "") != "" else "Нет значения"


def raw_day(query):
    station, param = selection(query)
    day = check_date(query.get("day", ""), station).isoformat()
    with closing(database()) as con:
        d = con.execute("SELECT * FROM daily WHERE station=? AND param=? AND day=?",
                        (station,param,day)).fetchone()
    rows = []
    for line, record in archive_rows(station, int(day[:4])):
        if record["datetime"][:10] > day:
            break
        if record["datetime"][:10] == day:
            value = record.get(param, "")
            rows.append({"time": record["datetime"], "value": float(value) if value else None,
                         "flags": record["qc_flag"], "status": source_status(record,param), "line": line})
    source = source_member(station, day[:4])
    if not rows:
        with zipfile.ZipFile(ARCHIVE) as archive:
            if source not in archive.namelist():
                source = None
    signals = []
    if param == "pressure_hpa" and d["valid"] and d["min"] == d["max"]:
        signals.append({"code":"constant_pressure", "n":d["n"], "value":d["min"],
                        "description":"Все пригодные значения давления за сутки одинаковы. Проверьте исходные сроки и соседнюю станцию."})
    return {"station": station, "param": param, "day": day, "source": source, "signals":signals,
            "rows": rows, "threshold": 72 if station == "ame60" else 18,
            "slots": d["slots"], "station_slots": d["station_slots"], "valid": bool(d["valid"]),
            "mean": d["mean"], "n": d["n"]}


@lru_cache(maxsize=1)
def comparison():
    year = 2025
    with closing(database()) as con:
        records = con.execute("""SELECT station,day,mean FROM daily
            WHERE station IN ('kr1','kr2') AND param='t_air' AND valid=1 AND day BETWEEN ? AND ?""",
                              (f"{year}-01-01", f"{year}-12-31")).fetchall()
    daily = {station: {d["day"]:d["mean"] for d in records if d["station"] == station}
             for station in ("kr1","kr2")}
    common = sorted(daily["kr1"].keys() & daily["kr2"].keys())
    monthly = []
    for month in range(1, 13):
        key = f"{year}-{month:02}"
        dates = [d for d in common if d[:7] == key]
        count = calendar.monthrange(year,month)[1]
        flags = [{"valid": f"{key}-{i:02}" in dates} for i in range(1,count+1)]
        valid = count - len(dates) <= 10 and maximum_gap(flags) < 5
        pair = {k:fmean(values[d] for d in dates) if valid else None for k,values in daily.items()}
        monthly.append({"month":key,"pairs":len(dates),**pair,"delta":difference(pair["kr1"],pair["kr2"],"t_air")})

    days_set = set(common)
    hourly = {}
    for station in daily:
        values = defaultdict(list)
        for _, r in archive_rows(station,year):
            stamp = r["datetime"]
            if stamp[:10] in days_set and source_status(r,"t_air") == "Пригодно":
                values[stamp[:13]].append(float(r["t_air"]))
        hourly[station] = {k:fmean(v) for k,v in values.items()}
    matches = sorted(hourly["kr1"].keys() & hourly["kr2"].keys())
    hours = []
    for hour in range(24):
        keys = [stamp for stamp in matches if int(stamp[11:]) == hour]
        pair = {k:fmean(series[t] for t in keys) if keys else None for k,series in hourly.items()}
        hours.append({"hour":hour, "pairs":len(keys), **pair,"delta":difference(pair["kr1"],pair["kr2"],"t_air")})
    return {"year":year,"months":monthly,"hours":hours}


CSV_COLUMNS = {
    "period":"Период", "start":"Начало", "end":"Конец", "valid":"Период принят",
    "mean":"Среднее", "min":"Минимум", "max":"Максимум", "valid_days":"Пригодных суток",
    "days":"Календарных суток", "longest_gap":"До скольких неполных суток подряд",
    "n":"Пригодных измерений до отбора суток", "flagged":"Флаги параметра",
    "missing":"Пропуски в записанных строках", "conflicts":"Конфликтных строк",
    "baseline":"Среднее за выбранный период", "years":"Лет в сравнении",
    "anomaly":"Отклонение", "reason":"Разбор покрытия",
}


def export_csv(result):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Станция","Параметр","Единица","Шаг","Сравнение с года","Сравнение по год", *CSV_COLUMNS.values()])
    prefix = [result[k] for k in ("station","param","unit","grain","base_start","base_end")]
    for row in result["rows"]:
        values = [row[k] for k in CSV_COLUMNS]
        writer.writerow(prefix + [("да" if v else "нет") if isinstance(v,bool) else
                                  format(v, ".12g").replace(".",",") if isinstance(v,float) else v for v in values])
    return ("\ufeff"+buf.getvalue()).encode("utf-8")
