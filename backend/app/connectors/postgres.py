"""PostgreSQL connector (read-only): configured table rows -> one markdown document per row.

Safety: no user-supplied SQL ever. Identifiers must be plain names, must exist in the catalog the
role can see, and are double-quoted. Sessions start with default_transaction_read_only=on and a
statement timeout, and a role that can write to a configured table (or a superuser) is refused.

Incremental: with `updated_column`, runs read only rows with updated >= the saved watermark and
never delete (the engine is told via cursor["incremental"]). Every `full_sync_every`-th run, and
the first, reads everything: rows gone from the table are deleted then.
"""

import hashlib
import ssl
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

import asyncpg
from pydantic import BaseModel, Field, JsonValue, SecretStr, ValidationError

from app.connectors.base import Checked, Config, ConnectorError, ConnectorMeta, Item
from app.connectors.ssrf import BlockedURL, endpoint_check, resolve_checked
from app.ingestion.config import get_ingestion_settings

STATEMENT_TIMEOUT_MS = 15_000
PAGE_ROWS = 500
Ident = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")]
# Watermark types: values that round-trip through JSON and compare correctly in SQL.
WATERMARK_TYPES: dict[str, Callable[[Any], Any]] = {
    "timestamp with time zone": datetime.fromisoformat,
    "timestamp without time zone": datetime.fromisoformat,
    "date": date.fromisoformat,
    "integer": int,
    "bigint": int,
    "smallint": int,
}


class TableSpec(BaseModel):
    name: Ident
    id_column: Ident = Field(description="Unique, sortable key (primary key)")
    text_columns: list[Ident] = Field(min_length=1, max_length=20)
    title_column: Ident | None = None
    updated_column: Ident | None = Field(
        default=None, description="Timestamp or integer version for incremental runs"
    )

    def columns(self) -> list[str]:
        cols = [self.id_column, self.title_column, *self.text_columns, self.updated_column]
        return list(dict.fromkeys(c for c in cols if c))


class PgConfig(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_$.-]+$")
    db_schema: Ident = Field(default="public", alias="schema")
    tables: list[TableSpec] = Field(min_length=1, max_length=20)
    sslmode: Literal["disable", "require", "verify-full"] = "require"
    max_rows_per_table: int = Field(default=10_000, ge=1, le=100_000)
    full_sync_every: int = Field(
        default=12, ge=1, le=1000, description="Every Nth run reads all rows and applies deletions"
    )
    allow_private_endpoint: bool = Field(
        default=False,
        description="Allow a host in a private network (self-hosted DB). Off by default.",
    )


class PgSecret(BaseModel):
    user: str = Field(min_length=1, max_length=63)
    password: SecretStr = Field(max_length=1024)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _parse(config: Config, secret: str | None) -> tuple[PgConfig, PgSecret]:
    cfg = PgConfig.model_validate(config)
    try:
        return cfg, PgSecret.model_validate_json(secret or "")
    except ValidationError:
        # `from None`: the validation error would carry the secret's text in its traceback.
        raise ConnectorError(
            'Credentials must be JSON: {"user": "...", "password": "..."}.'
        ) from None


async def _connect(cfg: PgConfig, creds: PgSecret) -> asyncpg.Connection:
    check = endpoint_check(cfg.allow_private_endpoint)
    try:
        ip = await resolve_checked(cfg.host, cfg.port, check)
    except BlockedURL as exc:
        hint = (
            "" if cfg.allow_private_endpoint else " A self-hosted DB needs allow_private_endpoint."
        )
        raise ConnectorError(f"{exc}{hint}") from None
    # Connect to the checked IP (no second DNS lookup). verify-full must use the name for the
    # certificate check, which also defeats rebinding: a rebound host can't present that cert.
    host = cfg.host if cfg.sslmode == "verify-full" else ip
    tls: ssl.SSLContext | str | bool = cfg.sslmode == "require" and "require"
    if cfg.sslmode == "verify-full":
        tls = ssl.create_default_context()  # system CAs + hostname check
    try:
        conn: asyncpg.Connection = await asyncpg.connect(
            host=host, port=cfg.port, user=creds.user, database=cfg.database,
            password=creds.password.get_secret_value(), ssl=tls, timeout=10,
            command_timeout=STATEMENT_TIMEOUT_MS / 1000 + 5, statement_cache_size=0,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": "60000",
                "lock_timeout": "5000",
                "application_name": "rag-connector",
            },
        )  # fmt: skip
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise ConnectorError(f"Could not connect to the database ({type(exc).__name__}).") from None
    return conn


