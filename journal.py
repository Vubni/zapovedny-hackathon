"""Показать выбранный метеоряд в терминале или сохранить таблицу и исходные сроки."""
import argparse
import json
from pathlib import Path

from analytics import analyze, export_csv, raw_day
from server import ready


def display(value):
    return "—" if value is None else f"{value:.2f}".replace(".", ",")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", default="kr1")
    parser.add_argument("--param", default="pressure_hpa")
    parser.add_argument("--start", help="первый день включительно")
    parser.add_argument("--end", help="последний день включительно")
    parser.add_argument("--grain", choices=["day","dekad","month"], default="month")
    parser.add_argument("--base-start", type=int)
    parser.add_argument("--base-end", type=int)
    parser.add_argument("--csv", type=Path, help="сохранить весь ряд в CSV")
    parser.add_argument("--raw-day", help="посмотреть исходные строки дня вместо агрегатов")
    parser.add_argument("--json", type=Path, help="сохранить показанную выборку или исходный день")
    args = parser.parse_args()
    ready()
    query = {k:v for k,v in vars(args).items() if v is not None}
    if not (args.start or args.end) and args.station in {"kr1", "kr2"}:
        query.update(start="2024-01-01", end="2024-12-31")
    try:
        if args.raw_day:
            if args.csv:
                parser.error("--csv используется для сводной таблицы. Исходный день сохраняется через --json.")
            report = raw_day(dict(query, day=args.raw_day))
            print(report["source"] or "Архив не содержит строк этого дня")
            print("Строка  Срок                  Значение  Статус и исходные флаги")
            for r in report["rows"]:
                print(f"{r['line']:>6}  {r['time']}  {display(r['value']):>12}  {r['status']}  {r['flags']}")
            print(f"Пригодных измерений {report['n']}. Покрытие {report['slots']} при пороге {report['threshold']}. "
                  f"Среднее {display(report['mean'])}.")
        else:
            report = analyze(query)
            print(f"{report['label']}, {report['unit']} · {report['station']} · {report['start']} — {report['end']}")
            print("Период                 Среднее       Мин.      Макс.    Сутки     Откл.   Лет*")
            for r in report["rows"]:
                days = f"{r['valid_days']}/{r['days']}"
                line = (f"{r['label']:<20} {display(r['mean']):>9} {display(r['min']):>10} {display(r['max']):>10}"
                        f" {days:>8} {display(r['anomaly']):>9} {r['years']:>6}" )
                print(line if r["valid"] else line + "  " + r["reason"])
            s = report["stats"]
            print(f"Пригодных суток {s['valid_days']}/{s['days']}, в итоговом среднем {s['used_days']} сут. "
                  f"за {s['years']} г. Среднее {display(s['mean'])} {report['unit']}.")
            if report["grain"] == "month":
                print(f"* Число лет для этого месяца в среднем за выбранный период {report['base_start']}–{report['base_end']}.")
            if s["trend"] is not None:
                print(f"Наклон по сопоставимым месяцам {display(s['trend'])} {s['trend_unit']}, месяцев {s['trend_n']}.")
            if args.csv:
                args.csv.write_bytes(export_csv(report))
        if args.json:
            args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Ошибка. {error}\n")


if __name__ == "__main__":
    main()
