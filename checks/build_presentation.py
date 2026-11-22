#!/usr/bin/env python3
"""Собрать PDF для защиты. Требуются ReportLab и системные шрифты с кириллицей."""
import argparse
import csv
import io
from pathlib import Path

from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
W, H = 960, 540
PAPER = "#F4F3EA"
WHITE = "#FFFEF8"
INK = "#1C3027"
FOREST = "#13382D"
GREEN = "#27684F"
MUTED = "#4E6658"
LINE = "#D1D8C8"
PALE = "#E8EDE1"
LIME = "#D8E5B9"
AMBER = "#815211"
SAND = "#F2E6CB"
SCREENSHOTS = ROOT / "results/browser"
REPO = "https://github.com/Vubni/zapovedny-hackathon"


def load_fonts():
    families = (
        (Path("/System/Library/Fonts/Supplemental"), "Arial.ttf", "Arial Bold.ttf", "Georgia.ttf"),
        (Path("/usr/share/fonts/truetype/dejavu"), "DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf"),
        (Path("C:/Windows/Fonts"), "arial.ttf", "arialbd.ttf", "georgia.ttf"),
    )
    for directory, *files in families:
        if all((directory / file).is_file() for file in files):
            for role, file in zip(("Body", "Bold", "Title"), files):
                pdfmetrics.registerFont(TTFont(role, str(directory / file)))
            return
    raise SystemExit("Нужны Arial и Georgia либо DejaVu Sans и DejaVu Serif.")


class Deck:
    def __init__(self, path):
        self.pdf = canvas.Canvas(str(path), pagesize=(W, H), pageCompression=1, invariant=1)
        self.pdf.setTitle("Метеожурнал урочища «Красное»")
        self.pdf.setAuthor("exeCUtor")
        self.pdf.setSubject("Заповедный хакатон · Кейс 02 · Обработка метеоданных")
        self.dark = False

    def rect(self, x, t, width, height, fill, stroke=None, radius=0):
        self.pdf.setFillColor(HexColor(fill))
        if stroke:
            self.pdf.setStrokeColor(HexColor(stroke))
            self.pdf.setLineWidth(1)
        if radius:
            self.pdf.roundRect(x, H - t - height, width, height, radius, stroke=bool(stroke), fill=1)
        else:
            self.pdf.rect(x, H - t - height, width, height, stroke=bool(stroke), fill=1)

    def line(self, x, t, x2, t2, color=LINE, weight=1):
        self.pdf.setStrokeColor(HexColor(color))
        self.pdf.setLineWidth(weight)
        self.pdf.line(x, H - t, x2, H - t2)

    def text(self, x, t, value, size=16, font="Body", color=INK, *, width=None, align="left"):
        if width is not None and pdfmetrics.stringWidth(value, font, size) > width:
            raise ValueError(f"Строка не помещается в {width} пт: {value!r}")
        self.pdf.setFont(font, size)
        self.pdf.setFillColor(HexColor(color))
        draw = self.pdf.drawRightString if align == "right" else self.pdf.drawString
        draw(x, H - t, value)

    def lines(self, x, t, values, size=16, leading=23, font="Body", color=INK, *, width=None):
        for index, value in enumerate(values):
            self.text(x, t + index * leading, value, size, font, color, width=width)

    def picture(self, name, crop, x, t, width):
        with Image.open(SCREENSHOTS / name) as image:
            clipped = image.crop(crop).convert("RGB")
        height = width * clipped.height / clipped.width
        self.rect(x - 1, t - 1, width + 2, height + 2, WHITE, LINE, radius=3)
        with io.BytesIO() as data:
            clipped.save(data, format="JPEG", quality=94, subsampling=0)
            data.seek(0)
            self.pdf.drawImage(ImageReader(data), x, H - t - height, width, height)
        return height

    def link(self, x, t, label, url, size=13, color=GREEN, font="Bold"):
        self.text(x, t, label, size, font, color)
        width = pdfmetrics.stringWidth(label, font, size)
        self.pdf.linkURL(url, (x, H - t - 4, x + width, H - t + size), relative=0)

    def page(self, number, section, source=None, dark=False):
        self.dark = dark
        self.rect(0, 0, W, H, FOREST if dark else PAPER)
        pale = LIME if dark else MUTED
        divider = "#476857" if dark else LINE
        self.text(48, 33, section, 11, "Bold", pale)
        self.text(912, 33, "exeCUtor   /   КЕЙС 02", 11, "Bold", pale, align="right")
        self.line(48, 49, 912, 49, divider)
        self.line(48, 496, 912, 496, divider)
        if source:
            self.text(48, 518, source, 10, color=pale, width=790)
        self.text(912, 518, f"{number:02d} / 07", 10, "Bold", pale, align="right")

    def heading(self, value):
        self.text(48, 111, value, 36, "Title", WHITE if self.dark else INK, width=864)

    def finish(self):
        self.pdf.showPage()


