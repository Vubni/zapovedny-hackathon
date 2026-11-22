"""Exercise the common routes through a standard WSGI request."""
import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

from admin_auth import OperatorAuth
from review_upload import MAX_BYTES
from serve_container import make_application


class ContainerHTTPTest(TestCase):
    port = 8766
    host = "localhost:8766"
    operator_password = "пароль для проверки входа"

    def setUp(self):
        self.app = validator(make_application(OperatorAuth(self.operator_password), self.port))

    def request(self, method, target, body=None, **headers):
        target = urlsplit(target)
        environ = dict(REQUEST_METHOD=method, SCRIPT_NAME="", PATH_INFO=target.path, QUERY_STRING=target.query,
                       HTTP_HOST=self.host, **{"wsgi.input": BytesIO(body or b"")})
        if body is not None:
            environ.update(CONTENT_LENGTH=str(len(body)), CONTENT_TYPE="application/json; charset=utf-8")
        environ.update(headers)
        setup_testing_defaults(environ)
        result = {}

        def start_response(status, response_headers, exc_info=None):
            result.update(status=int(status.split()[0]), headers=dict(response_headers))

        response = self.app(environ, start_response)
        try:
            data = b"".join(response)
        finally:
            response.close()
        self.assertEqual(result["headers"]["Content-Length"], str(len(data)))
        self.assertEqual(result["headers"]["X-Content-Type-Options"], "nosniff")
        return result["status"], result["headers"], data

    def login(self, **headers):
        payload = json.dumps({"password": self.operator_password}, ensure_ascii=False).encode("utf-8")
        return self.request("POST", "/api/operator/login", payload, **headers)

    def test_loopback_operator_session_and_binary_download(self):
        redirect, headers, _ = self.request("GET", "/files")
        self.assertEqual((redirect, headers.get("Location")), (303, "/operator"))
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, headers, body = self.login(HTTP_ORIGIN="http://" + self.host)
        self.assertEqual((status, json.loads(body)), (200, {"authenticated": True}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        status, _, body = self.request("GET", "/api/operator/session", HTTP_COOKIE=cookie)
        self.assertEqual((status, json.loads(body)), (200, {"authenticated": True}))

        contents = b"\x00\x93\xff\r\n"
        with patch("observation_store.original_file", return_value=("полевые.csv", "application/octet-stream", contents)):
            status, headers, body = self.request("GET", "/api/files/original", HTTP_COOKIE=cookie)
        self.assertEqual((status, body), (200, contents))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("filename*=UTF-8''%D0%BF", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, _, _ = self.request("POST", "/api/operator/logout", b"",
                                    HTTP_COOKIE=cookie, HTTP_ORIGIN="http://" + self.host)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/files", HTTP_COOKIE=cookie)[0], 401)

    def test_host_origin_and_ambiguous_headers(self):
        origin = "https://meteo.example:8443"
        self.app = validator(make_application(OperatorAuth(self.operator_password, origin), self.port))
        host = "meteo.example:8443"
        self.assertEqual(self.login(HTTP_HOST="meteo.example", HTTP_ORIGIN=origin)[0], 403)
        self.assertEqual(self.login(HTTP_HOST="foreign.example", HTTP_ORIGIN=origin,
                                    HTTP_X_FORWARDED_HOST=host, HTTP_X_FORWARDED_PROTO="https")[0], 403)
        self.assertEqual(self.login(HTTP_HOST=host + ", " + host, HTTP_ORIGIN=origin)[0], 403)
        self.assertEqual(self.login(HTTP_HOST=host, HTTP_ORIGIN=origin + ", " + origin)[0], 403)
        self.assertEqual(self.login(HTTP_HOST=host, HTTP_ORIGIN="")[0], 403)
        self.assertEqual(self.login(HTTP_HOST=host, HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE="")[0], 403)
        status, headers, _ = self.login(HTTP_HOST=host, HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE="same-origin")
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request("GET", "/api/files", HTTP_HOST=host, HTTP_COOKIE=cookie + ", " + cookie)[0], 401)
        self.assertEqual(self.request("GET", "/api/files", HTTP_HOST=host, HTTP_COOKIE=cookie + "; " + cookie)[0], 401)

    def test_methods_and_application_body_limits(self):
        status, headers, _ = self.request("GET", "/api/operator/login")
        self.assertEqual((status, headers["Allow"]), (405, "POST"))
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("POST", "/api/import", b"test")[0], 400)
        self.assertEqual(self.request("POST", "/api/import", b"test", CONTENT_TYPE="application/octet-stream")[0], 415)
        self.assertEqual(self.request("POST", "/api/review")[0], 411)
        self.assertEqual(self.request("POST", "/api/review", b"", CONTENT_LENGTH=str(MAX_BYTES + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/review", b"", CONTENT_LENGTH="10")[0], 400)

    def test_health_probes_both_stores_and_hides_internal_errors(self):
        with patch("observations_db.check_ready") as mutable, patch("server.database") as archive:
            status, headers, body = self.request("GET", "/healthz")
            self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
            self.assertEqual(headers["Cache-Control"], "no-store")
            mutable.assert_called_once_with()
            archive.return_value.execute.assert_called_once_with("SELECT 1 FROM daily LIMIT 1")

            archive.return_value.execute.return_value.fetchone.return_value = None
            self.assertEqual(self.request("GET", "/healthz")[0], 503)

            mutable.side_effect = RuntimeError("postgresql://opaque-test-value@localhost")
            status, headers, body = self.request("GET", "/healthz")
            self.assertEqual((status, json.loads(body)), (503, {"status": "unavailable"}))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn(b"postgresql", body)
