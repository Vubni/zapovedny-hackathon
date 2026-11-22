"""Serve the network deployment through Waitress."""
import logging
import os
import sys
from email.message import Message
from http import HTTPStatus

from admin_auth import OperatorAuth
from server import ROOT, Routes, ready

DEFAULT_PORT = 8766
REQUEST_LIMIT = 17 * 1024 * 1024
LOGGER = logging.getLogger("meteo.http")
REQUEST_HEADERS = {
    "Host": "HTTP_HOST",
    "Origin": "HTTP_ORIGIN",
    "Cookie": "HTTP_COOKIE",
    "Sec-Fetch-Site": "HTTP_SEC_FETCH_SITE",
    "Content-Length": "CONTENT_LENGTH",
    "Content-Type": "CONTENT_TYPE",
    "Transfer-Encoding": "HTTP_TRANSFER_ENCODING",
}


class WSGIRequest(Routes):
    def __init__(self, environ, start_response, auth, port):
        self.command = environ["REQUEST_METHOD"]
        self.route = environ.get("PATH_INFO") or "/"
        self.cache_path = self.route
        self.query_string = environ.get("QUERY_STRING", "")
        self.local_port = port
        self.operator_auth = auth
        self.headers = Message()
        for header, key in REQUEST_HEADERS.items():
            value = environ.get(key)
            if value is not None and (value or not key.startswith("CONTENT_")):
                self.headers[header] = value
        self.input = environ["wsgi.input"]
        self.start_response = start_response
        self.response_body = b""

    def read_body(self, size, *, timeout):
        # Waitress buffers the request and enforces the channel timeout before dispatch.
        return self.input.read(size)

    def write_response(self, status, headers, body):
        self.start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        self.response_body = body

    def log_error(self, message, *args):
        LOGGER.error(message, *args)


def make_application(auth, port):
    def application(environ, start_response):
        request = WSGIRequest(environ, start_response, auth, port)
        if request.command == "GET":
            request.do_GET()
        elif request.command == "POST":
            request.do_POST()
        else:
            request.send_json({"error": "Метод запроса не поддерживается."}, HTTPStatus.NOT_IMPLEMENTED)
        return [request.response_body]

    return application


def main():
    from waitress import serve

    from observations_db import initialize

    try:
        port = int(os.environ.get("METEO_PORT", str(DEFAULT_PORT)))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        sys.exit("METEO_PORT должен быть целым числом от 1 до 65535.")

    try:
        auth = OperatorAuth.from_environment(ROOT)
    except (OSError, ValueError) as error:
        sys.exit(f"Не удалось настроить вход оператора. {error}")
    ready()
    try:
        initialize()
    except Exception as error:
        sys.exit(f"Не удалось открыть хранилище наблюдений ({type(error).__name__}).")

    serve(make_application(auth, port), host="0.0.0.0", port=port, threads=4,
          max_request_body_size=REQUEST_LIMIT, max_request_header_size=64 * 1024,
          channel_timeout=30, cleanup_interval=10,
          trusted_proxy=None, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
