"""Verify account authorization, browser cookies and personal saved views."""
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import accounts
from observations_db import center_scope, connection


class AccountTest(TestCase):
    password = "synthetic password 123"

    def setUp(self):
        workspace = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "METEO_DATABASE_URL": "", "METEO_OBSERVATION_DB": str(Path(workspace) / "observations.sqlite3"),
        }))
        self.enterContext(patch.object(accounts, "PBKDF_ROUNDS", 100))
        self.enterContext(patch.object(accounts, "_attempts", deque()))
        accounts.initialize()
        self.central = accounts.CENTRAL_CENTER["id"]
        self.admin = accounts.bootstrap_global_admin("root@example.test", "Глобальный администратор", self.password)

    def request(self, method, route, *, cookie="", body=None, query=None, status=200, secure=False):
        reply, payload, headers = accounts.handle(
            method, "/api/account/" + route, query or {},
            json.dumps(body).encode("utf-8") if body is not None else b"", cookie, secure=secure,
        )
        self.assertEqual(reply, status, payload)
        return payload, headers

    def login(self, email, *, cookie="", secure=False):
        payload, headers = self.request(
            "POST", "login", cookie=cookie, body={"email": email, "password": self.password}, secure=secure,
        )
        return payload, headers["Set-Cookie"].split(";")[0], headers["Set-Cookie"]

    def create_center(self, admin, name):
        return self.request("POST", "centers", cookie=admin, body={"name": name}, status=201)[0]["center"]["id"]

    def create_user(self, actor, email, role, center, *, status=201):
        return self.request("POST", "users", cookie=actor, status=status,
                            body={"email": email, "name": "Тестовая учётная запись", "password": self.password,
                                  "role": role, "center_id": center})[0]

    def test_bootstrap_and_login_store_only_digests(self):
        self.assertEqual(self.request("GET", "me")[0], {
            "user": None, "center": accounts.CENTRAL_CENTER, "centers": [accounts.CENTRAL_CENTER],
        })
        with self.assertRaises(accounts.AccountError):
            accounts.bootstrap_global_admin("second@example.test", "Другой", self.password)
        _, cookie, set_cookie = self.login("  ROOT@example.test ", secure=True)
        self.assertIn("HttpOnly; SameSite=Strict; Secure", set_cookie)
        token = cookie.split("=")[1]
        self.assertEqual(len(token), 43)
        with connection(global_store=True) as db:
            session = db.execute("SELECT token_digest FROM meteo_login_sessions").fetchone()
            account = db.execute("SELECT salt,password_hash FROM meteo_accounts").fetchone()
        self.assertEqual(session["token_digest"], sha256(token.encode("ascii")).hexdigest())
        self.assertNotEqual(session["token_digest"], token)
        self.assertEqual(len(account["salt"]), 32)
        self.assertNotEqual(account["password_hash"], self.password)
        self.assertEqual(accounts.current_viewer(cookie)["role"], "global_admin")
        self.assertIsNone(accounts.current_viewer(cookie + "; " + cookie))
        self.assertIsNone(accounts.current_viewer(cookie + ", " + cookie))

    def test_login_replaces_cookie_logout_revokes_and_expiry_is_cleaned(self):
        _, first, _ = self.login(self.admin["email"])
        _, second, _ = self.login(self.admin["email"], cookie=first)
        self.assertIsNone(accounts.current_viewer(first))
        self.assertIsNotNone(accounts.current_viewer(second))
        payload, headers = self.request("POST", "logout", body={}, cookie=second)
        self.assertIsNone(payload["user"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIsNone(accounts.current_viewer(second))
        with patch.object(accounts, "time", return_value=100):
            _, expired, _ = self.login(self.admin["email"])
        with patch.object(accounts, "time", return_value=100 + accounts.SESSION_SECONDS):
            self.assertIsNone(accounts.current_viewer(expired))
            _, valid, _ = self.login(self.admin["email"])
            self.assertIsNotNone(accounts.current_viewer(valid))
        with connection(global_store=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM meteo_login_sessions").fetchone()[0], 1)

    def test_login_failure_and_throttling(self):
        invalid = {"email": self.admin["email"], "password": "wrong"}
        with patch.object(accounts, "monotonic", return_value=100), patch.object(accounts, "RATE_EMAIL", 2):
            nonexisting = {"email": "no-account@example.test", "password": "wrong"}
            self.assertEqual(self.request("POST", "login", body=invalid, status=401)[0],
                             self.request("POST", "login", body=nonexisting, status=401)[0])
            self.request("POST", "login", body=invalid, status=401)
            self.assertIn("Retry-After", self.request("POST", "login", body=invalid, status=429)[1])
        with patch.object(accounts, "monotonic", return_value=100 + accounts.RATE_WINDOW):
            self.login(self.admin["email"])

    def test_roles_and_global_storage_inside_center_scope(self):
        _, admin, _ = self.login(self.admin["email"])
        outside = self.create_center(admin, "Второй центр")
        self.create_user(admin, "local@example.test", "center_admin", self.central)
        self.create_user(admin, "other@example.test", "center_admin", outside)
        self.create_user(admin, "researcher@example.test", "researcher", self.central, status=403)
        self.create_user(admin, "another-root@example.test", "global_admin", None, status=403)
        self.create_user(admin, "LOCAL@example.test", "center_admin", self.central, status=409)
        _, local, _ = self.login("local@example.test")
        self.create_user(local, "cross@example.test", "researcher", outside, status=403)
        self.create_user(local, "new-admin@example.test", "center_admin", self.central, status=403)
        self.create_user(local, "researcher@example.test", "researcher", self.central)
        self.request("POST", "centers", cookie=local, body={"name": "Третий центр"}, status=403)
        self.request("POST", "select-center", cookie=local, body={"center_id": outside}, status=403)
        _, researcher, _ = self.login("researcher@example.test")
        self.request("GET", "users", cookie=researcher, status=403)
        self.request("GET", "users", status=401)
        self.create_user(researcher, "alien@example.test", "researcher", self.central, status=403)
        current = self.request("GET", "me", cookie=researcher)[0]
        self.assertEqual([center["id"] for center in current["centers"]], [self.central])
        active = self.request("POST", "select-center", cookie=admin, body={"center_id": outside})[0]
        self.assertEqual((active["center"]["id"], active["user"]["active_center_id"]), (outside, outside))
        self.assertIsNone(active["user"]["center_id"])
        self.assertEqual([user["email"] for user in self.request("GET", "users", cookie=admin)[0]["users"]],
                         ["other@example.test"])
        with center_scope(outside):
            self.assertEqual(accounts.current_viewer(local)["center_id"], self.central)
            self.assertEqual(accounts.current_viewer(admin)["active_center_id"], outside)
            with connection(write=True) as db:
                self.assertFalse(db.table_exists("meteo_accounts"))
        with connection(write=True, global_store=True) as db:
            db.execute("UPDATE meteo_accounts SET role=? WHERE email=?", ("researcher", "local@example.test"))
        self.assertEqual(accounts.current_viewer(local)["role"], "researcher")
        self.request("GET", "users", cookie=local, status=403)

    def test_personal_views_are_private_and_center_scoped(self):
        _, admin, _ = self.login(self.admin["email"])
        outside = self.create_center(admin, "Второй центр")
        self.create_user(admin, "local@example.test", "center_admin", self.central)
        _, local, _ = self.login("local@example.test")
        state = {"workspace": "/observations?stations=kr1&charts=air&start=2025-01-01&end=2025-01-20"}
        payload = {"name": "  Январский вид  ", "state": state}
        view = self.request("POST", "sessions", cookie=admin, body=payload, status=201)[0]["session"]
        self.assertEqual((view["center_id"], view["name"], view["state"]), (self.central, "Январский вид", state))
        query = {"id": view["id"]}
        self.assertEqual(self.request("GET", "session", cookie=admin, query=query)[0]["session"], view)
        self.request("GET", "session", cookie=local, query=query, status=404)
        self.assertEqual(self.request("GET", "sessions", cookie=local)[0]["sessions"], [])
        self.request("GET", "sessions", status=401)
        self.request("POST", "select-center", cookie=admin, body={"center_id": outside})
        self.request("GET", "session", cookie=admin, query=query, status=404)
        self.assertEqual(self.request("GET", "sessions", cookie=admin)[0]["sessions"], [])
        self.request("POST", "select-center", cookie=admin, body={"center_id": self.central})
        summary = self.request("GET", "sessions", cookie=admin)[0]["sessions"][0]
        self.assertNotIn("state", summary)
        self.assertEqual(summary["id"], view["id"])

    def test_delete_view_is_private_and_reclaims_capacity(self):
        _, admin, _ = self.login(self.admin["email"])
        outside = self.create_center(admin, "Другой центр")
        self.create_user(admin, "local@example.test", "center_admin", self.central)
        _, local, _ = self.login("local@example.test")
        payload = {"name": "Рабочий вид", "state": {"workspace": "/observations"}}
        with patch.object(accounts, "MAX_SAVED_VIEWS", 1):
            view = self.request("POST", "sessions", cookie=admin, body=payload, status=201)[0]["session"]
            self.request("POST", "sessions", cookie=admin, body=payload, status=409)
            selected = {"id": view["id"]}
            route = "sessions/delete"
            missing = self.request("POST", route, cookie=admin, body={"id": "0" * 32}, status=404)[0]
            self.assertEqual(self.request("POST", route, cookie=local, body=selected, status=404)[0], missing)
            self.request("POST", route, body=selected, status=401)
            self.request("POST", "select-center", cookie=admin, body={"center_id": outside})
            self.assertEqual(self.request("POST", route, cookie=admin, body=selected, status=404)[0], missing)
            self.request("POST", "select-center", cookie=admin, body={"center_id": self.central})
            self.assertEqual(self.request("POST", route, cookie=admin, body=selected)[0], {"deleted": True})
            self.assertEqual(self.request("POST", route, cookie=admin, body=selected, status=404)[0], missing)
            self.assertEqual(self.request("GET", "sessions", cookie=admin)[0]["sessions"], [])
            self.request("POST", "sessions", cookie=admin, body=payload, status=201)

    def test_workspace_state_is_normalized_and_limited(self):
        state = {"workspace": "/observations?" + urlencode({
            "stations": "kr1,kr1,kr2", "charts": "air,pressure", "coverage": "1",
            "coverageParameter": "t_air", "indicators": "temp-events",
            "intervals": "2025-06-01:2025-06-11,2025-01-01:2025-01-20",
            "presentation": json.dumps({"air": {"hidden": ["kr1", "kr1"], "zoom": [2, 3]}}),
        })}
        query = parse_qs(urlsplit(accounts.normalize_saved_state(state)["workspace"]).query)
        self.assertEqual(query["stations"], ["kr1,kr2"])
        self.assertEqual(query["intervals"], ["2025-01-01:2025-01-20,2025-06-01:2025-06-11"])
        self.assertEqual(json.loads(query["presentation"][0]), {"air": {"hidden": ["kr1"], "zoom": [2, 3]}})
        for url in ("https://example.test/observations", "//example.test/observations",
                    "/observations/../files", "/observations#redirect", "/observations?stations=kr1%0d%0a",
                    "/observations?stations=kr1&stations=kr2", "/observations?center=outside",
                    "/observations?stations=" + "x" * accounts.MAX_WORKSPACE_BYTES):
            with self.subTest(url=url[:50]), self.assertRaises(accounts.AccountError):
                accounts.normalize_saved_state({"workspace": url})
