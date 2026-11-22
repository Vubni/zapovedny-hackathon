"""Verify the import page and uploads through the local HTTP handler."""

import csv
import io
import json
import math
import os
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from build_data import sha256
from checks.http_accounts import HttpClient, bootstrap_admin, isolated_database
from server import ARCHIVE, DATABASE, IMPORT_EXAMPLE, Handler, MAX_UPLOAD, ROOT


class QuietHandler(Handler):
    def log_message(self, *_):
        pass


def post(client, body, mime="application/json", length=None, status=200, origin=None, **options):
    query = {"station": "kr1", "year": 2024, "param": "pressure_hpa", "grain": "day", **options}
    headers = {"Origin": origin} if origin is not None else {}
    if length is not None:
        headers["Content-Length"] = str(length)
    actual, headers, payload = client.request("/api/import", query=query, body=body, mime=mime,
                                              headers=headers, method="POST")
    assert actual == status, (actual, payload.decode("utf-8"))
    assert headers["Cache-Control"] == "no-store", headers
    return json.loads(payload)


def get(client, path, **params):
    status, headers, payload = client.request(path, query=params)
    assert status == 200, (status, payload)
    return headers, payload


def verify(admin, guest):
    for address in ("/import", "/import.html"):
        status, headers, _ = guest.request(address)
        assert (status, headers.get("Location")) == (303, "/files?source=upload")
    _, document = get(guest, headers["Location"])
    assert b'/files-page.js' in document
    _, script = get(guest, "/files-page.js")
    assert b'filesUrl("upload"' in script

    headers, sample = get(guest, "/api/import/example")
    assert sample == IMPORT_EXAMPLE.read_bytes()
    assert IMPORT_EXAMPLE.name in headers["Content-Disposition"]

    post(guest, sample, status=401)
    post(admin, sample, origin="https://foreign.invalid", status=403)
    preview = post(admin, sample)
    rows = json.loads(sample)
    usable = [r for r in rows if "pressure_hpa:range" not in r["qc_flag"].split(";") and r["pressure_hpa"]]
    expected = math.fsum(float(r["pressure_hpa"]) for r in usable) / len(usable)
    first, = preview["rows"]
    assert preview["raw_rows"] == len(rows) == 48
    assert (first["slots"], first["n"], first["flagged"], first["valid"]) == (19, 38, 10, True)
    assert math.isclose(first["mean"], expected)
    assert preview["filename"].endswith("_import.csv") and preview["csv"].startswith("\ufeff")
    imported_csv = list(csv.DictReader(io.StringIO(preview["csv"].lstrip("\ufeff")), delimiter=";"))
    assert len(imported_csv) == 1
    assert imported_csv[0]["Среднее"] == "1005,45"

    fragment, = post(admin, sample, grain="month")["rows"]
    assert not fragment["valid"] and fragment["mean"] is None and "фрагмент" in fragment["reason"]
    whole_month, = post(admin, sample, grain="month", start="2024-04-01", end="2024-04-30")["rows"]
    assert (whole_month["valid_days"], whole_month["longest_gap"], whole_month["valid"]) == (1, 29, False)

    member = "meteo_clgz_hackathon/data/sokol_m1/kr1_2024.csv"
    with zipfile.ZipFile(ARCHIVE) as archive:
        official_csv = archive.read(member)
    imported = post(admin, official_csv, mime="text/csv", grain="month", start="2024-01-01", end="2024-12-31")
    _, reference_payload = get(guest, "/api/series", station="kr1", param="pressure_hpa",
                               grain="month", start="2024-01-01", end="2024-12-31")
    reference = json.loads(reference_payload)
    keys = ("period", "mean", "min", "max", "valid", "valid_days", "days", "longest_gap", "n",
            "flagged", "missing", "conflicts")
    assert len(imported["rows"]) == len(reference["rows"]) == 12
    for derived, known in zip(imported["rows"], reference["rows"]):
        assert [derived[k] for k in keys] == [known[k] for k in keys], (derived, known)

    future = [{"datetime": f"2026-02-01 {hour:02d}:00", "t_air": hour, "qc_flag": ""} for hour in range(18)]
    remote_year = post(admin, json.dumps(future).encode(), year=2026, param="t_air")
    future_day, = remote_year["rows"]
    assert (remote_year["year"], future_day["period"], future_day["mean"], future_day["valid"]) == (
        2026, "2026-02-01", 8.5, True
    )
    broken_json = json.dumps([future[0], {**future[1], "datetime": "2026-02-30 01:00"}]).encode()
    bad_date = post(admin, broken_json, year=2026, param="t_air", status=400)["error"]
    assert "элемент 2" in bad_date and "2026-02-30 01:00" in bad_date
    bad_csv = post(admin, b"datetime,t_air,qc_flag\n2026-02-01 00:00,70,\n", mime="text/csv",
                   year=2026, param="t_air", status=400)["error"]
    assert "CSV:2" in bad_csv and "t_air=70" in bad_csv
    bad_date_range = post(admin, sample, start="2025-04-01", status=400)["error"]
    assert "2024" in bad_date_range
    large = post(admin, b"", length=MAX_UPLOAD + 1, status=413)["error"]
    bad_mime = post(admin, sample, mime="text/plain", status=415)["error"]

    return {
        "page": "/files?source=upload",
        "example": {"file": IMPORT_EXAMPLE.name, "source_rows": preview["raw_rows"], **first},
        "csv": {"name": preview["filename"], "mean_cell": imported_csv[0]["Среднее"]},
        "official_csv": {"member": member, "matched_months": len(imported["rows"]),
                         "valid_months": imported["valid_periods"], "april": imported["rows"][3]},
        "clipped_month": fragment,
        "sparse_whole_month": whole_month,
        "future_year": future_day,
        "rejected": {"invalid_json_item": bad_date, "invalid_csv_line": bad_csv,
                     "wrong_date_year": bad_date_range, "upload_above_cap": large, "wrong_mime": bad_mime},
    }


def main():
    paths = (ARCHIVE, DATABASE)
    before = {path.relative_to(ROOT).as_posix(): sha256(path) for path in paths}
    with TemporaryDirectory(prefix="meteo-import-http-") as directory:
        config = isolated_database(Path(directory) / "observations.sqlite3")
        credentials = bootstrap_admin(config)
        with patch.dict(os.environ, config), ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                admin = HttpClient(base)
                admin.login(credentials)
                report = verify(admin, HttpClient(base))
            finally:
                server.shutdown()
                worker.join()
    after = {path.relative_to(ROOT).as_posix(): sha256(path) for path in paths}
    assert before == after
    report["archive_and_database_sha256_unchanged"] = after
    output = Path(ROOT) / "results" / "import-http-checks.json"
    with TemporaryDirectory(prefix=".import-http-checks-", dir=output.parent) as directory:
        staged = Path(directory) / output.name
        staged.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        staged.replace(output)
    print(f"Проверены страница импорта, JSON и CSV через HTTP. Результаты: {output}")


if __name__ == "__main__":
    main()
