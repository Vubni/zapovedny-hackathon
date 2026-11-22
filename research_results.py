#!/usr/bin/env python3
"""Сводка метеорологических результатов и проверки по независимым расчётам."""

import argparse
import csv
import json
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

import analytics
from server import ready
from verify_analysis import compare_fields, equal, independent_slope

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
AUDIT = RESULTS / "audit"


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def as_number(text):
    return float(text) if text else None


def compare_month(api_row, audit_row):
    compare_fields(api_row["period"], api_row, {
        "valid": audit_row["valid"] == "True",
        "days": int(audit_row["days"]),
        "valid_days": int(audit_row["valid_days"]),
        "longest_gap": int(audit_row["longest_gap"]),
        "mean": as_number(audit_row["mean"]),
        "min": as_number(audit_row["min"]),
        "max": as_number(audit_row["max"]),
    })


def pressure_april(audit_months):
    audit_days = read_csv(AUDIT / "control_days.csv")
    source_examples = read_csv(AUDIT / "source_examples.csv")
    result = {}
    for station in ("kr1", "kr2"):
        query = {"station": station, "param": "pressure_hpa"}
        api = analytics.analyze({**query, "grain": "month", "start": "2024-04-01", "end": "2024-04-30"})
        month, = api["rows"]
        compare_month(month, audit_months[station, "pressure_hpa", "2024-04"])
        days = [row for row in audit_days if (row["station"], row["param"]) == (station, "pressure_hpa")
                and row["day"].startswith("2024-04-")]
        equal(f"{station} pressure days", len(days), 30)
        equal(f"{station} pressure flags", month["flagged"], sum(int(row["flagged"]) for row in days))
        equal(f"{station} readings after value QC", month["n"], sum(int(row["n"]) for row in days))
        accepted_dates = [row["day"] for row in days if row["valid"] == "True"]
        equal(f"{station} valid pressure days", month["valid_days"], len(accepted_dates))

        first_day = analytics.raw_day({**query, "day": "2024-04-01"})
        eligible = [row for row in first_day["rows"] if row["status"] == "Пригодно"]
        independent = [row for row in source_examples if row["station"] == station
                       and row["datetime"].startswith("2024-04-01 ")
                       and row["pressure_hpa"] and "pressure_hpa:" not in row["qc_flag"]
                       and "dup_conflict" not in row["qc_flag"].split(";")]
        equal(f"{station} pressure data on April 1",
              [(row["time"], row["value"]) for row in eligible],
              [(row["datetime"], float(row["pressure_hpa"])) for row in independent])
        equal(f"{station} pressure hours on April 1", first_day["slots"],
              len({row["datetime"][:13] for row in independent}))
        distinct = {row["value"] for row in eligible}
        constant_signal = any(signal["code"] == "constant_pressure" for signal in first_day["signals"])
        equal(f"{station} constant pressure warning", constant_signal, len(distinct) == 1)
        result[station] = {
            "month": month["period"],
            "calendar_days": month["days"],
            "valid_days": month["valid_days"],
            "longest_gap_days": month["longest_gap"],
            "valid": month["valid"],
            "mean_hpa": month["mean"],
            "min_hpa": month["min"],
            "max_hpa": month["max"],
            "raw_readings": sum(int(row["raw_n"]) for row in days),
            "flagged_readings": month["flagged"],
            "eligible_readings": month["n"],
            "readings_on_valid_days": sum(int(row["n"]) for row in days if row["valid"] == "True"),
            "used_daily_means": api["stats"]["used_days"],
            "valid_dates": accepted_dates,
            "april_01": {
                "raw_readings": len(first_day["rows"]),
                "eligible_readings": len(eligible),
                "distinct_values": len(distinct),
                "hours_with_values": first_day["slots"],
                "mean_hpa": first_day["mean"],
                "min_hpa": min(distinct),
                "max_hpa": max(distinct),
                "constant_signal": constant_signal,
            },
        }
    return result