async def _catalog(conn: asyncpg.Connection, cfg: PgConfig) -> dict[str, dict[str, str]]:
    """{table: {column: data_type}} for the configured tables, as visible to this role."""
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = ANY($2::text[])",
        cfg.db_schema,
        [t.name for t in cfg.tables],
    )
    found: dict[str, dict[str, str]] = defaultdict(dict)
    for r in rows:
        found[r["table_name"]][r["column_name"]] = r["data_type"]
    for t in cfg.tables:
        if t.name not in found:
            visible = await conn.fetch(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = $1 ORDER BY 1 LIMIT 50",
                cfg.db_schema,
            )
            names = ", ".join(r[0] for r in visible) or "none"
            raise ConnectorError(
                f"Table {cfg.db_schema}.{t.name} was not found. Readable tables: {names}."
            )
        if missing := [c for c in t.columns() if c not in found[t.name]]:
            raise ConnectorError(f"Column(s) not found in {t.name}: {', '.join(missing)}.")
        if t.updated_column and found[t.name][t.updated_column] not in WATERMARK_TYPES:
            raise ConnectorError(f"{t.name}.{t.updated_column} must be a timestamp or integer.")
    return found


async def _refuse_writers(conn: asyncpg.Connection, cfg: PgConfig) -> None:
    row = await conn.fetchrow(
        "SELECT r.rolsuper, array(SELECT t FROM unnest($2::text[]) t WHERE has_table_privilege("
        "quote_ident($1) || '.' || quote_ident(t), 'INSERT, UPDATE, DELETE, TRUNCATE')) AS w "
        "FROM pg_roles r WHERE r.rolname = current_user",
        cfg.db_schema,
        [t.name for t in cfg.tables],
    )
    if row is None or row["rolsuper"] or row["w"]:
        what = (
            "is a superuser"
            if row is None or row["rolsuper"]
            else f"can modify {', '.join(row['w'])}"
        )
        raise ConnectorError(
            f"The database role {what}. Use a read-only role (GRANT SELECT only) for this source."
        )


async def _readable(conn: asyncpg.Connection, cfg: PgConfig) -> dict[str, list[str]]:
    """{table: [columns]} the role may SELECT in the schema, for the UI's schema browser."""
    # ponytail: first 5000 columns of the schema; page it if someone has a huge schema.
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND has_column_privilege("
        "quote_ident(table_schema) || '.' || quote_ident(table_name), column_name, 'SELECT') "
        "ORDER BY table_name, ordinal_position LIMIT 5000",
        cfg.db_schema,
    )
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        out[r["table_name"]].append(r["column_name"])
    return dict(out)


def _render(t: TableSpec, row: asyncpg.Record) -> str:
    title = row[t.title_column] if t.title_column else None
    parts = [f"# {title}"] if title is not None and str(title).strip() else []
    for c in t.text_columns:
        if row[c] is not None and str(row[c]).strip():
            parts.append(f"## {c}\n\n{row[c]}")
    return "\n\n".join(parts)


def _row_item(cfg: PgConfig, t: TableSpec, row: asyncpg.Record, text: str) -> Item:
    rid = row[t.id_column]
    record: JsonValue = rid if isinstance(rid, int | str) else str(rid)
    data = text.encode()

    async def fetch() -> bytes:
        return data

    title = row[t.title_column] if t.title_column else None
    return Item(
        external_key=f"{cfg.db_schema}.{t.name}:{rid}",
        name=(str(title or "").strip() or f"{t.name} #{rid}")[:255],
        fetch=fetch,
        mime_hint="text/markdown",
        content_hash=hashlib.sha256(data).hexdigest(),
        meta={"record": record, "table": f"{cfg.db_schema}.{t.name}"},
    )


