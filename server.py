"""Локальный HTTP API и раздача файлов командного веб-интерфейса."""
import argparse
import json
import mimetypes
import re
import sys
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from analytics import ARCHIVE, DATABASE, ROOT, analyze, comparison, database, export_csv, latest_five_days, metadata, raw_day
from build_data import build, sha256
from import_station import CONTENT_FORMATS, MAX_UPLOAD, preview_import
from request_origin import allow_browser_write, configured_origin
from review_service import ReportExpired, create_report, report_excel, report_page
from review_upload import MAX_BYTES as MAX_REVIEW_UPLOAD
from station_catalog import register_station, station_list
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
    "/api/stations": "Каталог и карта станций. POST регистрирует пользовательскую станцию",
    "/api/observations": "Ряды, покрытие и сравнение станций за общий выбранный период",
    "/api/observations/latest-five": "Таблица наблюдений за пять суток по последний фактический срок показателя",
    "/api/observations/latest-five.csv": "CSV таблицы за последние пять суток",
    "/api/observation-coverage": "Матрица покрытия выбранных станций за отмеченные интервалы",
    "/api/files": "Очередь, исходные файлы, предпросмотр и подтверждённые наборы наблюдений",
    "/api/files/from-observations": "POST создаёт рабочий файл из выбранных наблюдений",
}
WEB_FILES = {
    "/": "observations.html",
    "/index.html": "observations.html",
    "/observations": "observations.html",
    "/journal": "index.html",
    "/files": "files-page.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/forecast": "forecast.html",
    "/forecast.js": "forecast.js",
    "/forecast-example.json": "forecast-example.json",
    "/events": "events.html",
    "/events.html": "events.html",
    "/events.js": "events.js",
    "/calendar": "calendar.html",
    "/calendar.js": "calendar.js",
    "/calendar.css": "calendar.css",
    "/precip": "precip.html",
    "/precip.js": "precip.js",
    "/precip.css": "precip.css",
    "/precip-example.json": "precip-example.json",
}
for asset in ("observation-timeline.js", "clgz-logo.png", "reserve-brand.css", "archive.css", "archive-shell.js", "legacy-shell.css", "observations.js", "observations.css", "chart-archive.js", "chart-report.js",
              "station-map.js", "station-map.css", "station-map-data.js", "russia-map-data.js", "precip-boundary.js", "observation-chart.js",
              "station-map-raster.js", "russia-relief.webp", "reserve-imagery.webp",
              "coverage-overview.js", "coverage-overview.css", "observation-indicators.js", "observation-indicators.css", "recent-days.js",
              "observation-chart.css", "files-page.js", "files-page.css", "weather-pages.css",
              "fonts/manrope.ttf", "fonts/OFL-Manrope.txt"):
    WEB_FILES["/" + asset] = asset
FILE_ACTIONS = {"/api/files/preview": "preview_file", "/api/files/confirm": "confirm_file",
                "/api/files/discard": "discard_clean", "/api/files/from-observations": "from_observations"}
