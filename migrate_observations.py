"""Copy one local observation-store snapshot to an empty PostgreSQL database."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import zlib
from contextlib import closing, contextmanager
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

from build_data import STATIONS
from observations_db import initialize_postgres


COLUMNS = {
    "registered_stations": "id name_key passport".split(),
    "files": ("id filename mime original source columns rows start end station error created_at updated_at "
              "generation version draft draft_meta confirmed confirmed_meta confirmed_order").split(),
    "published": "file_id station time synthetic data".split(),
    "file_corrections": "file_id revision parent_revision filename original_csv parent_result parent_meta created_at".split(),
}
BINARY = {"original", "source", "draft", "confirmed", "original_csv", "parent_result"}
BIGINT = {"generation", "version", "confirmed_order", "ordinal"}
INTEGER = BIGINT | {"rows", "synthetic"}
NULLABLE = {"source", "start", "end", "error", "draft", "draft_meta", "confirmed", "confirmed_meta", "confirmed_order"}


class MigrationError(ValueError):
    pass


def _columns(names):
    # These identifiers come only from COLUMNS and the shared schema, never from the input database.
    return ", ".join(f'"{name}"' for name in names)


def _insert(table, names):
    placeholders = ", ".join(["%s"] * len(names))
    return f'INSERT INTO "{table}" ({_columns(names)}) VALUES ({placeholders})'


@contextmanager
def source_snapshot(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise MigrationError("Укажите файл исходной SQLite.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def _source_layout(db):
    tables = set()
    for kind, name in db.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view', 'trigger')"):
        if name.startswith("sqlite_"):
            continue
        if kind != "table" or name not in COLUMNS:
            raise MigrationError(f"Неизвестная схема SQLite: объект {name}.")
        tables.add(name)
    if not tables or ("files" in tables) != ("published" in tables) or ("file_corrections" in tables and "files" not in tables):
        raise MigrationError("В SQLite неполная схема хранилища наблюдений.")
    for table in tables:
        columns = {name: kind.upper() for name, kind in db.execute("SELECT name, type FROM pragma_table_xinfo(?)", (table,))}
        expected = {name: {"BLOB"} if name in BINARY else {"INTEGER", "BIGINT"} if name in INTEGER else {"TEXT"}
                    for name in COLUMNS[table]}
        if columns.keys() != expected.keys() or any(kind not in expected[name] for name, kind in columns.items()):
            raise MigrationError(f"Неизвестная схема столбцов SQLite в {table}.")
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise MigrationError("SQLite не прошла проверку целостности.")
    for table in tables & {"published", "file_corrections"}:
        if db.execute(f"SELECT 1 FROM {table} LEFT JOIN files ON files.id={table}.file_id WHERE files.id IS NULL LIMIT 1").fetchone():
            raise MigrationError(f"Таблица {table} ссылается на отсутствующий файл.")
    return tables


def _rows(db, table, *, station_order=False):
    selector, order = ("rowid AS ordinal, ", " ORDER BY rowid") if station_order else ("", "")
    return db.execute(f'SELECT {selector}{_columns(COLUMNS[table])} FROM "{table}"{order}')


def _types(row, table):
    for name in COLUMNS[table]:
        value = row[name]
        expected = bytes if name in BINARY else int if name in INTEGER else str
        if value is None and name in NULLABLE:
            continue
        if type(value) is not expected or (expected is str and "\0" in value):
            raise MigrationError(f"В {table}.{name} обнаружено значение несовместимого типа.")


def _reject_constant(_):
    raise ValueError("non-finite JSON value")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        _reject_constant(text)
    return value


def _json(value, kind, location, *, compressed=False):
    try:
        parsed = json.loads(zlib.decompress(value) if compressed else value,
                            parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError, zlib.error):
        raise MigrationError(f"Повреждены сохранённые данные в {location}.") from None
    if not isinstance(parsed, kind):
        raise MigrationError(f"Неверный формат сохранённых данных в {location}.")
    return parsed


def _canonical_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is None and parsed.isoformat(" ", timespec="minutes") == value


def _revision(value, meta, stations, location):
    document = _json(value, dict, location, compressed=True)
    summary = _json(meta, dict, location + "_meta")
    settings = summary.get("settings")
    revision = summary.get("revision")
    if (not isinstance(revision, str) or not revision or not isinstance(settings, dict)
            or not isinstance(settings.get("station"), str) or settings["station"] not in stations
            or not isinstance(document.get("rows"), list)):
        raise MigrationError(f"Не удалось разрешить ревизию или станцию в {location}.")
    timestamps = set()
    for row in document["rows"]:
        timestamp = row.get("datetime") if isinstance(row, dict) else None
        if (not _canonical_timestamp(timestamp) or timestamp in timestamps or row.get("synthetic") not in (0, 1)
                or not isinstance(row.get("estimates"), dict)):
            raise MigrationError(f"Неразрешимый срок наблюдения в {location}.")
        timestamps.add(timestamp)
    return document, summary


def _existing_relations(conn):
    return conn.execute("""
        SELECT c.relname, c.relkind, n.nspname = current_schema(), c.relrowsecurity, c.relhasrules
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r','p','v','m','f')
    """).fetchall()


def _guard_target_layout(conn):
    if any(name not in COLUMNS or kind != "r" or not same_schema or rls or rules
           for name, kind, same_schema, rls, rules in _existing_relations(conn)):
        raise MigrationError("Целевая PostgreSQL содержит постороннюю схему или таблицы.")


def _prepare_target(conn):
    _guard_target_layout(conn)
    initialize_postgres(conn)
    conn.execute("LOCK TABLE " + _columns(COLUMNS) + " IN ACCESS EXCLUSIVE MODE")
    _guard_target_layout(conn)
    columns = {table: {} for table in COLUMNS}
    for table, name, typ, identity in conn.execute("""
        SELECT table_name, column_name, data_type, identity_generation FROM information_schema.columns
        WHERE table_schema=current_schema()
    """):
        if table in columns:
            columns[table][name] = (typ, identity)
    for table, names in COLUMNS.items():
        expected = {name: ("bytea" if name in BINARY else "bigint" if name in BIGINT
                           else "integer" if name in INTEGER else "text", None) for name in names}
        if table == "registered_stations":
            expected["ordinal"] = ("bigint", "BY DEFAULT")
        if columns[table] != expected:
            raise MigrationError(f"Неожиданная схема PostgreSQL в {table}.")
        if conn.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")').fetchone()[0]:
            raise MigrationError(f"Целевая PostgreSQL не пуста: в {table} уже есть записи.")
    if conn.execute("""
        SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace LIMIT 1
    """).fetchone():
        raise MigrationError("В целевой PostgreSQL обнаружены пользовательские триггеры.")


def _copy_stations(src, dest, tables, counts):
    known = set(STATIONS)
    if "registered_stations" not in tables:
        return known
    insert = _insert("registered_stations", ["ordinal", *COLUMNS["registered_stations"]])
    last = 0
    for row in _rows(src, "registered_stations", station_order=True):
        _types(row, "registered_stations")
        item = _json(row["passport"], dict, "registered_stations.passport")
        profile = item.get("profile")
        if (item.get("id") != row["id"] or row["id"] in known
                or ("profile" in item and (not isinstance(profile, str) or profile not in STATIONS))
                or not isinstance(item.get("label"), str)
                or item["label"].casefold() != row["name_key"] or not isinstance(item.get("params"), list)):
            raise MigrationError("Паспорт станции не согласуется с каталогом SQLite.")
        known.add(row["id"])
        last = max(last, row["ordinal"])
        dest.execute(insert, tuple(row))
        counts["registered_stations"] += 1
    if last >= 2 ** 63 - 1:
        raise MigrationError("Исчерпан диапазон порядковых номеров станций.")
    if counts["registered_stations"]:
        dest.execute(f"ALTER TABLE registered_stations ALTER COLUMN ordinal RESTART WITH {last + 1}")
    return known


def _facts(src, file_id, station, expected):
    names = _columns(COLUMNS["published"])
    found = src.execute(f"SELECT {names} FROM published WHERE file_id=? ORDER BY time", (file_id,))
    expected = sorted(expected, key=lambda item: item["datetime"])
    for row, observation in zip_longest(found, expected):
        if row is None or observation is None:
            raise MigrationError("Публикация не совпадает с сохранёнными строками подтверждённой ревизии.")
        _types(row, "published")
        fact = _json(row["data"], dict, "published.data")
        if (row["station"] != station or row["synthetic"] not in (0, 1) or fact.get("datetime") != row["time"]
                or fact.get("synthetic") != row["synthetic"] or fact != observation):
            raise MigrationError("Факт в published не соответствует подтверждённой ревизии.")
        yield tuple(row)


def _copy_files(src, dest, stations, counts):
    file_sql = _insert("files", COLUMNS["files"])
    fact_sql = _insert("published", COLUMNS["published"])
    orders = set()
    for row in _rows(src, "files"):
        _types(row, "files")
        if row["station"] not in stations or min(row["rows"], row["generation"], row["version"]) < 0:
            raise MigrationError("Файл ссылается на неизвестную станцию или повреждены счётчики его версий.")
        fields = _json(row["columns"], list, "files.columns")
        if any(not isinstance(field, str) for field in fields):
            raise MigrationError("Неверный формат столбцов исходного файла.")
        source = _json(row["source"], list, "files.source", compressed=True) if row["source"] is not None else []
        if len(source) != row["rows"] or any(
            not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int or item[0] < 1
            or not isinstance(item[1], dict) or not isinstance(item[1].get("datetime"), str) for item in source
        ):
            raise MigrationError("Сохранённые исходные строки не согласуются с файлом.")
        observations = []
        for kind in ("draft", "confirmed"):
            body, meta = row[kind], row[kind + "_meta"]
            if (body is None) != (meta is None):
                raise MigrationError(f"Неполная сохранённая ревизия {kind}.")
            if body is None:
                continue
            document, summary = _revision(body, meta, stations, "files." + kind)
            if kind == "confirmed":
                order = row["confirmed_order"]
                if (summary["settings"]["station"] != row["station"] or order is None or order <= 0
                        or order in orders or row["version"] <= 0):
                    raise MigrationError("Не удалось определить станцию или порядок подтверждённой ревизии.")
                orders.add(order)
                observations = document["rows"]
        if row["confirmed"] is None and row["confirmed_order"] is not None:
            raise MigrationError("У порядка публикации отсутствует подтверждённая ревизия.")
        dest.execute(file_sql, tuple(row))
        counts["files"] += 1
        with dest.cursor() as cursor:
            cursor.executemany(fact_sql, _facts(src, row["id"], row["station"], observations))
            counts["published"] += len(observations)


def _copy_corrections(src, dest, stations, counts):
    insert = _insert("file_corrections", COLUMNS["file_corrections"])
    for row in _rows(src, "file_corrections"):
        _types(row, "file_corrections")
        _, parent = _revision(row["parent_result"], row["parent_meta"], stations, "file_corrections.parent_result")
        if row["revision"] == row["parent_revision"] or parent["revision"] != row["parent_revision"]:
            raise MigrationError("Не удалось разрешить родительскую ревизию исправления.")
        dest.execute(insert, tuple(row))
        counts["file_corrections"] += 1


def migrate(path, destination):
    """Copy to a fresh psycopg connection, committing only after all validations and inserts."""
    counts = dict.fromkeys(COLUMNS, 0)
    with source_snapshot(path) as source:
        tables = _source_layout(source)
        with destination.transaction():
            destination.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            _prepare_target(destination)
            stations = _copy_stations(source, destination, tables, counts)
            if "files" in tables:
                _copy_files(source, destination, stations, counts)
            if "file_corrections" in tables:
                _copy_corrections(source, destination, stations, counts)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Перенести локальные наблюдения в пустую PostgreSQL из METEO_DATABASE_URL.")
    parser.add_argument("--source", type=Path, required=True, help="путь к исходной SQLite")
    args = parser.parse_args()
    url = os.environ.get("METEO_DATABASE_URL", "").strip()
    if not url:
        parser.exit(2, "Укажите целевую PostgreSQL в METEO_DATABASE_URL.\n")
    try:
        import psycopg
    except ImportError:
        parser.exit(2, "Установите драйвер PostgreSQL: python3 -m pip install 'psycopg[binary]'.\n")
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as destination:
            counts = migrate(args.source, destination)
    except (MigrationError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Перенос остановлен. {error}\n")
    except psycopg.Error as error:
        parser.exit(1, f"Перенос остановлен PostgreSQL. Код SQLSTATE {error.sqlstate or 'недоступен'}.\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
