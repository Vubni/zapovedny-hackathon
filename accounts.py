"""Global accounts, browser sessions and personal observation views."""
import argparse
from collections import deque
from datetime import datetime, timezone
from getpass import getpass
from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
import json
import re
import secrets
import sys
from threading import Lock
from time import monotonic, time
from urllib.parse import parse_qsl, urlencode, urlsplit

from observations_db import connection


CENTRAL_CENTER = {"id": "central-forest", "name": "Центрально-Лесной заповедник"}
COOKIE_NAME = "meteo_account"
SESSION_SECONDS = 12 * 60 * 60
MAX_WORKSPACE_BYTES = 8192
MAX_SAVED_VIEWS = 100
MAX_PASSWORD_BYTES = 1024
PBKDF_ROUNDS = 600_000
RATE_WINDOW = 5 * 60
RATE_EMAIL = 8
RATE_TOTAL = 128
ROLE_RESEARCHER = "researcher"
ROLE_CENTER_ADMIN = "center_admin"
ROLE_GLOBAL_ADMIN = "global_admin"
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")
TOKEN = re.compile(r"[a-zA-Z0-9_-]{43}")
WORKSPACE_KEYS = {"stations", "start", "end", "intervals", "calendar", "grain", "mode", "charts",
                  "coverage", "coverageParameter", "indicators", "presentation"}
