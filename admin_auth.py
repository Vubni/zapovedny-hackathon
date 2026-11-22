"""Authenticate the single archive operator and gate administrative HTTP routes."""
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections import deque
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

COOKIE_NAME = "meteo_operator"
SESSION_LIFETIME = 1800
MAX_SESSIONS = 64
MAX_LOGIN_BYTES = 4096
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 60
OPERATOR_REPORT_PREFIX = "operator."
SESSION = "/api/operator/session"
LOGIN = "/api/operator/login"
LOGOUT = "/api/operator/logout"
REVIEW_REPORTS = {"/api/review", "/api/review.xlsx", "/api/review-original.xlsx"}
LOGIN_REQUIRED = {"error": "Для этого действия войдите как оператор через /operator."}


def local_password(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        with NamedTemporaryFile(dir=path.parent, mode="w+", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
            handle.flush()
            try:
                os.link(handle.name, path)
            except FileExistsError:
                pass
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError(f"Для файла {path} требуются права 600.")
        return handle.read(MAX_LOGIN_BYTES).rstrip("\r\n")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).digest()


class OperatorAuth:
    def __init__(self, password, origin=None, password_file=None):
        if len(password.encode("utf-8")) < 16 or len(password.encode("utf-8")) > 1024:
            raise ValueError("Пароль оператора должен содержать от 16 до 1024 байт в UTF-8.")
        self.origin = origin
        self.password_file = password_file
        self.password_hash = digest(password)
        self.sessions = {}
        self.attempts = deque()
        self.lock = Lock()

    @classmethod
    def from_environment(cls, root):
        password = os.environ.get("METEO_ADMIN_PASSWORD")
        origin = os.environ.get("METEO_ADMIN_ORIGIN")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or
                parsed.path or parsed.query or parsed.fragment or origin != f"https://{parsed.netloc.lower()}"
                or parsed.port == 0):
                raise ValueError("METEO_ADMIN_ORIGIN должен быть адресом HTTPS без пути, например https://meteo.example.")
            if password is None:
                raise ValueError("Для сетевого входа задайте METEO_ADMIN_PASSWORD.")
        if password is not None:
            return cls(password, origin)
        path = Path(os.environ.get("METEO_ADMIN_PASSWORD_FILE", root / ".local" / "operator-password")).expanduser()
        path = path.absolute()
        return cls(local_password(path), password_file=path)

    def request_origin(self, handler):
        hosts = handler.headers.get_all("Host", [])
        if len(hosts) != 1:
            return None
        host = hosts[0].lower()
        if self.origin:
            return self.origin if host == urlsplit(self.origin).netloc else None
        port = handler.local_port
        expected = {"localhost", "127.0.0.1"}
        if port != 80:
            expected = {f"{hostname}:{port}" for hostname in expected}
        return "http://" + host if host in expected else None

    def same_origin(self, handler):
        origin = self.request_origin(handler)
        origins = handler.headers.get_all("Origin", [])
        sites = handler.headers.get_all("Sec-Fetch-Site", [])
        return bool(origin and (not origins or origins == [origin]) and (not sites or sites == ["same-origin"]))

    def request_token(self, handler):
        if not self.request_origin(handler):
            return None
        values = handler.headers.get_all("Cookie", [])
        if not values or any("," in value for value in values):
            return None
        pairs = "; ".join(values)
        if sum(part.partition("=")[0].strip() == COOKIE_NAME for part in pairs.split(";")) != 1:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(pairs)
        except CookieError:
            return None
        cookie = cookies.get(COOKIE_NAME)
        if cookie and len(cookie.value) == 43 and cookie.value.isascii():
            return digest(cookie.value)
        return None

    def prune(self, now):
        for token in [token for token, expires in self.sessions.items() if expires <= now]:
            del self.sessions[token]

    def authenticated(self, handler):
        token = self.request_token(handler)
        if token is None:
            return False
        with self.lock:
            self.prune(monotonic())
            return token in self.sessions

    def sign_in(self, handler, candidate):
        candidate_hash = digest(candidate)
        old_token = self.request_token(handler)
        with self.lock:
            now = monotonic()
            self.prune(now)
            while self.attempts and self.attempts[0] <= now - ATTEMPT_WINDOW:
                self.attempts.popleft()
            if len(self.attempts) >= MAX_ATTEMPTS:
                return HTTPStatus.TOO_MANY_REQUESTS, None
            if not hmac.compare_digest(candidate_hash, self.password_hash):
                self.attempts.append(now)
                return HTTPStatus.UNAUTHORIZED, None
            self.sessions.pop(old_token, None)
            if len(self.sessions) >= MAX_SESSIONS:
                del self.sessions[next(iter(self.sessions))]
            token = secrets.token_urlsafe(32)
            self.sessions[digest(token)] = now + SESSION_LIFETIME
            return HTTPStatus.OK, token

    def sign_out(self, handler):
        token = self.request_token(handler)
        with self.lock:
            self.sessions.pop(token, None)

    def cookie(self, token=""):
        age = SESSION_LIFETIME if token else 0
        secure = "; Secure" if self.origin else ""
        return f"{COOKIE_NAME}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"


class OperatorHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler, root):
        self.operator_auth = OperatorAuth.from_environment(root)
        super().__init__(address, handler)


def _password_input(handler):
    if handler.headers.get_content_type() != "application/json":
        raise ValueError("Для входа отправьте JSON.")
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdecimal():
        raise ValueError("Некорректный запрос входа.")
    if not 0 < int(lengths[0]) <= MAX_LOGIN_BYTES:
        raise ValueError("Некорректный запрос входа.")
    size = int(lengths[0])
    raw = handler.read_body(size, timeout=5)
    if len(raw) != size:
        raise ValueError("Запрос входа передан не полностью.")
    fields = json.loads(raw)
    if not isinstance(fields, dict) or not isinstance(fields.get("password"), str):
        raise ValueError("Введите пароль оператора.")
    return fields["password"]


def _account_request(handler, route, auth):
    method = "GET" if route == SESSION else "POST"
    if handler.command != method:
        handler.send_json({"error": "Метод запроса не поддерживается."}, HTTPStatus.METHOD_NOT_ALLOWED,
                          headers={"Allow": method})
    elif auth is None or not auth.request_origin(handler):
        handler.send_json({"error": "Вход оператора недоступен по этому адресу."}, HTTPStatus.FORBIDDEN)
    elif route == SESSION:
        handler.send_json({"authenticated": auth.authenticated(handler)})
    elif not auth.same_origin(handler):
        handler.send_json({"error": "Откройте страницу входа по адресу Метеоархива."}, HTTPStatus.FORBIDDEN)
    elif route == LOGOUT:
        auth.sign_out(handler)
        handler.send_json({"authenticated": False}, headers={"Set-Cookie": auth.cookie()})
    else:
        try:
            candidate = _password_input(handler)
            status, token = auth.sign_in(handler, candidate)
        except (ValueError, RecursionError):
            handler.send_json({"error": "Проверьте запрос входа и введите пароль."}, HTTPStatus.BAD_REQUEST)
            return
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            handler.send_json({"error": "Слишком много попыток. Повторите вход через минуту."}, status,
                              headers={"Retry-After": str(ATTEMPT_WINDOW)})
        elif token is None:
            handler.send_json({"error": "Неверный пароль оператора."}, status)
        else:
            handler.send_json({"authenticated": True}, headers={"Set-Cookie": auth.cookie(token)})


def handle_operator_access(handler, route, query):
    """Return true when the request has already received a response."""
    auth = handler.operator_auth
    if route in {SESSION, LOGIN, LOGOUT}:
        _account_request(handler, route, auth)
        return True
    writing = handler.command == "POST"
    restricted = (
        route == "/files" or route == "/api/files" or route.startswith("/api/files/")
        or (writing and route == "/api/stations")
        or (route == "/api/review" and writing and "file_id" in query)
        or (not writing and route in REVIEW_REPORTS and query.get("token", "").startswith(OPERATOR_REPORT_PREFIX))
    )
    if not restricted:
        return False
    if auth is None or not auth.authenticated(handler):
        if route == "/files" and handler.command == "GET":
            handler.respond(b"", "text/plain; charset=utf-8", HTTPStatus.SEE_OTHER, headers={"Location": "/operator"})
        else:
            handler.send_json(LOGIN_REQUIRED, HTTPStatus.UNAUTHORIZED)
        return True
    if writing and not auth.same_origin(handler):
        handler.send_json({"error": "Откройте Метеоархив по его адресу."}, HTTPStatus.FORBIDDEN)
        return True
    return False