def cover(deck):
    deck.page(1, "ЗАПОВЕДНЫЙ ХАКАТОН", "Центрально-Лесной заповедник · Архив наблюдений урочища «Красное»", dark=True)
    deck.lines(48, 125, ("Метеожурнал", "урочища", "«Красное»"), 51, 58, "Title", WHITE, width=410)
    deck.lines(50, 304, (
        "Из файла наблюдений в сводку,",
        "где видны пропуски и исходные строки.",
    ), 17, 25, color=LIME, width=395)
    deck.line(49, 362, 408, 362, "#476857")
    deck.lines(50, 391, (
        "Вопрос для показа",
        "Можно ли включить давление Кр1",
        "в апрельскую сводку 2024 года?",
    ), 16, 24, color=WHITE, width=360)
    deck.picture("year-1280.png", (31, 88, 1249, 762), 454, 119, 458)
    deck.text(454, 396, "Рабочий интерфейс · Кр1, давление за 2024 год", 12, color=LIME)
    deck.finish()


def april(deck):
    deck.page(2, "КАЧЕСТВО РАСЧЁТА", "Источники · kr1_2024.csv, kr2_2024.csv · results/audit/ОТЧЁТ.md и results/research/findings.json")
    deck.heading("Апрель исключён из сводки давления Кр1")
    deck.text(50, 149, "По давлению за год принято 11 из 12 месяцев и 334 из 366 суток.", 18, color=MUTED)
    deck.text(50, 203, "Пригодные сутки в апреле, из 30", 15, "Bold")
    for y, name, days, color in (
        (245, "Кр1 · давление", 2, AMBER),
        (306, "Кр1 · температура", 28, GREEN),
        (367, "Кр2 · давление", 30, GREEN),
    ):
        deck.text(50, y, name, 16, "Bold")
        deck.rect(225, y - 18, 320, 22, PALE, radius=4)
        deck.rect(225, y - 18, 320 * days / 30, 22, color, radius=4)
        deck.text(561, y, str(days), 19, "Bold", color)
    deck.text(50, 422, "Среднее давление Кр2 за апрель составляет 980,16 гПа.", 14, color=MUTED)
    deck.rect(624, 183, 288, 257, WHITE, LINE, radius=10)
    deck.text(647, 220, "Почему апрель исключён", 17, "Bold")
    deck.text(647, 284, "28", 60, "Title", AMBER)
    deck.lines(648, 313, ("неполных суток подряд", "при допустимых четырёх"), 17, 24, color=INK, width=243)
    deck.line(647, 355, 888, 355)
    deck.lines(648, 380, ("За весь месяц допускается", "не более 10 неполных суток."), 13, 19, color=MUTED, width=243)
    deck.finish()


def raw_day(deck):
    deck.page(3, "ПРОВЕРКА И ПОЛЬЗА", "Источники · results/examples/kr1-pressure-april-29-incomplete.json · results/audit/ОТЧЁТ.md")
    deck.heading("29 апреля видно, почему среднего нет")
    deck.picture("raw-1280.png", (229, 62, 1050, 573), 455, 145, 457)
    deck.text(49, 204, "15 из 24", 57, "Title", AMBER)
    deck.lines(50, 234, ("часов с пригодным давлением", "при пороге 18"), 17, 24, "Bold", width=380)
    deck.lines(50, 297, (
        "31 пригодное измерение распределено",
        "по 15 часам. Сутки исключены.",
    ), 16, 23, color=MUTED, width=375)
    deck.text(50, 365, "09:03 · 1002,27 гПа · строка 5644", 16, "Bold", width=360)
    deck.text(50, 391, "kr1_2024.csv · сверена 31 исходная строка", 13, color=MUTED, width=365)
    deck.line(455, 442, 912, 442)
    deck.lines(455, 463, (
        "1 апреля порог пройден (19 часов). Все 38 значений равны",
        "1005,45 гПа. Журнал просит проверить день вручную.",
    ), 12, 16, color=MUTED, width=457)
    deck.finish()