WORKSPACE_ID = re.compile(r"[a-zA-Z0-9._-]{1,80}")
ACCOUNT_FIELDS = "id, name, email, role, center_id"
VIEW_FIELDS = "id, name, center_id, created_at"
_attempts = deque()
_attempts_lock = Lock()

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meteo_centers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE IF NOT EXISTS meteo_accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        center_id TEXT REFERENCES meteo_centers(id),
        salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        CHECK ((role = 'global_admin' AND center_id IS NULL)
               OR (role IN ('researcher', 'center_admin') AND center_id IS NOT NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS meteo_login_sessions (
        token_digest TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES meteo_accounts(id) ON DELETE CASCADE,
        active_center_id TEXT NOT NULL REFERENCES meteo_centers(id),
        expires_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS meteo_login_sessions_expiry ON meteo_login_sessions (expires_at)",
    """CREATE TABLE IF NOT EXISTS meteo_saved_views (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES meteo_accounts(id) ON DELETE CASCADE,
        center_id TEXT NOT NULL REFERENCES meteo_centers(id),
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS meteo_saved_views_owner ON meteo_saved_views (account_id, center_id, created_at)",
)

METHODS = {
    "/api/account/me": ("GET",),
    "/api/account/login": ("POST",),
    "/api/account/logout": ("POST",),
    "/api/account/select-center": ("POST",),
    "/api/account/users": ("GET", "POST"),
    "/api/account/centers": ("POST",),
    "/api/account/sessions": ("GET", "POST"),
    "/api/account/sessions/delete": ("POST",),
    "/api/account/session": ("GET",),
}


class AccountError(ValueError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def initialize():
    with connection(write=True, global_store=True) as db:
        for statement in SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO meteo_centers (id,name,name_key) VALUES (?,?,?) ON CONFLICT (id) DO NOTHING",
                   (CENTRAL_CENTER["id"], CENTRAL_CENTER["name"], CENTRAL_CENTER["name"].casefold()))


def _name(value, prompt):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100 or not value.strip().isprintable():
        raise AccountError(prompt)
    return value.strip()


def _email(value):
    result = value.strip().casefold() if isinstance(value, str) else ""
    if len(result) > 254 or not EMAIL.fullmatch(result):
        raise AccountError("Введите корректный адрес электронной почты.")
    return result


def _password_bytes(value):
    if not isinstance(value, str):
        return b""
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return b""


def _password_material(value):
    password = _password_bytes(value)
    if not 12 <= len(password) <= MAX_PASSWORD_BYTES:
        raise AccountError("Пароль должен содержать от 12 до 1024 байт.")
    salt = secrets.token_bytes(16)
    return salt.hex(), _password_hash(password, salt)


def _password_hash(password, salt):
    return pbkdf2_hmac("sha256", password, salt, PBKDF_ROUNDS).hex()


def _record(row, columns):
    return {column: row[column] for column in columns.split(", ")}


def _insert_account(db, email, name, role, center_id, credentials):
    user = {"id": secrets.token_hex(16), "name": name, "email": email, "role": role, "center_id": center_id}
    inserted = db.execute(
        "INSERT INTO meteo_accounts (id,name,email,role,center_id,salt,password_hash) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (email) DO NOTHING",
        (*user.values(), *credentials),
    )
    if inserted.rowcount != 1:
        raise AccountError("Этот адрес электронной почты уже зарегистрирован.", HTTPStatus.CONFLICT)
    return user


def bootstrap_global_admin(email, name, password):
    """Create the first global administrator from the local management CLI."""
    normalized_email = _email(email)
    normalized_name = _name(name, "Введите имя длиной до 100 символов.")
    credentials = _password_material(password)
    initialize()
    with connection(write=True, global_store=True) as db:
        if db.execute("SELECT 1 FROM meteo_accounts WHERE role=? LIMIT 1", (ROLE_GLOBAL_ADMIN,)).fetchone():
            raise AccountError("Глобальный администратор уже создан.", HTTPStatus.CONFLICT)
        return _insert_account(db, normalized_email, normalized_name, ROLE_GLOBAL_ADMIN, None, credentials)


def _login_slot(email):
    now = monotonic()
    with _attempts_lock:
        while _attempts and _attempts[0][0] <= now - RATE_WINDOW:
            _attempts.popleft()
        if len(_attempts) >= RATE_TOTAL or sum(1 for _, key in _attempts if key == email) >= RATE_EMAIL:
            raise AccountError("Слишком много попыток входа. Попробуйте позже.", HTTPStatus.TOO_MANY_REQUESTS,
                               {"Retry-After": str(RATE_WINDOW)})
        slot = (now, email)
        _attempts.append(slot)
        return slot


def _session_digest(cookie_header):
    if not cookie_header or "," in cookie_header:
        return None
    if sum(part.partition("=")[0].strip() == COOKIE_NAME for part in cookie_header.split(";")) != 1:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except CookieError:
        return None
    item = cookies.get(COOKIE_NAME)
    if item is None or not TOKEN.fullmatch(item.value):
        return None
    return sha256(item.value.encode("ascii")).hexdigest()


def _viewer(db, digest):
    if db is None or digest is None:
        return None
    row = db.execute(
        "SELECT a.id,a.name,a.email,a.role,a.center_id,s.active_center_id "
        "FROM meteo_login_sessions s JOIN meteo_accounts a ON a.id=s.account_id "
        "WHERE s.token_digest=? AND s.expires_at>?", (digest, int(time())),
    ).fetchone()
    if row is None:
        return None
    viewer = _record(row, ACCOUNT_FIELDS)
    viewer["active_center_id"] = row["active_center_id"] if viewer["role"] == ROLE_GLOBAL_ADMIN else viewer["center_id"]
    return viewer


def current_viewer(cookie_header):
    """Read the current role and center from storage, given a browser Cookie header."""
    digest = _session_digest(cookie_header)
    if digest is None:
        return None
    with connection(require="meteo_login_sessions", global_store=True) as db:
        return _viewer(db, digest)


def _cookie(token, secure):
    age = SESSION_SECONDS if token else 0
    attributes = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")
    return f"{COOKIE_NAME}={token}; Max-Age={age}" + attributes


def _center(db, center_id):
    row = db.execute("SELECT id,name FROM meteo_centers WHERE id=?", (center_id,)).fetchone()
    if row is None:
        raise AccountError("Центр не найден.", HTTPStatus.NOT_FOUND)
    return {"id": row["id"], "name": row["name"]}


def _me(db, viewer):
    active = viewer["active_center_id"] if viewer else CENTRAL_CENTER["id"]
    center = _center(db, active) if db is not None else CENTRAL_CENTER.copy()
    centers = [center]
    if viewer and viewer["role"] == ROLE_GLOBAL_ADMIN:
        centers = [{"id": row["id"], "name": row["name"]}
                   for row in db.execute("SELECT id,name FROM meteo_centers ORDER BY name_key,id")]
    return {"user": viewer, "center": center, "centers": centers}


def _login(data, old_digest, secure):
    email = _email(data.get("email"))
    password = _password_bytes(data.get("password"))
    if not password or len(password) > MAX_PASSWORD_BYTES:
        raise AccountError("Введите адрес и пароль.")
    slot = _login_slot(email)
    with connection(require="meteo_accounts", global_store=True) as db:
        row = db.execute(
            "SELECT id,role,center_id,salt,password_hash FROM meteo_accounts WHERE email=?", (email,)
        ).fetchone() if db is not None else None
    salt = bytes.fromhex(row["salt"]) if row is not None else b"\0" * 16
    expected = row["password_hash"] if row is not None else "0" * 64
    if not compare_digest(_password_hash(password, salt), expected) or row is None:
        raise AccountError("Неверный адрес электронной почты или пароль.", HTTPStatus.UNAUTHORIZED)
    with _attempts_lock:
        if slot in _attempts:
            _attempts.remove(slot)
    token = secrets.token_urlsafe(32)
    digest = sha256(token.encode("ascii")).hexdigest()
    now = int(time())
    with connection(write=True, global_store=True) as db:
        db.execute("DELETE FROM meteo_login_sessions WHERE expires_at<=?", (now,))
        if old_digest is not None:
            db.execute("DELETE FROM meteo_login_sessions WHERE token_digest=?", (old_digest,))
        db.execute("INSERT INTO meteo_login_sessions (token_digest,account_id,active_center_id,expires_at) "
                   "VALUES (?,?,?,?)", (digest, row["id"], row["center_id"] or CENTRAL_CENTER["id"], now + SESSION_SECONDS))
        return HTTPStatus.OK, _me(db, _viewer(db, digest)), {"Set-Cookie": _cookie(token, secure)}


def _require(viewer, *roles):
    if not viewer:
        raise AccountError("Войдите в учётную запись.", HTTPStatus.UNAUTHORIZED)
    if roles and viewer["role"] not in roles:
        raise AccountError("Для этого действия недостаточно прав.", HTTPStatus.FORBIDDEN)


def _select_center(db, viewer, digest, data):
    _require(viewer, ROLE_GLOBAL_ADMIN)
    center_id = data.get("center_id")
    if not isinstance(center_id, str) or not IDENTIFIER.fullmatch(center_id):
        raise AccountError("Выберите центр.")
    _center(db, center_id)
    db.execute("UPDATE meteo_login_sessions SET active_center_id=? WHERE token_digest=?", (center_id, digest))
    return _me(db, _viewer(db, digest))


def _create_center(db, viewer, data):
    _require(viewer, ROLE_GLOBAL_ADMIN)
    name = _name(data.get("name"), "Введите название центра длиной до 100 символов.")
    key = "center-" + secrets.token_hex(8)
    result = db.execute("INSERT INTO meteo_centers (id,name,name_key) VALUES (?,?,?) ON CONFLICT (name_key) DO NOTHING",
                        (key, name, name.casefold()))
    if result.rowcount != 1:
        raise AccountError("Центр с таким названием уже существует.", HTTPStatus.CONFLICT)
    return {"center": {"id": key, "name": name}}


def _new_account(db, viewer, data, credentials):
    _require(viewer, ROLE_GLOBAL_ADMIN, ROLE_CENTER_ADMIN)
    proposed_role = data.get("role")
    center_id = data.get("center_id")
    if viewer["role"] == ROLE_CENTER_ADMIN:
        if proposed_role != ROLE_RESEARCHER or center_id not in (None, viewer["center_id"]):
            raise AccountError("Можно добавить исследователя своего центра.", HTTPStatus.FORBIDDEN)
        center_id = viewer["center_id"]
    elif proposed_role != ROLE_CENTER_ADMIN:
        raise AccountError("Можно добавить администратора центра.", HTTPStatus.FORBIDDEN)
    if not isinstance(center_id, str) or not IDENTIFIER.fullmatch(center_id):
        raise AccountError("Выберите центр новой учётной записи.")
    _center(db, center_id)
    email = _email(data.get("email"))
    name = _name(data.get("name"), "Введите имя длиной до 100 символов.")
    return {"user": _insert_account(db, email, name, proposed_role, center_id, credentials)}


def _presentations(raw, charts):
    value = json.loads(raw)
    if not isinstance(value, dict) or not value.keys() <= charts.keys():
        raise ValueError("Неизвестные настройки представления графиков.")
    for settings in value.values():
        if not isinstance(settings, dict) or not settings.keys() <= {"hidden", "zoom"}:
            raise ValueError("Неизвестные настройки представления графиков.")
        hidden = settings.get("hidden", [])
        if (not isinstance(hidden, list) or len(hidden) > 128 or
                any(not isinstance(key, str) or not 1 <= len(key) <= 120 or not key.isprintable() for key in hidden)):
            raise ValueError("Некорректный список скрытых рядов.")
        if "hidden" in settings:
            settings["hidden"] = list(dict.fromkeys(hidden))
        if "zoom" in settings:
            zoom = settings["zoom"]
            if (not isinstance(zoom, list) or len(zoom) != 2 or
                    not all(type(number) in (int, float) and -9e15 <= number <= 9e15 for number in zoom)
                    or zoom[0] >= zoom[1]):
                raise ValueError("Некорректный масштаб графика.")
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _workspace_ids(raw, maximum, options=None):
    values = raw.split(",") if raw else []
    if (len(values) > maximum or not all(WORKSPACE_ID.fullmatch(item) for item in values)
            or (options is not None and any(item not in options for item in values))):
        raise ValueError("Некорректный список станций или графиков.")
    return ",".join(dict.fromkeys(values))


def normalize_saved_state(state):
    """Store one bounded, canonical, same-workspace URL."""
    raw = state.get("workspace") if isinstance(state, dict) and state.keys() == {"workspace"} else None
    if (not isinstance(raw, str) or not raw or len(raw) > MAX_WORKSPACE_BYTES or not raw.isprintable()
            or "\\" in raw or "#" in raw or re.search(r"%(?![0-9a-fA-F]{2})", raw)):
        raise AccountError("Сохранить можно только текущую страницу наблюдений.")
    try:
        if len(raw.encode("utf-8")) > MAX_WORKSPACE_BYTES:
            raise ValueError
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.path != "/observations" or not raw.startswith("/observations"):
            raise ValueError
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=len(WORKSPACE_KEYS), errors="strict")
        query = dict(pairs)
        if len(pairs) != len(query) or not query.keys() <= WORKSPACE_KEYS or any(
                value and not value.isprintable() for value in query.values()):
            raise ValueError
        from observations import CHARTS, GRAINS, MODES, query_intervals
        if (query.get("grain", "day") not in GRAINS or query.get("mode", "modern") not in MODES or
                any(query.get(key, "0") not in {"0", "1"} for key in ("calendar", "coverage"))):
            raise ValueError
        if query.get("coverageParameter") and not WORKSPACE_ID.fullmatch(query["coverageParameter"]):
            raise ValueError
        for key, maximum, options in (("stations", 64, None), ("charts", 64, CHARTS), ("indicators", 64, None)):
            if key in query:
                query[key] = _workspace_ids(query[key], maximum, options)
        if query.keys() & {"start", "end", "intervals"}:
            ranges = query_intervals(query)
            if "intervals" in query:
                query["intervals"] = ",".join(f"{start}:{end}" for start, end in ranges)
            for key, value in (("start", ranges[0][0]), ("end", ranges[-1][1])):
                if key in query:
                    query[key] = value.isoformat()
        if "presentation" in query:
            query["presentation"] = _presentations(query["presentation"], CHARTS)
        search = urlencode(query)
        workspace = "/observations" + ("?" + search if search else "")
        if len(workspace) > MAX_WORKSPACE_BYTES:
            raise ValueError
    except (ValueError, RecursionError):
        raise AccountError("Проверьте параметры страницы наблюдений.") from None
    return {"workspace": workspace}


def _saved_view(db, viewer, query):
    key = query.get("id", "")
    row = db.execute(f"SELECT {VIEW_FIELDS}, state FROM meteo_saved_views "
                     "WHERE account_id=? AND center_id=? AND id=?",
                     (viewer["id"], viewer["active_center_id"], key)).fetchone()
    if row is None:
        raise AccountError("Сохранённый вид не найден.", HTTPStatus.NOT_FOUND)
    return {"session": {**_record(row, VIEW_FIELDS), "state": json.loads(row["state"])}}


def _list_views(db, viewer):
    rows = db.execute(f"SELECT {VIEW_FIELDS} FROM meteo_saved_views "
                      "WHERE account_id=? AND center_id=? ORDER BY created_at DESC,id",
                      (viewer["id"], viewer["active_center_id"]))
    return {"sessions": [_record(row, VIEW_FIELDS) for row in rows]}


def _save_view(db, viewer, data):
    owner = viewer["id"], viewer["active_center_id"]
    name = _name(data.get("name"), "Введите название сохранения длиной до 100 символов.")
    state = normalize_saved_state(data.get("state"))
    count = db.execute("SELECT COUNT(*) FROM meteo_saved_views WHERE account_id=? AND center_id=?", owner).fetchone()[0]
    if count >= MAX_SAVED_VIEWS:
        raise AccountError("Достигнут лимит сохранённых видов для центра.", HTTPStatus.CONFLICT)
    key = secrets.token_hex(16)
    created = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    db.execute("INSERT INTO meteo_saved_views (id,account_id,center_id,name,state,created_at) VALUES (?,?,?,?,?,?)",
               (key, *owner, name, json.dumps(state, ensure_ascii=False), created))
    return {"session": {"id": key, "name": name, "center_id": owner[1], "created_at": created, "state": state}}


def _delete_view(db, viewer, data):
    key = data.get("id")
    if isinstance(key, str) and IDENTIFIER.fullmatch(key):
        removed = db.execute("DELETE FROM meteo_saved_views WHERE id=? AND account_id=? AND center_id=?",
                             (key, viewer["id"], viewer["active_center_id"]))
        if removed.rowcount == 1:
            return {"deleted": True}
    raise AccountError("Сохранённый вид не найден.", HTTPStatus.NOT_FOUND)


def _fields(body):
    try:
        data = json.loads(body) if body else {}
    except (ValueError, RecursionError):
        raise AccountError("Ожидается JSON-объект.") from None
    if not isinstance(data, dict):
        raise AccountError("Ожидается JSON-объект.")
    return data


def _read_account_route(db, route, viewer, query):
    if route == "/api/account/me":
        return _me(db, viewer)
    if route == "/api/account/users":
        _require(viewer, ROLE_GLOBAL_ADMIN, ROLE_CENTER_ADMIN)
        rows = db.execute(f"SELECT {ACCOUNT_FIELDS} FROM meteo_accounts WHERE center_id=? ORDER BY email",
                          (viewer["active_center_id"],))
        return {"users": [_record(row, ACCOUNT_FIELDS) for row in rows]}
    _require(viewer)
    if route == "/api/account/session":
        return _saved_view(db, viewer, query)
    return _list_views(db, viewer)


def handle(method, route, query, body, cookies, *, secure=False):
    """Return (status, JSON body, extra headers) for a route in the account API."""
    allowed = METHODS.get(route)
    if allowed is None:
        return HTTPStatus.NOT_FOUND, {"error": "Маршрут отсутствует."}, {}
    if method not in allowed:
        return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Метод запроса не поддерживается."}, {"Allow": ", ".join(allowed)}
    digest = _session_digest(cookies)
    try:
        if method == "GET":
            with connection(require="meteo_centers", global_store=True) as db:
                return HTTPStatus.OK, _read_account_route(db, route, _viewer(db, digest), query), {}
        data = _fields(body)
        if route == "/api/account/login":
            return _login(data, digest, secure)
        if route == "/api/account/logout":
            with connection(write=digest is not None, require="meteo_centers", global_store=True) as db:
                if digest is not None and db is not None:
                    db.execute("DELETE FROM meteo_login_sessions WHERE token_digest=?", (digest,))
                return HTTPStatus.OK, _me(db, None), {"Set-Cookie": _cookie("", secure)}
        credentials = None
        if route == "/api/account/users":
            _require(current_viewer(cookies), ROLE_GLOBAL_ADMIN, ROLE_CENTER_ADMIN)
            credentials = _password_material(data.get("password"))
        with connection(write=True, global_store=True) as db:
            viewer = _viewer(db, digest)
            if route == "/api/account/select-center":
                return HTTPStatus.OK, _select_center(db, viewer, digest, data), {}
            if route == "/api/account/centers":
                return HTTPStatus.CREATED, _create_center(db, viewer, data), {}
            if route == "/api/account/users":
                return HTTPStatus.CREATED, _new_account(db, viewer, data, credentials), {}
            _require(viewer)
            if route == "/api/account/sessions/delete":
                return HTTPStatus.OK, _delete_view(db, viewer, data), {}
            return HTTPStatus.CREATED, _save_view(db, viewer, data), {}
    except AccountError as error:
        return error.status, {"error": str(error)}, error.headers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    bootstrap = actions.add_parser("bootstrap", help="Создать первого глобального администратора.")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--name", required=True)
    bootstrap.add_argument("--password-stdin", action="store_true", help="Прочитать одну строку пароля из stdin.")
    args = parser.parse_args()
    if not args.password_stdin and not sys.stdin.isatty():
        parser.error("Запустите в терминале или укажите --password-stdin.")
    try:
        if args.password_stdin:
            password = sys.stdin.buffer.readline(MAX_PASSWORD_BYTES + 3).rstrip(b"\r\n").decode("utf-8")
        else:
            password = getpass("Пароль: ")
            if password != getpass("Повторите пароль: "):
                raise AccountError("Пароли не совпадают.")
        bootstrap_global_admin(args.email, args.name, password)
    except (AccountError, UnicodeDecodeError, EOFError) as error:
        sys.exit(str(error))
    print("Глобальный администратор создан.")


if __name__ == "__main__":
    main()
