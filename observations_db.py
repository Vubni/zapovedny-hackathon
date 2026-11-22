"""Connections and schema for user-editable weather observations."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
from threading import Lock


ROOT = Path(__file__).resolve().parent
CENTRAL_CENTER = "central-forest"
_CENTER = ContextVar("observation_center", default=CENTRAL_CENTER)
_PUBLIC_ONLY = ContextVar("observation_public_archive_only", default=False)
# One transaction-scoped lock for initialization and mutable archive writes.
_WRITE_LOCK = 0x4D4554454F4442
_SCHEMA_LOCK = Lock()
_INITIALIZED_STORES = set()

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        mime TEXT NOT NULL,
        original {binary} NOT NULL,
        source {binary},
        columns TEXT NOT NULL,
        rows INTEGER NOT NULL CHECK (rows >= 0),
        start TEXT,
        "end" TEXT,
        station TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
        version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
        draft {binary},
        draft_meta TEXT,
        confirmed {binary},
        confirmed_meta TEXT,
        confirmed_order BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published (
        file_id TEXT NOT NULL REFERENCES files(id),
        station TEXT NOT NULL,
        time TEXT NOT NULL,
        synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
        data TEXT NOT NULL,
        PRIMARY KEY (file_id, time)
    )
    """,
    "CREATE INDEX IF NOT EXISTS published_station_time ON published (station, time)",
    """
    CREATE TABLE IF NOT EXISTS file_corrections (
        file_id TEXT NOT NULL REFERENCES files(id),
        revision TEXT NOT NULL,
        parent_revision TEXT NOT NULL,
        filename TEXT NOT NULL,
        original_csv {binary} NOT NULL,
        parent_result {binary} NOT NULL,
        parent_meta TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (file_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS registered_stations (
        id TEXT PRIMARY KEY,
        name_key TEXT UNIQUE NOT NULL,
        passport TEXT NOT NULL{ordinal}
    )
    """,
)


def _dsn():
    return os.environ.get("METEO_DATABASE_URL", "").strip()


def using_postgres():
    return bool(_dsn())


def current_center():
    return _CENTER.get()


def uses_archive():
    return current_center() == CENTRAL_CENTER


@contextmanager
def center_scope(center_id, *, public_only=False):
    """Select one center for every mutable observation operation in this context."""
    if not isinstance(center_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", center_id):
        raise ValueError("Некорректный код центра.")
    token = _CENTER.set(center_id)
    public_token = _PUBLIC_ONLY.set(public_only)
    try:
        yield
    finally:
        _PUBLIC_ONLY.reset(public_token)
        _CENTER.reset(token)


def local_db_path(*, global_store=False):
    setting = os.environ.get("METEO_OBSERVATION_DB")
    path = Path(setting) if setting else ROOT / ".local" / "observations.sqlite3"
    if global_store or uses_archive():
        return path
    digest = sha256(current_center().encode()).hexdigest()
    return path.with_name(path.name + ".centers") / f"{digest}.sqlite3"


class _PostgresRow:
    """Provide named and positional access just as sqlite3.Row does."""
    __slots__ = ("_columns", "_values")

    def __init__(self, columns, values):
        self._columns = columns
        self._values = values

    def __getitem__(self, key):
        return self._values[self._columns[key] if isinstance(key, str) else key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _row_factory(cursor):
    columns = {column.name: index for index, column in enumerate(cursor.description or ())}
    return lambda values: _PostgresRow(columns, values)


class _Database:
    def __init__(self, conn, *, postgres):
        self._conn = conn
        self.is_postgres = postgres

    def execute(self, statement, params=()):
        if not self.is_postgres:
            return self._conn.execute(statement, params)
        if not params:
            return self._conn.execute(statement)
        return self._conn.execute(statement.replace("?", "%s"), params)

    def executemany(self, statement, entries):
        if not self.is_postgres:
            return self._conn.executemany(statement, entries)
        with self._conn.cursor() as cursor:
            cursor.executemany(statement.replace("?", "%s"), entries)
            return cursor.rowcount

    def table_exists(self, name):
        if self.is_postgres:
            return self.execute("SELECT to_regclass(?) IS NOT NULL", (name,)).fetchone()[0]
        return self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _create_schema(conn, *, postgres):
    binary = "BYTEA" if postgres else "BLOB"
    ordinal = ", ordinal BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE" if postgres else ""
    for statement in _SCHEMA:
        conn.execute(statement.format(binary=binary, ordinal=ordinal))


def _lock_postgres(conn):
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK,))