def upload(deck):
    deck.page(4, "РАБОТОСПОСОБНОСТЬ ПРОТОТИПА", "Источники · results/browser/import-1280.png · results/import-example/ · results/import-http-checks.json")
    deck.heading("Новый JSON проходит тот же контроль")
    deck.text(50, 147, "Откройте файл станции и задайте станцию, год и показатель.", 18, color=MUTED)
    deck.picture("import-1280.png", (0, 77, 1280, 680), 48, 179, 628)
    deck.rect(701, 178, 211, 290, WHITE, LINE, radius=10)
    deck.text(722, 215, "Кр1 · 1 апреля 2024", 15, "Bold", width=170)
    for y, value, label in (
        (258, "48", "исходных строк JSON"),
        (314, "38", "значений в 19 часах"),
        (370, "1005,45 гПа", "среднее за сутки"),
    ):
        deck.text(722, y, value, 25, "Title", width=170)
        deck.text(722, y + 19, label, 13, color=MUTED, width=170)
    deck.link(722, 437, "Открыть импорт →", "http://127.0.0.1:8765/import", 13)
    deck.finish()


def method(deck):
    deck.page(5, "ТЕХНИЧЕСКАЯ ОБОСНОВАННОСТЬ", "Источники · docs/case-2-weather.pdf · docs/МЕТОДИКА.md · results/audit/ОТЧЁТ.md")
    deck.heading("Покрытие определяет допуск к расчёту")
    deck.text(50, 147, "Правила кейса применяются к каждому показателю и каждой точке.", 18, color=MUTED)
    rows = (
        ("01", "Исходный срок", "Время хранится до минуты. Конфликтный срок исключается целиком."),
        ("02", "Пригодное значение", "Флаг исключает значение своего параметра. Пропуски сохраняются."),
        ("03", "Сутки", "Нужно 72 разных срока у АМЕ-60 или 18 разных часов у Сокол М1."),
        ("04", "Месяц", "Допустимо до 10 неполных суток, из них подряд до четырёх."),
    )
    for index, (number, phase, note) in enumerate(rows):
        y = 195 + index * 50
        deck.line(48, y + 20, 912, y + 20)
        deck.text(50, y, number, 13, "Bold", GREEN)
        deck.text(95, y, phase, 16, "Bold", width=210)
        deck.text(316, y, note, 15, width=594)
    deck.rect(48, 382, 864, 85, PALE, radius=8)
    deck.text(67, 411, "Среднее суток считают по всем пригодным значениям. Среднее месяца даёт суткам равный вес.", 15, "Bold", width=827)
    deck.text(67, 442, "Python готовит сводки SQLite, HTTP API отдаёт их в браузер, исходные строки берутся из ZIP.", 15, color=MUTED, width=827)
    deck.finish()