def _select(cfg: PgConfig, t: TableSpec, with_mark: bool, after_id: bool, limit: int) -> str:
    """Only validated identifiers (quoted) and server-chosen numbers; values are $ parameters."""
    conds = [f"{_q(t.updated_column)} >= $1"] if with_mark and t.updated_column else []
    if after_id:
        conds.append(f"{_q(t.id_column)} > ${len(conds) + 1}")
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    cols = ", ".join(_q(c) for c in t.columns())
    return (
        f"SELECT {cols} FROM {_q(cfg.db_schema)}.{_q(t.name)}{where} "  # noqa: S608
        f"ORDER BY {_q(t.id_column)} LIMIT {int(limit)}"
    )


async def _page(conn: asyncpg.Connection, sql: str, args: list[Any], name: str) -> list[Any]:
    try:
        return list(await conn.fetch(sql, *args))
    except asyncpg.QueryCanceledError:
        raise ConnectorError(
            f"Reading {name} took longer than {STATEMENT_TIMEOUT_MS // 1000} s."
        ) from None
    except asyncpg.PostgresError as exc:
        raise ConnectorError(f"Reading {name} failed ({type(exc).__name__}).") from None


async def _rows(
    conn: asyncpg.Connection, cfg: PgConfig, t: TableSpec, mark: Any, marks: dict[str, Any]
) -> AsyncIterator[Item]:
    """Keyset pagination by id; records the newest updated value as the next watermark."""
    cap, n, last, newest = get_ingestion_settings().max_upload_mb * 2**20, 0, None, mark
    while n < cfg.max_rows_per_table:
        limit = min(PAGE_ROWS, cfg.max_rows_per_table - n)
        sql = _select(cfg, t, mark is not None, last is not None, limit)
        args = [a for a in (mark, last) if a is not None]
        rows = await _page(conn, sql, args, f"{cfg.db_schema}.{t.name}")
        for row in rows:
            if t.updated_column and row[t.updated_column] is not None:
                v = row[t.updated_column]
                newest = v if newest is None else max(newest, v)
            text = _render(t, row)
            if text and len(text) <= cap:  # empty rows hold nothing to search
                yield _row_item(cfg, t, row, text)
        n += len(rows)
        if len(rows) < limit:
            break
        last = rows[-1][t.id_column]
    if newest is not None:
        marks[t.name] = newest.isoformat() if isinstance(newest, date) else newest


class PostgresConnector:
    meta = ConnectorMeta(
        type="postgres",
        title="PostgreSQL",
        description="Index rows of chosen tables. Read-only role required; no custom SQL.",
        category="database",
        auth="basic",
        config_schema=PgConfig.model_json_schema(by_alias=True),
    )

    # Config fields that pick the server/account the stored password is sent to (see router).
    target_fields = ("host", "port", "database", "sslmode")

    def validate_config(self, raw: Config) -> Config:
        return PgConfig.model_validate(raw).model_dump(by_alias=True)

    async def test(self, config: Config, secret: str | None) -> Checked:
        cfg, creds = _parse(config, secret)
        conn = await _connect(cfg, creds)
        try:
            found = await _catalog(conn, cfg)
            await _refuse_writers(conn, cfg)
            readable = await _readable(conn, cfg)
        finally:
            await conn.close()
        tables = "; ".join(f"{t} ({', '.join(cols)})" for t, cols in found.items())
        return Checked(f"Connected with a read-only role. Tables: {tables}"[:1000], readable)

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg, creds = _parse(config, secret)
        runs = int(cursor.get("runs", 0))
        full = runs % cfg.full_sync_every == 0
        marks: dict[str, Any] = dict(cursor.get("watermarks", {}))
        conn = await _connect(cfg, creds)
        try:
            types = await _catalog(conn, cfg)
            await _refuse_writers(conn, cfg)
            for t in cfg.tables:
                raw = marks.get(t.name) if t.updated_column and not full else None
                parse = WATERMARK_TYPES.get(types[t.name].get(t.updated_column or "", ""))
                mark = parse(raw) if raw is not None and parse else None
                async for item in _rows(conn, cfg, t, mark, marks):
                    yield item
        finally:
            await conn.close()
        cursor.update(runs=runs + 1, watermarks=marks, incremental=not full)
