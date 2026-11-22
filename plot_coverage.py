#!/usr/bin/env python3
"""Апрельское покрытие Кр1. Для отрисовки используется ReportLab."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib.colors import HexColor, white

ROOT = Path(__file__).resolve().parent
STATION = HexColor("#A9BFD1")
PRESSURE = HexColor("#126D66")
THRESHOLD = HexColor("#9B4119")
INK = HexColor("#182630")
MUTED = HexColor("#405566")
GRID = HexColor("#DCE4EA")
MIN_HOURS = 18


def pressure_days(db):
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            """SELECT day, station_slots, slots, n
               FROM daily
               WHERE station = 'kr1' AND param = 'pressure_hpa'
                 AND day >= '2024-04-01' AND day < '2024-05-01'
               ORDER BY day"""
        ).fetchall()
    if [row[0] for row in rows] != [f"2024-04-{day:02}" for day in range(1, 31)]:
        raise ValueError("В базе должны быть все 30 дней апреля 2024 года для давления Кр1")
    return rows


def text(drawing, x, y, value, size=13, color=INK, bold=False, anchor="start"):
    font = "Helvetica-Bold" if bold else "Helvetica"
    drawing.add(String(x, y, value, fontName=font, fontSize=size, fillColor=color, textAnchor=anchor))


def swatch(drawing, x, y, color, label):
    drawing.add(Rect(x, y - 2, 14, 14, strokeColor=None, fillColor=color))
    text(drawing, x + 22, y, label)


def chart(rows):
    width, height = 1060, 487
    drawing = Drawing(width, height)
    drawing.add(Rect(0, 0, width, height, strokeColor=None, fillColor=white))
    text(drawing, 38, 443, "Покрытие Кр1 в апреле 2024 года", 27, bold=True)

    swatch(drawing, 39, 401, STATION, "Часы с записями станции")
    swatch(drawing, 271, 401, PRESSURE, "Часы с пригодным значением давления")
    drawing.add(Line(599, 406, 630, 406, strokeColor=THRESHOLD,
                     strokeWidth=1.6, strokeDashArray=[6, 4]))
    text(drawing, 639, 401, f"Порог валидности суток · {MIN_HOURS} из 24", color=THRESHOLD)

    left, right, base, top = 74, 1021, 120, 360
    scale = (top - base) / 24
    step = (right - left) / len(rows)
    text(drawing, 38, 377, "Часы", 12, MUTED)
    for hours in (0, 6, 12, 18, 24):
        y = base + hours * scale
        drawing.add(Line(left, y, right, y, strokeColor=GRID, strokeWidth=1))
        text(drawing, left - 11, y - 4, str(hours), 12,
             THRESHOLD if hours == MIN_HOURS else MUTED, bold=hours == MIN_HOURS, anchor="end")

    for day, (_, station_hours, pressure_hours, _) in enumerate(rows, start=1):
        x = left + (day - 0.5) * step
        for bar_x, hours, color in ((x - 12, station_hours, STATION), (x + 1, pressure_hours, PRESSURE)):
            if hours:
                drawing.add(Rect(bar_x, base, 11, hours * scale, strokeColor=None, fillColor=color))
        text(drawing, x, base - 22, str(day), 11.5,
             PRESSURE if day == 29 else MUTED, bold=day == 29, anchor="middle")

    cutoff = base + MIN_HOURS * scale
    drawing.add(Line(left, cutoff, right, cutoff, strokeColor=THRESHOLD,
                     strokeWidth=1.6, strokeDashArray=[6, 4]))
    text(drawing, (left + right) / 2, 74, "День апреля", 12, MUTED, anchor="middle")

    pressure_hours = rows[28][2]
    measurements = rows[28][3]
    text(drawing, 39, 48, f"29 апреля · {measurements} измерение давления за {pressure_hours} часов", bold=True)
    text(drawing, 39, 26, "Источник — архив организаторов, kr1_2024.csv. Часы посчитаны по записанному времени.",
         size=12, color=MUTED)
    return drawing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "results/weather.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "results/audit/april-coverage.svg")
    args = parser.parse_args()
    drawing = chart(pressure_days(args.db))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    renderSVG.drawToFile(drawing, str(args.out), title="Покрытие Кр1 в апреле 2024 года")


if __name__ == "__main__":
    main()
