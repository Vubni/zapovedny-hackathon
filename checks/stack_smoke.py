"""Create and verify synthetic observations on a disposable running Meteoarchive."""

import argparse
from dataclasses import dataclass
from email.message import Message
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DAY = "2026-01-15"
CSV = f"datetime,t_air,rh\n{DAY} 00:00,7,70\n{DAY} 01:00,11,72\n{DAY} 02:00,-100,74\n".encode()
EXPECTED_MEAN = 9
FOREIGN_ORIGIN = "https://foreign.invalid"


class SmokeFailure(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise SmokeFailure(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Response:
    headers: Message
    body: bytes

    def json(self):
        check(self.headers.get_content_type() == "application/json", "Ожидался ответ JSON.")
        return json.loads(self.body)


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, *, query=None, payload=None, body=None, mime=None, origin=None, status=200):
        url = self.base + path + ("?" + urlencode(query) if query else "")
        if payload is not None:
            body, mime = json.dumps(payload).encode(), "application/json"
        headers = {"Origin": origin} if origin is not None else {}
        if mime is not None:
            headers["Content-Type"] = mime
        request = Request(url, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            contents = response.read()
            check(response.status == status, f"{request.get_method()} {path}: HTTP {response.status}, ожидался {status}.")
            return Response(response.headers, contents)


def chart_value(client, station, expected):
    result = client.request("/api/observations", query={
        "stations": station, "chart": "air", "grain": "day", "start": DAY, "end": DAY,
    }).json()
    series = [row for row in result["series"] if row["station"] == station and row["parameter"] == "t_air"]
    check(len(series) == 1, "График не вернул ряд температуры тестовой станции.")
    points = series[0]["points"]
    check(len(points) == 1 and points[0]["x"] == DAY and points[0]["y"] == expected,
          f"График не подтвердил дневное значение {expected!r}.")


def file_access(client, state):
    query = {"id": state["file_id"]}
    files = client.request("/api/files").json()["files"]
    check(any(file["id"] == state["file_id"] for file in files), "Файл отсутствует в истории.")
    item = client.request("/api/files/item", query=query).json()
    check(item["station"] == state["station"] and item["status"] == "confirmed",
          "История не вернула подтверждённый файл тестовой станции.")
    check(item["confirmed"]["revision"] == state["revision"] and item["version"] == state["version"],
          "История вернула другую подтверждённую версию.")
    response = client.request("/api/files/original", query=query)
    check(response.headers.get_content_type() == "text/csv", "Исходный файл не отдан как CSV.")
    check(sha256(response.body).hexdigest() == state["original_sha256"], "Байты исходного файла изменились.")


def read_audit(client, state):
    query = {"file_id": state["file_id"], "param": "t_air"}
    audit = client.request("/api/review", query=query, body=b"").json()
    check(audit["station"] == state["station"] and audit["minimum_slots"] == 2
          and [row["value"] for row in audit["rows"]] == [7, 11, -100],
          "Аудит не восстановил паспорт станции и исходные значения.")
    token = {"token": audit["token"]}
    client.request("/api/review", query=token)
    return audit["token"]


def create(client):
    station_id = "stack-smoke-" + token_hex(6)
    station = {
        "id": station_id, "label": f"Стек · {station_id}", "profile": "kr1",
        "minimum_slots": 2, "params": ["t_air", "rh"], "latitude": 56.52, "longitude": 32.87,
    }
    files_page = client.request("/files")
    check(files_page.headers.get_content_type() == "text/html", "Страница файлов недоступна без входа.")
    client.request("/api/stations", payload=station, origin=FOREIGN_ORIGIN, status=403)
    registered = client.request("/api/stations", payload=station, origin=client.base, status=201).json()["station"]
    check(registered["id"] == station_id and registered["minimum_slots"] == 2,
          "Паспорт тестовой станции отличается от переданного.")
    chart_value(client, station_id, None)

    options = {"filename": station_id + ".csv", "station": station_id}
    client.request("/api/files/upload", query=options, body=CSV, mime="text/csv", origin=FOREIGN_ORIGIN, status=403)
    check(not any(item["station"] == station_id for item in client.request("/api/files").json()["files"]),
          "Запрос с чужим Origin изменил очередь загрузок.")
    file = client.request("/api/files/upload", query=options, body=CSV, mime="text/csv", status=201).json()
    check(file["status"] == "uploaded" and file["rows"] == 3, "CSV не зарегистрирован как три исходные строки.")
    preview_query = {"id": file["id"], "station": station_id}
    preview = client.request("/api/files/preview", payload=preview_query, origin=client.base).json()
    stats = preview["stats"]["parameters"]
    check(preview["stats"]["marked"] == 1 and stats["t_air"]["marked"] == 1 and stats["rh"]["kept"] == 3,
          "Первичная проверка не исключила только ожидаемую ячейку температуры.")
    confirm_query = dict(preview_query, revision=preview["revision"])
    client.request("/api/files/confirm", payload=confirm_query, origin=FOREIGN_ORIGIN, status=403)
    chart_value(client, station_id, None)
    confirmed = client.request("/api/files/confirm", payload=confirm_query, origin=client.base).json()
    chart_value(client, station_id, EXPECTED_MEAN)

    state = {
        "station": station_id, "file_id": file["id"], "revision": preview["revision"],
        "version": confirmed["version"], "original_sha256": sha256(CSV).hexdigest(),
    }
    file_access(client, state)
    state["audit_token"] = read_audit(client, state)
    return state


def verify(client, state):
    catalog = client.request("/api/stations").json()["stations"]
    station = next((item for item in catalog if item["id"] == state["station"]), None)
    check(station and (station["minimum_slots"], station["start"], station["end"]) == (2, DAY, DAY),
          "Каталог после запуска не восстановил паспорт и период тестовой станции.")
    chart_value(client, state["station"], EXPECTED_MEAN)
    file_access(client, state)
    client.request("/api/review", query={"token": state["audit_token"]}, status=410)
    read_audit(client, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Передайте METEO_SMOKE_URL через окружение. "
        "Режим create пишет синтетические данные. После перезапуска сервера "
        "выполните verify с тем же временным файлом состояния."
    ))
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--state", required=True, type=Path, help="Файл состояния в тестовой временной папке.")
    args = parser.parse_args()

    base = os.environ.get("METEO_SMOKE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.path or parsed.query or parsed.fragment):
        parser.error("Задайте METEO_SMOKE_URL в виде HTTP(S)-адреса без пути.")
    if args.mode == "create" and (args.state.exists() or not args.state.parent.is_dir()):
        parser.error("Для create укажите новый файл состояния в существующей временной папке.")
    try:
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.mode == "verify" else None
        client = Client(base)
        check(client.request("/healthz").json() == {"status": "ok"}, "Проверка готовности не вернула status=ok.")
        if args.mode == "create":
            state = create(client)
            fd = os.open(args.state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                json.dump(state, destination, ensure_ascii=False, indent=2)
                destination.write("\n")
            print(f"PASS create: CSV подтверждён без входа, график показывает {EXPECTED_MEAN} °C, чужой Origin отклонён.")
        else:
            verify(client, state)
            print(f"PASS verify: паспорт, исходник и {EXPECTED_MEAN} °C сохранены, старый аудит истёк.")
    except (OSError, TypeError, ValueError, KeyError, SmokeFailure) as error:
        parser.exit(1, f"FAIL {args.mode}: {error}\n")


if __name__ == "__main__":
    main()