def paired_temperatures(control):
    api = analytics.comparison()
    equal("comparison year", api["year"], 2025)
    output = {"year": api["year"]}
    for group, key, length, count_key in (
        ("months", "month", 12, "common_days"), ("hours", "hour", 24, "common_hours"),
    ):
        rows = api[group]
        equal(f"{group} size", len(rows), length)
        equal(f"{group} keys", [row[key] for row in rows], [row[key] for row in control[group]])
        for actual, expected in zip(rows, control[group]):
            compare_fields(f"paired {group} {actual[key]}", actual,
                           {field: expected[field] for field in (key, "pairs", "kr1", "kr2", "delta")})
        output[count_key] = sum(row["pairs"] for row in rows)
        equal(count_key, output[count_key], control[count_key])
        output[group] = {
            "rows": rows,
            "kr1_warmer": sum(row["delta"] > 0 for row in rows),
            "kr1_cooler": sum(row["delta"] < 0 for row in rows),
            "equal": sum(row["delta"] == 0 for row in rows),
            "max_positive": max(rows, key=lambda row: row["delta"]),
            "max_negative": min(rows, key=lambda row: row["delta"]),
        }
    output["calendar_days"] = 365
    output["hours_on_common_days"] = 24 * output["common_days"]
    return output


def temperature_trends(audit_months):
    query = {"station": "ame60", "param": "t_air", "grain": "month", "start": "1992-01-01",
             "end": "2021-12-31"}
    api = analytics.analyze(query)
    equal("calendar months in 1992–2021", len(api["rows"]), 360)
    api_means = {row["period"]: row["mean"] for row in api["rows"]}
    for row in api["rows"]:
        compare_month(row, audit_months["ame60", "t_air", row["period"]])

    valid_by_year = Counter(row["period"][:4] for row in api["rows"] if row["valid"])
    full_years = {year for year, count in valid_by_year.items() if count == 12}
    rows = []
    for row in api["rows"]:
        period = row["period"]
        audited = audit_months["ame60", "t_air", period]
        rows.append({
            "month": period, "calendar_days": row["days"], "valid_days": row["valid_days"],
            "valid": row["valid"], "longest_gap_days": row["longest_gap"],
            "mean_c": audited["mean"],
            "full_calendar_year_sample": period[:4] in full_years,
        })

    results = {"query": query, "calendar_years": 30, "calendar_months": 360, "samples": {}}
    for name, sample in (
        ("all_valid_months", rows),
        ("full_calendar_years", [row for row in rows if row["full_calendar_year_sample"]]),
    ):
        slope, count = analytics.seasonal_slope(
            dict(month=row["month"], valid=row["valid"], mean=api_means[row["month"]]) for row in sample
        )
        strata = defaultdict(list)
        for row in sample:
            if row["valid"]:
                year, month = map(int, row["month"].split("-"))
                strata[month].append((year, float(row["mean_c"])))
        reference_slope, reference_count = independent_slope(strata)
        years = sorted({year for points in strata.values() for year, _ in points})
        equal(f"{name} independent months", count, reference_count)
        equal(f"{name} independent slope", slope, reference_slope)
        results["samples"][name] = {
            "included_years": years,
            "included_months": count,
            "months_by_calendar_month": {month: len(points) for month, points in sorted(strata.items())},
            "slope_c_per_year": slope,
            "slope_c_per_year_independent": reference_slope,
        }
    compare_fields("1992–2021 full selection", api["stats"], {
        "trend": results["samples"]["all_valid_months"]["slope_c_per_year"],
        "trend_n": results["samples"]["all_valid_months"]["included_months"],
        "years": len(valid_by_year),
    })
    return results, rows


def number(value, digits=3, signed=False):
    options = "+" if signed else ""
    return f"{value:{options}.{digits}f}".replace(".", ",").replace("-", "−")


def integer(value):
    return f"{value:,}".replace(",", "\u202f")


