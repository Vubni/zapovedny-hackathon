"""Verify the saved provider responses, working copy, and failure paths offline."""

import copy
import hashlib
import io
import json
import unittest
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from zoneinfo import ZoneInfo

import external_fill as fill
from review_filter import review


QUERY = {"fill": "1", "timezone": "Europe/Moscow"}


def report(day="2024-04-21", param="t_air", rows=()):
    return {"station": "kr1", "param": param, "start": day, "end": day, "rows": list(rows)}


def observation(stamp, line, value=None, keep=False, conflict=False):
    return {"time": stamp, "line": line, "source": "station.csv", "original": {"t_air": value},
            "value": value, "keep": keep, "official_keep": keep, "reasons": [], "conflict": conflict}


class ExternalFill(unittest.TestCase):
    def test_snapshot_identity_units_and_counts(self):
        provenance = json.loads((fill.FIXTURES / "provenance.json").read_bytes())
        for info in provenance["models"]:
            raw = (fill.FIXTURES / info["file"]).read_bytes()
            model = fill.MODELS[info["id"]]
            self.assertEqual(info["sha256"], hashlib.sha256(raw).hexdigest())
            start, end = fill.SNAPSHOT_START_UTC, fill.SNAPSHOT_END_UTC
            self.assertEqual(info["url"], model.url(start, end, fill.ARCHIVE_POINT))
            self.assertEqual(info["retrieved_at"], model.retrieved_at)
            _, series = fill._parse_model(model, json.loads(raw), start, end, fill.ARCHIVE_POINT)
            for param in fill.UNITS:
                self.assertEqual(len(series[param]), 768)
                self.assertEqual(info["count_by_parameter"][param], 768)

    def test_hourly_offline_estimate_provenance(self):
        response = fill.apply_estimates(report(), QUERY)
        row = response["rows"][11]
        self.assertEqual(row["time"], "2024-04-21 11:00")
        self.assertEqual(row["estimate"], 9.0)
        self.assertTrue(row["synthetic"])
        self.assertIsNone(row["line"])
        self.assertFalse(row["official_keep"])
        self.assertEqual(response["external"]["estimated_count"], 24)
        self.assertEqual(row["provenance"]["start_utc"], "2024-04-21T08:00:00+00:00")
        self.assertEqual(row["provenance"]["end_utc"], "2024-04-21T09:00:00+00:00")
        self.assertEqual(row["provenance"]["requested_coordinates"], {"latitude": 56.52, "longitude": 32.87})
        era, merra = row["provenance"]["sources"]
        self.assertEqual([v["value"] for v in era["samples"]], [10.3, 9.3])
        self.assertEqual(era["value"], 9.8)
        self.assertEqual(merra["value"], 8.2)
        self.assertEqual(era["coordinates"], {"latitude": 56.5, "longitude": 32.75, "elevation_m": 250.0})
        utc = fill.apply_estimates(report(), dict(QUERY, timezone="UTC"))
        self.assertEqual(utc["rows"][8]["estimate"], row["estimate"])

    def test_registered_minute_exclusion_can_be_interpolated_without_changing_review(self):
        query = dict(QUERY, station="kr1", param="t_air", start="2024-04-15", end="2024-04-15", filters="hampel")
        unfilled = review(query)
        before = copy.deepcopy(unfilled)
        skip = fill.apply_estimates(copy.deepcopy(unfilled), query)
        response = fill.apply_estimates(unfilled, dict(query, subhour="interpolate"))
        self.assertEqual(response["counts"], before["counts"])
        self.assertEqual(response["summaries"], before["summaries"])
        rows = {row["line"]: row for row in response["rows"] if not row["synthetic"]}
        for original in before["rows"]:
            self.assertEqual({key: rows[original["line"]][key] for key in original}, original)
        row = rows[5035]
        self.assertFalse(row["keep"])
        self.assertTrue(row["official_keep"])
        self.assertEqual(row["value"], 8.4)
        self.assertEqual(row["estimate"], 5.97)
        self.assertEqual(row["provenance"]["time_utc"], "2024-04-15T12:32:00+00:00")
        self.assertEqual(row["provenance"]["kind"], "subhour_reanalysis")
        era, merra = row["provenance"]["sources"]
        self.assertEqual(era["samples"][0]["anchor_utc"], "2024-04-15T12:00:00+00:00")
        self.assertEqual(merra["samples"][0]["anchor_utc"], "2024-04-15T12:30:00+00:00")
        self.assertEqual(merra["samples"][1]["anchor_utc"], "2024-04-15T13:30:00+00:00")
        self.assertEqual(next(r for r in skip["rows"] if r["line"] == 5035)["estimate_reason"], "subhourly")

    def test_synthetic_grid_does_not_duplicate_hours_or_conflicts(self):
        source = [observation("2024-04-21 00:03", 1), observation("2024-04-21 01:00", 2),
                  observation("2024-04-21 01:00", 3), observation("2024-04-21 02:00", 4, conflict=True),
                  observation("2024-04-21 03:00", 5, keep=True, value=10.0)]
        result = fill.apply_estimates(report(rows=source), dict(QUERY, subhour="interpolate"))
        by_line = {row["line"]: row for row in result["rows"] if not row["synthetic"]}
        self.assertNotIn("2024-04-21 00:00", {row["time"] for row in result["rows"]})
        self.assertEqual(result["external"]["synthetic_count"], 20)
        self.assertEqual(by_line[1]["estimate_status"], "estimated")
        self.assertEqual({by_line[line]["estimate_reason"] for line in (2, 3, 4)}, {"duplicate_term"})
        self.assertEqual(by_line[5]["estimate_status"], "not_needed")
        self.assertEqual(len(fill.apply_estimates(result, QUERY)["rows"]), 25)

    def test_units_and_pressure_reference(self):
        pressure = fill.apply_estimates(report(param="pressure_hpa"), QUERY)
        self.assertEqual(pressure["external"]["reason"], "unknown_pressure_reference")
        self.assertTrue(all(row["estimate"] is None for row in pressure["rows"]))
        pressure = fill.apply_estimates(report(param="pressure_hpa"), dict(QUERY, pressure_reference="surface"))
        self.assertEqual(pressure["rows"][11]["estimate"], 991.48)
        merra = pressure["rows"][11]["provenance"]["sources"][1]
        self.assertEqual(merra["samples"][0]["value"], 99.25)
        self.assertEqual(merra["source_unit"], "kPa")
        self.assertEqual(merra["value"], 992.5)
        rh = fill.apply_estimates(report(param="rh"), QUERY)
        self.assertEqual(rh["rows"][11]["estimate"], 68.52)

    def test_one_source_or_missing_anchor_does_not_produce_estimate(self):
        response = fill.apply_estimates(report(), dict(QUERY, sources="era5,era5"))
        self.assertEqual(response["external"]["estimated_count"], 0)
        self.assertEqual(response["rows"][0]["provenance"]["source_count"], 1)
        model = fill.MODELS["merra2"]
        data = fill._load_model(model, fill.SNAPSHOT_START_UTC, fill.SNAPSHOT_END_UTC, True, fill.ARCHIVE_POINT)
        first_minute = datetime(2024, 3, 31, 0, 1, tzinfo=fill.UTC)
        self.assertIsNone(fill._sample(data, "t_air", first_minute))
        exact_anchor = first_minute.replace(minute=30)
        self.assertEqual(len(fill._sample(data, "t_air", exact_anchor)["samples"]), 1)
        last_minute = datetime(2024, 4, 30, 23, 59, tzinfo=fill.UTC)
        self.assertEqual(len(fill._sample(data, "t_air", last_minute)["samples"]), 2)

    def test_timezone_window_and_input_errors(self):
        zone = ZoneInfo("Europe/Moscow")
        self.assertIsNone(fill._to_utc(datetime(2010, 3, 28, 2), zone))
        self.assertIsNone(fill._to_utc(datetime(2010, 10, 31, 2), zone))
        with self.assertRaises(ValueError):
            fill.apply_estimates(report(), {"fill": "1"})
        with self.assertRaises(ValueError):
            fill.apply_estimates(report(), dict(QUERY, subhour="unsupported"))
        with self.assertRaises(ValueError):
            fill.apply_estimates(dict(report(), end="2024-06-01"), QUERY)
        today = datetime.now(fill.UTC).date().isoformat()
        with patch.object(fill, "_load_sources") as loader:
            result = fill.apply_estimates(report(today), dict(QUERY, timezone="UTC"))
        loader.assert_not_called()
        self.assertEqual({row["estimate_reason"] for row in result["rows"]}, {"incomplete_day"})

    def test_network_request_and_single_provider_outage(self):
        def serve(url, timeout):
            parts = urlparse(url)
            params = parse_qs(parts.query)
            self.assertEqual(timeout, 6)
            if parts.netloc == "power.larc.nasa.gov":
                self.assertEqual(params["time-standard"], ["UTC"])
                self.assertEqual(params["start"], ["20240430"])
                self.assertEqual(params["end"], ["20240502"])
                raise URLError("offline")
            self.assertEqual(params["timezone"], ["GMT"])
            self.assertEqual(params["models"], ["era5"])
            self.assertEqual(params["end_date"], ["2024-05-02"])
            return io.BytesIO((fill.FIXTURES / fill.MODELS["era5"].snapshot).read_bytes())

        rows = [observation("2024-05-01 00:01", 1), observation("2024-05-01 23:59", 2)]
        with patch.object(fill, "_CACHE", OrderedDict()), patch.object(fill, "urlopen", side_effect=serve) as opening:
            result = fill.apply_estimates(report("2024-05-01", rows=rows),
                                          dict(QUERY, subhour="interpolate", timezone="UTC"))
            replay = fill.apply_estimates(report("2024-05-01", rows=rows),
                                          dict(QUERY, subhour="interpolate", timezone="UTC"))
        self.assertEqual(opening.call_count, 2)
        self.assertEqual(result, replay)
        self.assertEqual(result["external"]["estimated_count"], 0)
        self.assertEqual([s["status"] for s in result["external"]["sources"]], ["available", "unavailable"])

    def test_custom_station_points_use_their_own_network_cache(self):
        first = {"latitude": 53.5, "longitude": 40.0}
        second = {"latitude": 53.75, "longitude": 40.25}
        archive = {"latitude": 56.52, "longitude": 32.87}
        era = json.loads((fill.FIXTURES / fill.MODELS["era5"].snapshot).read_bytes())
        merra = json.loads((fill.FIXTURES / fill.MODELS["merra2"].snapshot).read_bytes())

        def serve(url, timeout):
            parts = urlparse(url)
            params = parse_qs(parts.query)
            coordinates = {key: float(params[key][0]) for key in first}
            self.assertIn(coordinates, (first, second))
            value = 12.0 if coordinates == first else 18.0
            if parts.netloc == "archive-api.open-meteo.com":
                body = copy.deepcopy(era)
                body.update(coordinates)
                body["hourly"]["temperature_2m"] = [value] * len(body["hourly"]["time"])
            else:
                self.assertEqual(parts.netloc, "power.larc.nasa.gov")
                body = copy.deepcopy(merra)
                body["geometry"]["coordinates"] = [coordinates["longitude"], coordinates["latitude"], 200.0]
                hours = body["properties"]["parameter"]["T2M"]
                body["properties"]["parameter"]["T2M"] = dict.fromkeys(hours, value + 4)
            return io.BytesIO(json.dumps(body).encode())

        with patch.object(fill, "_CACHE", OrderedDict()), patch.object(fill, "urlopen", side_effect=serve) as opening:
            reserve = fill.apply_estimates(report(), dict(QUERY, **archive))
            first_result = fill.apply_estimates(report(), dict(QUERY, **first))
            second_result = fill.apply_estimates(report(), dict(QUERY, **second))
            replay = fill.apply_estimates(report(), dict(QUERY, **first))
        self.assertEqual(opening.call_count, 4)
        self.assertEqual(reserve["rows"][11]["estimate"], 9.0)
        self.assertEqual([source["mode"] for source in reserve["external"]["sources"]], ["snapshot", "snapshot"])
        self.assertEqual(first_result["rows"][11]["estimate"], 14.0)
        self.assertEqual(second_result["rows"][11]["estimate"], 20.0)
        self.assertEqual(replay, first_result)
        self.assertEqual(second_result["external"]["requested_coordinates"], second)
        provenance = second_result["rows"][11]["provenance"]
        self.assertEqual(provenance["requested_coordinates"], second)
        for source in provenance["sources"]:
            self.assertEqual({key: source["coordinates"][key] for key in second}, second)
            params = parse_qs(urlparse(source["url"]).query)
            self.assertEqual({key: float(params[key][0]) for key in second}, second)

    def test_malformed_station_coordinates_stop_before_network(self):
        invalid = ({"latitude": 56.52}, {"longitude": 32.87}, {"latitude": True, "longitude": 32.87},
                   {"latitude": "56.52", "longitude": 32.87}, {"latitude": 91, "longitude": 0},
                   {"latitude": 56.52, "longitude": float("nan")}, {"latitude": 0, "longitude": 181})
        with patch.object(fill, "_load_sources") as loader:
            for coordinates in invalid:
                with self.subTest(coordinates=coordinates), self.assertRaises(ValueError):
                    fill.apply_estimates(report(), dict(QUERY, **coordinates))
        loader.assert_not_called()

    def test_http_error_and_incompatible_payload(self):
        start, end = fill.SNAPSHOT_START_UTC, fill.SNAPSHOT_END_UTC
        model = fill.MODELS["merra2"]
        with patch.object(fill, "urlopen", side_effect=HTTPError(model.endpoint, 503, "unavailable", {}, None)):
            data = fill._load_model(model, start, end, False, fill.ARCHIVE_POINT)
        self.assertEqual(data.metadata["error"], "HTTP 503")
        body = json.loads((fill.FIXTURES / model.snapshot).read_bytes())
        body["header"]["sources"].append("GEOS-IT")
        with patch.object(fill, "urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            data = fill._load_model(model, start, end, False, fill.ARCHIVE_POINT)
        self.assertEqual(data.metadata["status"], "unavailable")

    def test_wait_budget_reports_unavailable_source(self):
        pending = Future()
        with patch.object(fill._FETCHERS, "submit", return_value=pending), patch.object(fill, "TIMEOUT", 0):
            stamps = {datetime(2024, 5, 2, 12, tzinfo=fill.UTC)}
            data, = fill._load_sources(("era5",), stamps, False, fill.ARCHIVE_POINT)
        self.assertTrue(pending.cancelled())
        self.assertEqual(data.metadata["status"], "unavailable")
        self.assertEqual(data.metadata["error"], "Истекло время ожидания")


if __name__ == "__main__":
    unittest.main()
