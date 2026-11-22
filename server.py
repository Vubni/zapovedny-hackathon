"""Локальный HTTP API и раздача файлов командного веб-интерфейса."""
import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from analytics import ARCHIVE, DATABASE, ROOT, analyze, comparison, export_csv, metadata, raw_day
from build_data import build, sha256

WEB = ROOT / "web"
API = {
    "/api/meta": "Список станций, диапазонов и параметров",
    "/api/series": "Суточные, декадные и месячные ряды с расчётом покрытия",
    "/api/raw": "Исходные сроки одного дня с номером строки CSV",
    "/api/export": "Таблица выбранного ряда в CSV",
    "/api/compare": "Общие даты и часы поляны и поля за 2025 год",
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
        self.send_header("Cache-Control", "no-cache")
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
            elif route == "/api/export":
                result = analyze(query)
                name = "_".join(result[k] for k in ("station","param","start","end","grain")) + ".csv"
                self.respond(export_csv(result), "text/csv; charset=utf-8", filename=name)
            elif route == "/api/raw":
                result = raw_day(query)
                name = f"{result['station']}_{result['param']}_{result['day']}.json" if query.get("download") == "1" else None
                self.send_json(result, filename=name)
            elif route == "/api/compare":
                self.send_json(comparison())
            elif route in {"/", "/index.html", "/app.js", "/style.css"}:
                path = WEB / ("index.html" if route == "/" else route[1:])
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
        except ValueError as error:
            self.send_json({"error":str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.log_error("Ошибка обработки: %r", error)
            self.send_json({"error":"Ошибка чтения данных. Подробности в журнале сервера."}, 500)


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
