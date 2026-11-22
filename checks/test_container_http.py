"""Exercise the common routes through a standard WSGI request."""
import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

from request_origin import parse_origin
from review_upload import MAX_BYTES
from serve_container import make_application


class ContainerHTTPTest(TestCase):
    port = 8766
    host = "localhost:8766"

    def setUp(self):
        self.app = validator(make_application(self.port))

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

    def test_public_files_and_legacy_redirects(self):
        self.assertEqual(self.request("GET", "/files")[0], 200)
        for legacy in ("/review", "/operator"):
            status, headers, _ = self.request("GET", legacy + "?file=abc&label=%D0%AF")
            self.assertEqual((status, headers.get("Location")), (303, "/files?file=abc&label=%D0%AF"))
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/operator.js")[0], 404)
        self.assertEqual(self.request("GET", "/api/operator/session")[0], 404)
        self.assertEqual(self.request("GET", "/api/map-config")[0], 404)
        self.assertEqual(self.request("GET", "/yandex-location.js")[0], 404)
        for route in ("/model", "/model.html"):
            status, headers, _ = self.request("GET", route)
            self.assertEqual((status, headers.get("Location")), (303, "/forecast"))
        for route in ("/api/model", "/api/model/catalog", "/api/model/stations", "/model.js", "/model.css"):
            self.assertEqual(self.request("GET", route)[0], 404)

        contents = b"\x00\x93\xff\r\n"
        with patch("observation_store.original_file", return_value=("полевые.csv", "application/octet-stream", contents)):
            status, headers, body = self.request("GET", "/api/files/original")
        self.assertEqual((status, body), (200, contents))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("filename*=UTF-8''%D0%BF", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_host_origin_and_ambiguous_headers(self):
        def write(**headers):
            return self.request("POST", "/api/stations", b"{}", **headers)[0]

        with patch("server.register_station", return_value={"station": "fixture"}):
            origin = "http://" + self.host
            self.assertEqual(write(), 201)
            self.assertEqual(write(HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE="same-origin"), 201)
            self.assertEqual(write(HTTP_ORIGIN="https://foreign.example"), 403)
            self.assertEqual(write(HTTP_ORIGIN="https://" + self.host), 403)
            self.assertEqual(write(HTTP_ORIGIN="null"), 403)
            self.assertEqual(write(HTTP_ORIGIN=""), 403)
            self.assertEqual(write(HTTP_SEC_FETCH_SITE="cross-site"), 403)
            self.assertEqual(write(HTTP_HOST="foreign.example", HTTP_ORIGIN="http://foreign.example"), 403)

            origin = "https://meteo.example:8443"
            self.app = validator(make_application(self.port, parse_origin(origin)))
            host = "meteo.example:8443"
            self.assertEqual(write(HTTP_HOST="meteo.example", HTTP_ORIGIN=origin), 403)
            self.assertEqual(write(HTTP_HOST="foreign.example", HTTP_ORIGIN=origin,
                                   HTTP_X_FORWARDED_HOST=host, HTTP_X_FORWARDED_PROTO="https"), 403)
            self.assertEqual(write(HTTP_HOST=host + ", " + host, HTTP_ORIGIN=origin), 403)
            self.assertEqual(write(HTTP_HOST=host, HTTP_ORIGIN=origin + ", " + origin), 403)
            self.assertEqual(write(HTTP_HOST=host, HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE=""), 403)
            self.assertEqual(write(HTTP_HOST=host, HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE="same-origin"), 201)
            self.assertEqual(write(HTTP_HOST=host), 201)

    def test_methods_and_application_body_limits(self):
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("POST", "/api/operator/login", b"{}")[0], 404)
        self.assertEqual(self.request("POST", "/api/import", b"test")[0], 400)
        self.assertEqual(self.request("POST", "/api/import", b"test", CONTENT_TYPE="application/octet-stream")[0], 415)
        self.assertEqual(self.request("POST", "/api/review")[0], 411)
        self.assertEqual(self.request("POST", "/api/review", b"", CONTENT_LENGTH=str(MAX_BYTES + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/review", b"", CONTENT_LENGTH="10")[0], 400)

    def test_new_static_assets_have_browser_mime_types(self):
        for route in ("/chart-report.js", "/station-map-raster.js"):
            status, headers, _ = self.request("GET", route)
            self.assertEqual(status, 200)
            self.assertIn("javascript", headers["Content-Type"])
        for route in ("/reserve-imagery.webp", "/russia-relief.webp"):
            status, headers, body = self.request("GET", route)
            self.assertEqual((status, headers["Content-Type"]), (200, "image/webp"))
            self.assertEqual((body[:4], body[8:12]), (b"RIFF", b"WEBP"))

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