def comparison(deck):
    deck.page(6, "РЕЗУЛЬТАТЫ НА АРХИВЕ", "Источники · results/research/ВЫВОДЫ.md · temperature-2025-monthly.csv · ame60-temperature-monthly.csv")
    deck.heading("Температура на двух точках и в длинном ряду")
    deck.line(514, 160, 514, 460)
    deck.text(50, 173, "Поляна и поле · 2025", 20, "Bold")
    deck.text(50, 201, "Кр1 холоднее Кр2 в 8 из 12 месяцев.", 16, "Bold", width=450)
    deck.text(50, 234, "Разность средних, Кр1 минус Кр2, °C", 13, color=MUTED)
    with (ROOT / "results/research/temperature-2025-monthly.csv").open(encoding="utf-8", newline="") as file:
        monthly = [float(row["difference_kr1_minus_kr2_c"]) for row in csv.DictReader(file)]
    base = 287
    for value, label in ((0.2, "+0,2"), (0, "0"), (-0.2, "−0,2"), (-0.4, "−0,4"), (-0.6, "−0,6")):
        y = base - value * 124
        deck.line(88, y, 477, y, MUTED if value == 0 else LINE)
        deck.text(79, y + 4, label, 10, color=MUTED, align="right")
    for index, value in enumerate(monthly):
        bar = abs(value) * 124
        deck.rect(94 + index * 32, base - bar if value > 0 else base, 19, bar, GREEN if value > 0 else "#9F7130", radius=2)
    for index, month in enumerate(("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")):
        deck.text(93 + index * 32, 379, month, 9, color=MUTED)
    deck.lines(50, 408, (
        "Сравнение по 361 общей дате и 8 498 парам часов.",
        "В 13:00 разность +0,857 °C, в 20:00 −0,742 °C.",
        "Вывод относится к двум точкам и одному году.",
    ), 14, 22, color=MUTED, width=450)
    deck.text(551, 173, "Наклон температуры АМЕ-60", 20, "Bold")
    deck.text(551, 201, "1992–2021 · с учётом сезонного хода", 15, color=MUTED)
    for y, value, label in (
        (265, "+0,057 °C/год", "254 пригодных месяца"),
        (341, "+0,089 °C/год", "96 месяцев восьми полных лет"),
    ):
        deck.text(550, y, value, 37, "Title", GREEN)
        deck.text(552, y + 26, label, 15, color=MUTED)
    deck.lines(552, 416, (
        "Отбор полных лет меняет оценку.",
        "АМЕ-60 и Сокол М1 дают отдельные ряды.",
        "Значимость и причины изменения не оценены.",
    ), 14, 22, color=MUTED, width=357)
    deck.finish()


def evidence(deck):
    deck.page(7, "ВОСПРОИЗВЕДЕНИЕ", "Источники · docs/criteria.pdf · results/research/ПЕРЕХОДЫ-ЧЕРЕЗ-НОЛЬ.md · results/browser/forecast.json", dark=True)
    deck.heading("Проверки для защиты")
    deck.line(541, 147, 541, 463, "#476857")
    criteria = (
        ("35%", "Качество расчёта", "Контрольный апрель и исходные сроки"),
        ("20%", "Проверка качества и ограничения", "420 месяцев покрытия совпали с таблицей кейса"),
        ("20%", "Работающий прототип", "Импорт JSON, таблица, график и выгрузки"),
        ("15%", "Практическая полезность", "От сводки до строки CSV и решения о включении"),
        ("10%", "Техническая обоснованность", "Пороги, знаменатели и независимый пересчёт"),
    )
    for index, (weight, title, detail) in enumerate(criteria):
        y = 167 + index * 58
        deck.text(50, y, weight, 18, "Bold", LIME)
        deck.text(116, y, title, 16, "Bold", WHITE, width=415)
        deck.text(116, y + 21, detail, 13, color=LIME, width=415)

    deck.text(571, 167, "Запустить у себя", 20, "Bold", WHITE)
    deck.text(571, 199, "Python 3.9+ · архивные расчёты офлайн", 14, color=LIME, width=341)
    deck.rect(571, 216, 341, 36, "#274B3E", radius=5)
    deck.text(585, 239, "python3 server.py", 15, "Bold", WHITE)
    deck.link(571, 279, "127.0.0.1:8765 →", "http://127.0.0.1:8765/", 15, LIME)
    deck.text(571, 323, "Пересчитать покрытие", 17, "Bold", WHITE)
    deck.text(571, 350, "python3 audit_quality.py --verify results/weather.sqlite", 12, color=LIME, width=341)
    deck.link(571, 382, "Репозиторий и инструкция →", REPO, 14, LIME)
    deck.line(571, 406, 912, 406, "#476857")
    deck.link(571, 433, "719 сверенных переходов через 0 °C →", "http://127.0.0.1:8765/events", 13, LIME)
    deck.link(571, 455, "Региональный прогноз на 3 дня →", "http://127.0.0.1:8765/forecast", 13, LIME)
    deck.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/Защита-exeCUtor.pdf")
    args = parser.parse_args()
    load_fonts()
    deck = Deck(args.output)
    for slide in (cover, april, raw_day, upload, method, comparison, evidence):
        slide(deck)
    deck.pdf.save()
    print(args.output)


if __name__ == "__main__":
    main()