LEGACY_REDIRECTS = {
    "/import": "/files?source=upload",
    "/import.html": "/files?source=upload",
    "/review": "/files",
    "/review.html": "/files",
    "/operator": "/files",
    "/operator.html": "/files",
    "/model": "/forecast",
    "/model.html": "/forecast",
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


def check_ready():
    from observations_db import check_ready as check_observations

    check_observations()
    with closing(database()) as index:
        if index.execute("SELECT 1 FROM daily LIMIT 1").fetchone() is None:
            raise ValueError("Индекс архива пуст.")


class Routes:
    """Request routing shared by the loopback HTTP and container WSGI adapters."""

    def respond(self, body, content_type, status=200, filename=None, *, headers=None):
        route = self.cache_path
        no_store = self.command == "POST" or route.startswith((
            "/api/review", "/api/files", "/api/observation", "/api/stations", "/files", "/healthz",
        )) or route in LEGACY_REDIRECTS
        response_headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store" if no_store else "no-cache"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        response_headers.extend((headers or {}).items())
        if filename:
            fallback = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
            disposition = f'attachment; filename="{fallback}"'
            if fallback != filename:
                disposition += "; filename*=UTF-8''" + quote(filename, safe="")
            response_headers.append(("Content-Disposition", disposition))
        self.write_response(status, response_headers, body)

    def send_json(self, value, status=200, filename=None, *, headers=None):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.respond(body, "application/json; charset=utf-8", status, filename, headers=headers)

    def do_GET(self):
        try:
            route = self.route
            query = {k: v[-1] for k, v in parse_qs(self.query_string, keep_blank_values=True).items()}
            if route == "/healthz":
                try:
                    check_ready()
                except Exception:
                    self.send_json({"status": "unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
                else:
                    self.send_json({"status": "ok"})
            elif route == "/api":
                self.send_json(API)
            elif route in LEGACY_REDIRECTS:
                encoded = quote(self.query_string, safe="!$&'()*+,-./:;=?@_%~[]")
                location = LEGACY_REDIRECTS[route]
                if encoded:
                    location += ("&" if "?" in location else "?") + encoded
                self.respond(b"", "text/plain; charset=utf-8", HTTPStatus.SEE_OTHER,
                             headers={"Location": location})
            elif route == "/api/stations":
                self.send_json(station_list())
            elif route == "/api/observations":
                from observations import chart_data
                self.send_json(chart_data(query))
            elif route in ("/api/observations/latest-five", "/api/observations/latest-five.csv"):
                from recent_observations import export_recent_csv, latest_five
                table = latest_five(query)
                if route.endswith(".csv"):
                    name, body = export_recent_csv(table)
                    self.respond(body, "text/csv; charset=utf-8", filename=name)
                else:
                    self.send_json(table)
            elif route == "/api/observation-coverage":
                from observations import coverage_overview
                self.send_json(coverage_overview(query))
            elif route in ("/api/files", "/api/files/item", "/api/files/preview", "/api/files/original", "/api/files/csv", "/api/files/excel"):
                import observation_store as store
                if route == "/api/files":
                    self.send_json(store.list_files())
                elif route == "/api/files/item":
                    self.send_json(store.file_record(query))
                elif route == "/api/files/preview":
                    self.send_json(store.get_file(query))
                elif route == "/api/files/original":
                    name, kind, body = store.original_file(query)
                    self.respond(body, kind, filename=name)
                elif route == "/api/files/csv":
                    name, body = store.draft_csv(query)
                    self.respond(body, "text/csv; charset=utf-8", filename=name)
                else:
                    name, body = store.preview_excel(query)
                    self.respond(body, XLSX_TYPE, filename=name)
            elif route == "/api/meta":
                from review_filter import quality_metadata
                self.send_json({**metadata(), "quality": quality_metadata()})
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
            elif route in WEB_FILES:
                path = WEB / WEB_FILES[route]
                if route in ("/", "/index.html") and ("station" in query or query.get("view") == "compare") and "stations" not in query:
                    path = WEB / "index.html"  # Existing links to audit days, years and paired-station evidence.
                if not path.is_file():
                    self.send_json({"error":"Файл интерфейса отсутствует. Данные доступны по /api."}, 404)
                    return
                kind = "image/webp" if path.suffix == ".webp" else mimetypes.guess_type(path)[0]
                kind = kind or "application/octet-stream"
                self.respond(path.read_bytes(), kind + "; charset=utf-8" if kind.startswith("text/") else kind)
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
            route = self.route
            query = {k: v[-1] for k, v in parse_qs(self.query_string, keep_blank_values=True).items()}
            file_upload = route in ("/api/files/upload", "/api/files/corrected")
            action = route in FILE_ACTIONS or route == "/api/stations"
            if route not in ("/api/import", "/api/review") and not file_upload and not action:
                self.send_json({"error": "Маршрут отсутствует"}, HTTPStatus.NOT_FOUND)
                return
            if not allow_browser_write(self.headers, local_port=self.local_port,
                                       request_scheme=self.request_scheme, public_origin=self.public_origin):
                self.send_json({"error": "Отправьте запрос со страницы этого Метеоархива."}, HTTPStatus.FORBIDDEN)
                return
            source_format = CONTENT_FORMATS.get(self.headers.get_content_type())
            is_review = route == "/api/review"
            if route == "/api/import" and source_format is None:
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
            if size < 0 or (route == "/api/import" and size == 0):
                raise ValueError("Файл не содержит данных")
            maximum = 64 * 1024 if action else MAX_REVIEW_UPLOAD if is_review or file_upload else MAX_UPLOAD
            if route == "/api/files/corrected":
                from observation_store import MAX_CORRECTED_BYTES
                maximum = MAX_CORRECTED_BYTES
            if size > maximum:
                self.send_json({"error": "Слишком большой запрос настроек" if action else
                                f"Размер файла превышает {maximum // (1024*1024)} МиБ"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = self.read_body(size, timeout=10)
            if len(body) != size:
                raise ValueError("Файл загружен не полностью")
            if action:
                if body:
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("Для настроек используйте JSON.")
                    settings = json.loads(body)
                    if not isinstance(settings, dict):
                        raise ValueError("Настройки должны быть объектом JSON.")
                    if route in FILE_ACTIONS:
                        settings = {key: value for key, value in settings.items() if value is not None}
                        for key in ("filters", "sources"):
                            items = settings.get(key)
                            if isinstance(items, list) and all(isinstance(item, str) for item in items):
                                settings[key] = ",".join(items)
                        for key, value in settings.items():
                            if key in ("ranges", "thresholds"):
                                if not isinstance(value, (dict, str)):
                                    raise ValueError("Передайте границы и пороги по названиям параметров.")
                            elif key in ("fill_external", "fill_model") and isinstance(value, bool):
                                settings[key] = int(value)
                            elif type(value) not in (str, int):
                                raise ValueError("Проверьте настройки обработки, фильтры и источники.")
                    query.update(settings)
                if route == "/api/stations":
                    self.send_json(register_station(query), 201)
                else:
                    import observation_store as store
                    self.send_json(getattr(store, FILE_ACTIONS[route])(query))
            elif route == "/api/files/corrected":
                from observation_store import corrected_file
                self.send_json(corrected_file(query, body, self.headers.get_content_type()))
            elif file_upload:
                from observation_store import upload_file
                self.send_json(upload_file(query, body, self.headers.get_content_type()), 201)
            elif is_review:
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


class Handler(Routes, BaseHTTPRequestHandler):
    request_scheme = "http"

    @property
    def cache_path(self):
        try:
            return self.route
        except ValueError:
            return self.path

    @property
    def route(self):
        return urlsplit(self.path).path

    @property
    def query_string(self):
        return urlsplit(self.path).query

    @property
    def public_origin(self):
        return getattr(self.server, "public_origin", None)

    @property
    def local_port(self):
        return self.server.server_port

    def read_body(self, size, *, timeout):
        self.connection.settimeout(timeout)
        return self.rfile.read(size)

    def write_response(self, status, headers, body):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ready()
    try:
        origin = configured_origin()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except (OSError, ValueError) as error:
        sys.exit(f"Не удалось запустить сервер на порту {args.port}: {error}")
    with server:
        server.public_origin = origin
        print(f"API метеожурнала — http://127.0.0.1:{server.server_port}/api", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
