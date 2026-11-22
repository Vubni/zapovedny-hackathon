#!/usr/bin/env python3
"""График парной разности температуры Кр1 и Кр2. Требует ReportLab."""

import argparse
import csv
from math import isfinite
from pathlib import Path

from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Drawing, Line, Rect
from reportlab.lib.colors import HexColor, white

from plot_coverage import GRID, INK, MUTED, swatch, text
from research_results import integer, number

RESEARCH = Path(__file__).resolve().parent / "results" / "research"
TITLE = "Температура на поляне и в поле в 2025 году"
COOLER = HexColor("#275979")
WARMER = HexColor("#A5502B")
MONTH_LABELS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def read_pairs(path, period_field, count_field, expected_periods):
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    if [row[period_field] for row in rows] != expected_periods:
        raise ValueError(f"Неполная календарная шкала в {path}")
    differences = [float(row["difference_kr1_minus_kr2_c"]) for row in rows]
    if any(not isfinite(value) or abs(value) > 1 for value in differences):
        raise ValueError(f"Значение за пределами шкалы ±1 °C в {path}")
    return differences, sum(int(row[count_field]) for row in rows)


def panel(drawing, bottom, heading, caption, labels, differences):
    left, right, height = 77, 1062, 158
    zero = bottom + height / 2
    scale = height / 2
    text(drawing, left, bottom + height + 24, heading, 17, bold=True)
    text(drawing, right, bottom + height + 24, caption, 13, color=MUTED, anchor="end")

    for tick in (-1, -0.5, 0, 0.5, 1):
        y = zero + scale * tick
        color = MUTED if tick == 0 else GRID
        drawing.add(Line(left, y, right, y, strokeColor=color, strokeWidth=1))
        label = "0" if tick == 0 else number(tick, 1, signed=True)
        text(drawing, left - 10, y - 4, label, 11.5, color=MUTED, anchor="end")

    step = (right - left) / len(labels)
    bar_width = min(43, step * 0.55)
    lowest, highest = min(differences), max(differences)
    for index, (label, delta) in enumerate(zip(labels, differences)):
        x = left + (index + 0.5) * step
        tip = zero + scale * delta
        color = WARMER if delta > 0 else COOLER
        extreme = delta in (lowest, highest)
        if delta:
            drawing.add(Rect(x - bar_width / 2, min(zero, tip), bar_width, abs(tip - zero),
                             strokeColor=None, fillColor=color))
        text(drawing, x, bottom - 19, label, 12, color if extreme else MUTED, extreme, anchor="middle")
        if extreme:
            text(drawing, x, tip + (16 if delta > 0 else -14), number(delta, signed=True),
                 11.5, color, bold=True, anchor="middle")


def figure(months, days, hours, hour_pairs):
    drawing = Drawing(1100, 630)
    drawing.add(Rect(0, 0, drawing.width, drawing.height, strokeColor=None, fillColor=white))
    text(drawing, 38, 586, TITLE, 27, color=INK, bold=True)
    text(drawing, 38, 553, "Парная разность · Кр1 (поляна) − Кр2 (поле), °C", 14, bold=True)
    swatch(drawing, 445, 553, WARMER, "Кр1 теплее")
    swatch(drawing, 571, 553, COOLER, "Кр1 холоднее")

    panel(drawing, 325, "По месяцам", f"{integer(days)} общая дата", MONTH_LABELS, months)
    panel(drawing, 72, "По часам суток", f"{integer(hour_pairs)} пар «дата и час»",
          [f"{hour:02}" for hour in range(24)], hours)
    text(drawing, 38, 20, "Источник — архив организаторов, kr1_2025.csv и kr2_2025.csv. "
         "Часы соответствуют времени в исходных записях.", 12, color=MUTED)
    return drawing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", type=Path, default=RESEARCH, help="Каталог CSV парного сравнения")
    parser.add_argument("--out", type=Path, default=RESEARCH / "microclimate-2025.svg")
    args = parser.parse_args()
    months, days = read_pairs(args.tables / "temperature-2025-monthly.csv", "month", "paired_days",
                              [f"2025-{month:02}" for month in range(1, 13)])
    hours, hour_pairs = read_pairs(args.tables / "temperature-2025-hourly.csv", "hour", "paired_date_hours",
                                   [str(hour) for hour in range(24)])
    svg = renderSVG.drawToString(figure(months, days, hours, hour_pairs), svgAttrs={"role": "img"})
    svg = svg.replace("<title>...</title>", f"<title>{TITLE}</title>")
    svg = svg.replace("<desc>...</desc>", "<desc>Разность Кр1 минус Кр2 по месяцам и по часам суток, "
                      "в градусах Цельсия.</desc>")
    svg = svg.replace("font-family: Helvetica-Bold;", "font-family: Arial, sans-serif; font-weight: 700;")
    svg = svg.replace("font-family: Helvetica;", "font-family: Arial, sans-serif;")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