def _postgres_namespace(conn, center_id):
    base, = conn.execute("SELECT current_schema()").fetchone()
    if not base:
        raise RuntimeError("В PostgreSQL не задана основная схема Метеоархива.")
    digest = sha256(f"{base}\0{center_id}".encode()).hexdigest()
    return f"meteo_center_{digest[:48]}"


def _namespace_exists(conn, schema):
    return conn.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,)).fetchone()[0]


def _select_namespace(conn, schema):
    from psycopg import sql

    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))


def initialize_postgres(conn, *, schema=None):
    """Create mutable archive tables under a transaction managed by the caller."""
    _lock_postgres(conn)
    if schema is not None:
        from psycopg import errors, sql

        if not _namespace_exists(conn, schema):
            try:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            except errors.InsufficientPrivilege:
                raise RuntimeError("У роли PostgreSQL нет права создать схему центра. "
                                   "Для Compose выполните python3 setup_compose.py --up.") from None
        _select_namespace(conn, schema)
    _create_schema(conn, postgres=True)


def _ensure_postgres_schema(conn, dsn, schema, write):
    key = (dsn, schema)
    if key in _INITIALIZED_STORES:
        return True
    if schema is not None and not write and not _namespace_exists(conn, schema):
        return False
    with _SCHEMA_LOCK:
        if key not in _INITIALIZED_STORES:
            with conn.transaction():
                initialize_postgres(conn, schema=schema)
            _INITIALIZED_STORES.add(key)
    return True


@contextmanager
def _postgres_connection(dsn, write, global_store):
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("Для METEO_DATABASE_URL установите psycopg 3: pip install 'psycopg[binary]'.") from None
    with psycopg.connect(dsn, autocommit=True, row_factory=_row_factory, connect_timeout=5) as conn:
        schema = None if global_store or uses_archive() else _postgres_namespace(conn, current_center())
        if not _ensure_postgres_schema(conn, dsn, schema, write):
            yield None
            return
        with conn.transaction():
            if write:
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            else:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if schema is not None:
                _select_namespace(conn, schema)
            if write:
                _lock_postgres(conn)
            yield _Database(conn, postgres=True)


@contextmanager
def _sqlite_connection(write, require, global_store):
    path = local_db_path(global_store=global_store).resolve()
    if not write and not path.is_file():
        yield None
        return
    if write:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = path if write else path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(target, timeout=15, uri=not write)) as conn, conn:
        conn.row_factory = sqlite3.Row
        db = _Database(conn, postgres=False)
        if write:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            _create_schema(conn, postgres=False)
        if require is not None and not db.table_exists(require):
            yield None
        else:
            yield db


@contextmanager
def connection(*, write=False, require=None, global_store=False):
    """Yield a transaction on the configured backend, or None for an absent local store."""
    if not global_store and _PUBLIC_ONLY.get():
        if write:
            raise PermissionError("Гостевой доступ позволяет только просмотр исходного архива.")
        yield None
        return
    dsn = _dsn()
    if dsn:
        with _postgres_connection(dsn, write, global_store) as db:
            yield db
    else:
        with _sqlite_connection(write, require, global_store) as db:
            yield db


def initialize():
    """Initialize the configured backend and surface startup errors."""
    with connection(write=True):
        pass


def check_ready():
    """Check the configured mutable store without creating a cold local database."""
    with connection(global_store=True) as db:
        if db is None:
            return True
        if not db.is_postgres:
            files = db.table_exists("files")
            if files != db.table_exists("published"):
                raise RuntimeError("В локальном хранилище отсутствует часть схемы импорта.")
            if not files:
                return True
        db.execute("SELECT 1 FROM files LIMIT 1").fetchone()
    return True
