"""Локальный HTTP API и раздача файлов командного веб-интерфейса."""
import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from analytics import ARCHIVE, DATABASE, ROOT, analyze, comparison, export_csv, latest_five_days, metadata, raw_day
from build_data import build, sha256
from import_station import CONTENT_FORMATS, MAX_UPLOAD, preview_import
from review_service import ReportExpired, create_report, report_excel, report_page
from review_upload import MAX_BYTES as MAX_REVIEW_UPLOAD
from station_model import predict
from temperature_events import export_events, query_events

WEB = ROOT / "web"
IMPORT_EXAMPLE = ROOT / "results" / "examples" / "kr1-station-2024-04-01.json"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
API = {
    "/api/meta": "Список станций, диапазонов и параметров",
    "/api/series": "Суточные, декадные и месячные ряды с расчётом покрытия",
    "/api/raw": "Исходные сроки одного дня с номером строки CSV",
    "/api/export": "Таблица выбранного ряда в CSV",
    "/api/export/latest-five": "CSV за последние пять календарных суток архива станции",
    "/api/compare": "Общие даты и часы поляны и поля за 2025 год",
    "/api/events": "Переходы среднесуточной температуры воздуха через 0 °C",
    "/api/events/export": "Выгрузка выбранных переходов в CSV",
    "/api/import": "POST с файлом наблюдений в формате CSV или JSON для отдельного расчёта",
    "/api/review": "POST — фильтрация архива или XLSX/CSV/JSON, GET — страница готового расчёта по token",
    "/api/review.xlsx": "Полный расчёт в редактируемом Excel по token",
    "/api/review-original.xlsx": "Загруженная книга XLSX с отметками на исходном листе и рабочими решениями",
    "/api/model": "Прогноз следующей среднесуточной температуры по станции",
}
WEB_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/import": "import.html",
    "/import.html": "import.html",
    "/import.js": "import.js",
    "/forecast": "forecast.html",
    "/forecast.js": "forecast.js",
    "/forecast-example.json": "forecast-example.json",
    "/events": "events.html",
    "/events.html": "events.html",
    "/events.js": "events.js",
    "/calendar": "calendar.html",
    "/calendar.js": "calendar.js",
    "/calendar.css": "calendar.css",
    "/review": "review.html",
    "/review.js": "review.js",
    "/review.css": "review.css",
    "/model": "model.html",
    "/model.js": "model.js",
    "/model.css": "model.css",
    "/precip": "precip.html",
    "/precip.js": "precip.js",
    "/precip.css": "precip.css",
    "/precip-example.json": "precip-example.json",
}


def ready():
    if not ARCHIVE.is_file():
        raise SystemExit(f"Для запуска нужен файл {ARCHIVE}")
    manifest_path = DATABASE.parent / "build.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (DATABASE.is_file() and manifest["source_sha256"] == sha256(ARCHIVE)
                and manifest["database_sha256"] == sha256(DATABASE)):
            return
    except (OSError, ValueError, KeyError):
        pass
    print("Готовлю и проверяю базу наблюдений…", flush=True)
    build(ARCHIVE, DATABASE.parent)