def write_report(path, findings):
    pressure = findings["pressure_2024_april"]
    left, right = pressure["kr1"], pressure["kr2"]
    left_day, right_day = left["april_01"], right["april_01"]
    temp = findings["paired_temperature_2025"]
    months, hours = temp["months"], temp["hours"]
    slope = findings["ame60_temperature_slope"]
    all_months = slope["samples"]["all_valid_months"]
    full_years = slope["samples"]["full_calendar_years"]
    all_slope, full_slope = all_months["slope_c_per_year"], full_years["slope_c_per_year"]

    report = [
        "# Метеорологические результаты по урочищу «Красное»",
        "",
        "## Давление на Кр1 и Кр2 в апреле 2024 года",
        "",
        f"На поляне Кр1 прошли {left['valid_days']} из {left['calendar_days']} суток. "
        f"С 2 по 29 апреля покрытие давления оставалось ниже порога {left['longest_gap_days']} суток подряд. "
        f"Флаг диапазона получили {integer(left['flagged_readings'])} из "
        f"{integer(left['raw_readings'])} исходных значений. Месяц исключён по обоим условиям покрытия. "
        "Допустимы до 10 неполных суток и до 4 подряд. "
        f"На поле Кр2 прошли все {right['valid_days']} из {right['calendar_days']} суток. "
        f"По {right['used_daily_means']} суточным средним давление составило "
        f"**{number(right['mean_hpa'], 2)} гПа**. В них вошли "
        f"{integer(right['readings_on_valid_days'])} исходных измерений. "
        "Сопоставление давлений за весь месяц ограничено покрытием Кр1.",
        "",
        f"**1 апреля** на Кр1 все {left_day['eligible_readings']} пригодных значений "
        f"из {left_day['raw_readings']} исходных равны {number(left_day['mean_hpa'], 2)} гПа. "
        f"Значение в диапазоне 940–1050 гПа, а покрытие {left_day['hours_with_values']} часов "
        f"проходит порог 18. На Кр2 за тот же день {right_day['distinct_values']} разных значений "
        f"из {right_day['eligible_readings']} пригодных, среднее {number(right_day['mean_hpa'])} гПа. "
        "Повтор на Кр1 требует сверки журнала наблюдений и прибора. Его причина по архиву не установлена.",
        "",
        "[График почасового покрытия](../audit/april-coverage.svg) · "
        "[Исходные сроки и флаги](../audit/source_examples.csv)",
        "",
        "## Температура на поляне и в поле в 2025 году",
        "",
        f"Поляна Кр1 холоднее поля Кр2 в **{months['kr1_cooler']} из {len(months['rows'])} месяцев**, "
        f"теплее в {months['kr1_warmer']}, совпадений средних {months['equal']}. "
        f"Всего {temp['common_days']} из {temp['calendar_days']} дат валидны по температуре "
        "на обеих станциях. Средние за месяц рассчитаны по одним и тем же датам с равным весом суток.",
        "",
        f"В среднем по одноимённым часам Кр1 холоднее в **{hours['kr1_cooler']} из {len(hours['rows'])} часов суток**, "
        f"теплее в {hours['kr1_warmer']}, совпадений средних {hours['equal']}. "
        f"Сопоставлены {integer(temp['common_hours'])} пар «дата и час» "
        f"из {integer(temp['hours_on_common_days'])} возможных на общих валидных датах. "
        "В каждом занятом часе исходные измерения усреднены отдельно для каждой точки. "
        "Время взято из архива без пересчёта часового пояса.",
        "",
        "| Наибольшие и наименьшие разности | Кр1, °C | Кр2, °C | Кр1 − Кр2, °C | Пар |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, row, unit in (
        ("Месяц", months["max_negative"], "суток"), ("Месяц", months["max_positive"], "суток"),
        ("Час", hours["max_negative"], "дата и час"), ("Час", hours["max_positive"], "дата и час"),
    ):
        period = row["month"] if "month" in row else f"{row['hour']:02}:00"
        report.append(
            f"| {label} {period} | {number(row['kr1'])} | {number(row['kr2'])} | "
            f"{number(row['delta'], signed=True)} | {row['pairs']} ({unit}) |"
        )
    report.extend([
        "",
        "Знак разности меняется по месяцам и часам. Результат описывает эти две точки и этот год. "
        "Расположение станции, прибор и другие условия наблюдений нельзя разделить по этому сравнению.",
        "",
        "[График парных разностей](microclimate-2025.svg) · "
        "[Месячные пары](temperature-2025-monthly.csv) · [Часовые пары](temperature-2025-hourly.csv)",
        "",
        "## Наклон температуры АМЕ-60 и чувствительность к покрытию",
        "",
        "| Выборка в интервале 1992–2021 | Лет с данными | Месяцев | Наклон, °C/год |",
        "|---|---:|---:|---:|",
        f"| Все валидные месяцы | {len(all_months['included_years'])} из {slope['calendar_years']} | "
        f"{all_months['included_months']} из {slope['calendar_months']} | {number(all_slope, signed=True)} |",
        f"| Годы с 12 валидными месяцами температуры | {len(full_years['included_years'])} | "
        f"{full_years['included_months']} | {number(full_slope, signed=True)} |",
        "",
        "Полные годы — " + ", ".join(map(str, full_years["included_years"])) + ". "
        "В обеих выборках применён один метод наименьших квадратов. Для каждого календарного месяца "
        "год и температура центрированы относительно средних внутри этого месяца. Наклон равен сумме "
        "произведений центрированных значений, делённой на сумму квадратов центрированных лет. "
        "В расчёт каждой календарной группы входят как минимум три года.",
        "",
        f"Отбор полных лет увеличивает оценку на **{number(full_slope - all_slope, signed=True)} °C/год**. "
        "Состав и временной охват меняются вместе. Обе оценки описывают отобранные наблюдения АМЕ-60. "
        "Статистическую значимость и причины изменения в этом расчёте не оценивали.",
        "",
        "[Месячные значения и принадлежность к выборке](ame60-temperature-monthly.csv)",
        "",
        "## Воспроизведение",
        "",
        "Из корня проекта выполните `python3 research_results.py`. Скрипт проверяет хеш архива, "
        "сверяет 12 месячных и 24 часовых строки сравнения с отдельным расчётом из ZIP и повторяет "
        "оба наклона попарной формулой по независимому ряду месячных температур.",
        "При отсутствии базы команда собирает её из архива. "
        "С установленным ReportLab график парных разностей воспроизводится командой `python3 plot_microclimate.py`.",
        "",
        f"SHA-256 исходного ZIP — `{findings['archive_sha256']}`.",
        "",
        "[Точные результаты](findings.json) · [Независимая проверка сравнения](../analysis-checks.json) · "
        "[Пересчёт покрытия из архива](../audit/ОТЧЁТ.md)",
    ])
    path.write_text("\n".join(report) + "\n", encoding="utf-8")


