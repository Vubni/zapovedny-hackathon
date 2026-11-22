"""Extract sensor readings and prior decisions for a single-parameter draft."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from heapq import merge
from itertools import groupby
from operator import itemgetter

from build_data import STATIONS
from observations_db import uses_archive
from review_filter import archive_observations, checked_date, minute_bounds
from review_upload import MAX_ROWS


def _flags(flags, parameter):
    return ";".join(flag.strip() for flag in flags.split(";")
                    if flag.strip() == "dup_conflict" or flag.strip().startswith(parameter + ":"))


def _row(stamp, parameter, value, origin, *, line=None, flags="", decision=""):
    return {"datetime": stamp, parameter: value, "qc_flag": _flags(flags, parameter),
            "origin": origin, "origin_line": line, "origin_decision": decision}


def _archived_terms(station, parameter, start, end):
    if not uses_archive() or station not in STATIONS:
        return
    first, last = minute_bounds(checked_date(start), checked_date(end))
    rows = archive_observations(station, parameter, first, last, {})
    with closing(rows):
        for stamp, group in groupby(rows, key=lambda row: row.time):
            yield stamp, [
                _row(stamp, parameter, item.original.get(parameter), f"archive:{item.source}",
                     line=item.line, flags=item.original.get("qc_flag", ""))
                for item in group
            ]


def _rejected_rows(source, payload, parameter, start, end):
    decisions = {(item[0], item[1]): item[4] for item in payload["changes"] if item[2] == parameter}
    manual = payload.get("manual_cells", {}).get(parameter, {})
    by_time = defaultdict(list)
    for line, item in source:
        if parameter not in item:
            continue
        date = item["datetime"][:10]
        if not start <= date <= end:
            continue
        stamp = datetime.fromisoformat(item["datetime"]).isoformat(" ", timespec="minutes")
        value = item.get(parameter)
        decision = ""
        if value is not None and str(value).strip():
            if stamp in manual and manual[stamp]["value"] is None:
                decision = "manual_clear"
            else:
                decision = ", ".join(decisions.get((stamp, line), ())) or item.get("origin_decision") or "rejected"
        by_time[stamp].append((line, value, item.get("qc_flag", ""), decision))
    return by_time


def _published_terms(confirmed, parameter, start, end, read_source):
    sources = {}
    for row in confirmed:
        if row["synthetic"]:
            continue
        stamp = row["datetime"]
        source = row["observation_sources"].get(parameter)
        if source is None:
            yield stamp, [_row(stamp, parameter, None, "term")]
            continue
        file_id, version = source
        origin = f"confirmed:{file_id}@{version}"
        value = row.get(parameter)
        if value is not None:
            yield stamp, [_row(stamp, parameter, value, origin)]
            continue
        if source not in sources:
            original, payload = read_source(file_id, version)
            sources[source] = _rejected_rows(original, payload, parameter, start, end) if payload is not None else {}
        rejected = sources[source].get(stamp, ())
        yield stamp, [
            _row(stamp, parameter, value, origin, line=line, flags=flags, decision=decision)
            for line, value, flags, decision in rejected
        ] or [_row(stamp, parameter, None, origin)]


def selection_rows(station, parameter, start, end, confirmed, read_source):
    """Overlay confirmed measurements and exclusions without treating estimates as facts."""
    records = []
    sources = Counter()
    with closing(_archived_terms(station, parameter, start, end)) as archived, closing(confirmed):
        published = _published_terms(confirmed, parameter, start, end, read_source)
        for _, group in groupby(merge(archived, published, key=itemgetter(0)), key=itemgetter(0)):
            _, selected = next(group)
            for _, candidate in group:
                if candidate[0]["origin"] != "term":
                    selected = candidate
            if len(records) + len(selected) > MAX_ROWS:
                raise ValueError("Выберите период не более 60 000 исходных строк для черновика.")
            for record in selected:
                records.append((len(records) + 2, record))
                sources[record["origin"].partition(":")[0]] += 1
    if not records:
        raise ValueError("За выбранный период у станции нет исходных сроков наблюдений.")
    return records, dict(sources)