class Handler(BaseHTTPRequestHandler):
    def respond(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        no_store = self.command == "POST" or self.path.startswith("/api/review")
        self.send_header("Cache-Control", "no-store" if no_store else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200, filename=None):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.respond(body, "application/json; charset=utf-8", status, filename)

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
            route = parts.path
            query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            if route == "/api":
                self.send_json(API)
            elif route == "/api/meta":
                self.send_json(metadata())
            elif route == "/api/series":
                self.send_json(analyze(query))
            elif route in ("/api/export", "/api/export/latest-five"):
                result = latest_five_days(query) if route.endswith("/latest-five") else analyze(query)
                name = "_".join(result[k] for k in ("station","param","start","end","grain")) + ".csv"
                self.respond(export_csv(result), "text/csv; charset=utf-8", filename=name)
            elif route == "/api/raw":
                result = raw_day(query)
                name = f"{result['station']}_{result['param']}_{result['day']}.json" if query.get("download") == "1" else None
                self.send_json(result, filename=name)
            elif route == "/api/compare":
                self.send_json(comparison())
            elif route == "/api/events":
                self.send_json(query_events(query))
            elif route == "/api/events/export":
                result = query_events(query)
                name = "_".join(result[k] for k in ("station", "start", "end"))
                self.respond(export_events(result), "text/csv; charset=utf-8",
                             filename=f"temperature_crossings_{name}.csv")
            elif route == "/api/import/example":
                self.respond(IMPORT_EXAMPLE.read_bytes(), "application/json; charset=utf-8",
                             filename=IMPORT_EXAMPLE.name)
            elif route == "/api/review":
                self.send_json(report_page(query))
            elif route in ("/api/review.xlsx", "/api/review-original.xlsx"):
                filename, body = report_excel(query, annotated=route == "/api/review-original.xlsx")
                self.respond(body, XLSX_TYPE, filename=filename)
            elif route == "/review-example.xlsx":
                self.respond((ROOT / "results/review/review-example.xlsx").read_bytes(), XLSX_TYPE,
                             filename="review-example.xlsx")
            elif route == "/api/model":
                self.send_json(predict(query))
            elif route in WEB_FILES:
                path = WEB / WEB_FILES[route]
                if not path.is_file():
                    self.send_json({"error":"Файл интерфейса отсутствует. Данные доступны по /api."}, 404)
                    return
                self.respond(path.read_bytes(), mimetypes.guess_type(path)[0] + "; charset=utf-8")
            elif route == "/method":
                self.respond((ROOT/"docs"/"МЕТОДИКА.md").read_bytes(), "text/plain; charset=utf-8")
            else:
                self.send_json({"error":"Маршрут отсутствует"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ReportExpired as error:
            self.send_json({"error": str(error)}, HTTPStatus.GONE)
        except ValueError as error:
            self.send_json({"error":str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.log_error("Ошибка обработки: %r", error)
            self.send_json({"error":"Ошибка чтения данных. Подробности в журнале сервера."}, 500)

    def do_POST(self):
        try:
            parts = urlsplit(self.path)
            if parts.path not in ("/api/import", "/api/review"):
                self.send_json({"error": "Маршрут отсутствует"}, HTTPStatus.NOT_FOUND)
                return
            source_format = CONTENT_FORMATS.get(self.headers.get_content_type())
            is_review = parts.path == "/api/review"
            if not is_review and source_format is None:
                self.send_json({"error": "Выберите файл CSV или JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                self.send_json({"error": "Укажите размер файла"}, HTTPStatus.LENGTH_REQUIRED)
                return
            try:
                size = int(lengths[0])
            except ValueError:
                raise ValueError("Указан неверный размер файла") from None
            if size < 0 or (not is_review and size == 0):
                raise ValueError("Файл не содержит данных")
            maximum = MAX_REVIEW_UPLOAD if is_review else MAX_UPLOAD
            if size > maximum:
                self.send_json({"error": f"Размер файла превышает {maximum // (1024*1024)} МиБ"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            self.connection.settimeout(10)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Файл загружен не полностью")
            query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            if is_review:
                self.send_json(create_report(query, body, self.headers.get_content_type()))
            else:
                self.send_json(preview_import(body, source_format, query))
        except (BrokenPipeError, ConnectionResetError):
            return
        except TimeoutError:
            self.send_json({"error": "Не удалось прочитать файл за отведённое время"}, HTTPStatus.REQUEST_TIMEOUT)
        except RecursionError:
            self.send_json({"error": "Ожидается JSON-массив объектов наблюдений без вложенных записей"},
                           HTTPStatus.BAD_REQUEST)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.log_error("Ошибка импорта: %r", error)
            self.send_json({"error": "Ошибка расчёта. Подробности в журнале сервера."},
                           HTTPStatus.INTERNAL_SERVER_ERROR)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ready()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        sys.exit(f"Не удалось запустить сервер на порту {args.port}: {error}")
    with server:
        print(f"API метеожурнала — http://127.0.0.1:{server.server_port}/api", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