def write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_pairs(path, rows, key, pairs_field):
    values = [
        {key: row[key], pairs_field: row["pairs"], "kr1_c": row["kr1"],
         "kr2_c": row["kr2"], "difference_kr1_minus_kr2_c": row["delta"]}
        for row in rows
    ]
    write_csv(path, values, values[0].keys())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS / "research")
    args = parser.parse_args()
    digest = sha256(analytics.ARCHIVE.read_bytes()).hexdigest()
    audit = json.loads((AUDIT / "audit.json").read_text(encoding="utf-8"))
    checks = json.loads((RESULTS / "analysis-checks.json").read_text(encoding="utf-8"))
    equal("archive hash from independent audit", digest, audit["archive_sha256"])
    equal("comparison source name", analytics.ARCHIVE.name, checks["archive"])
    ready()
    build = json.loads((RESULTS / "build.json").read_text(encoding="utf-8"))
    equal("archive hash from build", digest, build["source_sha256"])

    audit_months = {(row["station"], row["param"], row["month"]): row
                    for row in read_csv(AUDIT / "parameter_months.csv")}
    pressure = pressure_april(audit_months)
    comparison = paired_temperatures(checks["comparison_2025"])
    trends, trend_rows = temperature_trends(audit_months)
    findings = {
        "archive": analytics.ARCHIVE.name, "archive_sha256": digest,
        "pressure_2024_april": pressure, "paired_temperature_2025": comparison,
        "ame60_temperature_slope": trends,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    write_report(args.out / "ВЫВОДЫ.md", findings)
    (args.out / "findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    write_pairs(args.out / "temperature-2025-monthly.csv", comparison["months"]["rows"], "month", "paired_days")
    write_pairs(args.out / "temperature-2025-hourly.csv", comparison["hours"]["rows"], "hour", "paired_date_hours")
    write_csv(args.out / "ame60-temperature-monthly.csv", trend_rows, trend_rows[0].keys())
    print("Сверены 2 месяца и исходные сутки давления, 12 месячных и 24 часовых строки сравнения,")
    print("360 месячных строк АМЕ-60 и оба наклона по независимой формуле.")


if __name__ == "__main__":
    main()
