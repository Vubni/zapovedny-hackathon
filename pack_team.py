"""Собрать переносимый архив команды и проверить запуск в отдельной папке."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from server import ready

ROOT = Path(__file__).resolve().parent
FOLDERS = ("web", "checks", "docs", "data", "results")
SKIP = {"__pycache__", ".DS_Store", "node_modules"}


def sources():
    files = {p for p in ROOT.iterdir() if p.is_file() and p.suffix in {".py", ".md"}}
    files.add(ROOT / ".gitignore")
    files.add(ROOT / ".gitattributes")
    config = ROOT / ".impeccable" / "config.json"
    if config.is_file():
        files.add(config)
    for folder in FOLDERS:
        for p in (ROOT / folder).rglob("*"):
            if p.is_file() and not p.is_symlink() and not any(
                    part in SKIP or part.startswith(".weather-build-") for part in p.relative_to(ROOT).parts):
                files.add(p)
    return sorted(files)


def check(project, command, expected):
    result = subprocess.run([sys.executable, "-B", *command], cwd=project, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    if expected not in result.stdout:
        raise ValueError(f"Не подтвердился результат {command}: {result.stdout}")


def pack(destination):
    ready()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as stage:
        stage = Path(stage)
        packet = stage / "packet.zip"
        with ZipFile(packet, "w", ZIP_DEFLATED) as archive:
            for file in sources():
                entry = ZipInfo(file.relative_to(ROOT).as_posix(), date_time=(1980,1,1,0,0,0))
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, file.read_bytes(), compress_type=ZIP_DEFLATED)
        project = stage / "project"
        with ZipFile(packet) as archive:
            if archive.testzip() is not None:
                raise ValueError("Повреждён член архива")
            archive.extractall(project)
            file_count = len(archive.namelist())
        database = project / "results" / "weather.sqlite"
        manifest = json.loads((project / "results" / "build.json").read_text(encoding="utf-8"))
        if hashlib.sha256(database.read_bytes()).hexdigest() != manifest["database_sha256"]:
            raise ValueError("Контрольная сумма базы в комплекте не совпала с манифестом")
        check(project, ["journal.py"], "334/366")
        check(project, ["-c", "from observations import chart_data; from station_catalog import station_list; "
                        "r = chart_data(dict(stations='kr1', chart='pressure', start='2024-01-01', "
                        "end='2024-12-31', grain='month')); "
                        "print(len(station_list()['stations']), r['coverage'][0]['valid_days'], "
                        "r['coverage'][0]['valid_months'])"], "3 334 11")
        database.unlink()  # Rehearse a cold rebuild in the temporary extracted copy.
        check(project, ["research_results.py"], "оба наклона")
        check(project, ["journal.py"], "334/366")
        check(project, ["replay_day.py", "results/examples/kr1-pressure-april-29-incomplete.json",
                        "--archive", "data/meteo_hack_dataset.zip"], "все строки за дату (31)")
        check(project, ["import_station.py", "--station", "kr1", "--year", "2024", "--input",
                        "results/examples/kr1-station-2024-04-01.json", "--output-dir", "trial-import"],
              "Прочитано строк: 48")
        check(project, ["-c", "from review_service import create_report, report_excel; "
                        "q = dict(station='kr1', param='pressure_hpa', start='2024-04-01', end='2024-04-01', filters='stuck'); "
                        "r = create_report(q, b'', ''); "
                        "print(r['counts']['excluded'], report_excel(r)[1][:2])"], "38 b'PK'")
        check(project, ["station_model.py", "--output", "trial-model.json"], "Кр1")
        for rel in ("web/observations.html", "web/observations.js", "web/chart-archive.js", "web/station-map-data.js",
                    "web/observation-chart.js", "web/files-page.html", "web/files-page.js",
                    "web/yandex-location.js", "web/yandex-station-sdk.js", "web/coverage-overview.js",
                    "web/observation-indicators.js", "admin_auth.py", "web/operator.html",
                    "web/fonts/manrope.ttf", "web/fonts/OFL-Manrope.txt",
                    "web/index.html", "web/app.js", "web/import.html", "web/import.js",
                    "web/events.html", "web/events.js", "web/forecast.html", "web/forecast.js",
                    "web/forecast-example.json", "web/calendar.html", "web/calendar.js",
                    "web/calendar.css", "web/review.html", "web/review.js", "web/review.css",
                    "web/model.html", "web/precip.html", "web/precip-example.json",
                    "results/review/review-example.xlsx", "results/review/external-validation.json",
                    "docs/Защита-exeCUtor.pdf"):
            if not (project / rel).is_file():
                raise ValueError(f"Отсутствует файл для показа {rel}")
        sha = hashlib.sha256(packet.read_bytes()).hexdigest()
        note = stage / "proof.txt"
        note.write_text(
            f"{destination.name} · SHA-256 {sha}\n"
            f"Проверено файлов {file_count}, Python {sys.version.split()[0]}.\n"
            "В отдельной папке проверены CRC, SHA-256 комплектной базы, запуск с базой и повторная сборка из исходного ZIP, воспроизведение исследования и месячного расчёта, сверка "
            "со всеми 31 исходными строками 29 апреля и импорт 48 наблюдений за 1 апреля.\n"
            "Проверены отбор 38 записей по залипанию, выгрузка XLSX и обучение модели станции из распакованной копии.\n"
            "В Наблюдениях из распакованной копии доступны три архивных станции. "
            "У Кр1 проходят 334 дня и 11 месяцев давления за 2024 год. "
            "Протокол HTTP для новых станций и импорта находится в results/observations/workflow.json.\n",
            encoding="utf-8")
        os.replace(packet, destination)
        os.replace(note, destination.with_name("Проверка.txt"))
        print(f"{destination} · {file_count} файлов · SHA-256 {sha}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "handoff" / "meteo-team.zip")
    pack(parser.parse_args().output)
