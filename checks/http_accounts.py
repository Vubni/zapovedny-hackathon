"""Real accounts and HTTP sessions for disposable integration test databases."""

from dataclasses import dataclass, field
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
CENTRAL = "central-forest"


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str = field(repr=False)

    def login_payload(self):
        return {"email": self.email, "password": self.password}


def isolated_database(path):
    return {
        "METEO_DATABASE_URL": "",
        "METEO_OBSERVATION_DB": str(Path(path).resolve()),
        "METEO_PUBLIC_ORIGIN": "",
    }


def bootstrap_admin(config):
    admin = Credentials("qa-global@example.test", token_urlsafe(24))
    env = dict(os.environ, **config)
    result = subprocess.run(
        [sys.executable, "accounts.py", "bootstrap", "--email", admin.email,
         "--name", "Интеграционный администратор", "--password-stdin"],
        cwd=ROOT, env=env, input=admin.password + "\n", text=True,
        capture_output=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError(f"Bootstrap тестового администратора завершился с кодом {result.returncode}.")
    return admin


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpClient:
    def __init__(self, base):
        self.base = base
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect())

    def request(self, path, *, query=None, payload=None, body=None, mime=None, headers=None, method=None):
        url = self.base + path + ("?" + urlencode(query) if query else "")
        headers = dict(headers or {})
        if payload is not None:
            body, mime = json.dumps(payload).encode(), "application/json"
        if mime is not None:
            headers["Content-Type"] = mime
        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def login(self, credentials):
        status, _, body = self.request("/api/account/login", payload=credentials.login_payload())
        if status != 200 or not list(self.cookies):
            raise AssertionError(f"Вход тестовой учётной записи завершился HTTP {status}.")
        return json.loads(body)


def create_account(client, role, *, center_id=CENTRAL):
    credentials = Credentials(f"qa-{role.replace('_', '-')}@example.test", token_urlsafe(24))
    status, _, _ = client.request("/api/account/users", payload={
        **credentials.login_payload(), "name": "Тестовая учётная запись", "role": role, "center_id": center_id,
    })
    if status != 201:
        raise AssertionError(f"Создание тестовой роли {role} завершилось HTTP {status}.")
    return credentials
