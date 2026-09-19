"""Query Databricks Lakebase (OLTP Postgres) as the signed-in user.

Databricks does not publish a managed MCP server for Lakebase, so it is exposed to the model
as local function tools instead. Governance is preserved the same way as the other tools: a
short-lived Postgres credential is minted from the *caller's own* Databricks OAuth token via
``POST /api/2.0/postgres/credentials``, so the connection authenticates as the signed-in user
(On-Behalf-Of) and Postgres role permissions decide what that user can read.

Queries are forced read-only by Postgres itself, capped by a statement timeout, and limited to
a bounded number of rows so a large result cannot blow up the model's context.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Annotated, Any

import httpx
from agent_framework import FunctionTool
from azure.core.credentials import TokenCredential

from .auth import token_header_provider
from .config import Settings

logger = logging.getLogger(__name__)

_MAX_ROWS = 50
_STATEMENT_TIMEOUT_MS = 30_000
_CONNECT_TIMEOUT_SECONDS = 20
_CREDENTIAL_PATH = "/api/2.0/postgres/credentials"

# Postgres schemas that only ever hold engine or sync bookkeeping.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "__db_system", "pg_toast")

# Columns, types, nullability, defaults and key relationships for one table, in a single
# round trip so a schema lookup costs one credential mint rather than several.
_DESCRIBE_SQL = """
with cols as (
    select column_name, ordinal_position, data_type, is_nullable, column_default
    from information_schema.columns
    where table_schema = %s and table_name = %s
),
keys as (
    select
        kcu.column_name,
        string_agg(
            case tc.constraint_type
                when 'PRIMARY KEY' then 'PK'
                when 'FOREIGN KEY' then
                    'FK -> ' || ccu.table_schema || '.' || ccu.table_name || '.' || ccu.column_name
                else tc.constraint_type
            end,
            ', ' order by tc.constraint_type
        ) as key_info
    from information_schema.table_constraints tc
    join information_schema.key_column_usage kcu
        on kcu.constraint_name = tc.constraint_name
        and kcu.constraint_schema = tc.constraint_schema
    left join information_schema.constraint_column_usage ccu
        on ccu.constraint_name = tc.constraint_name
        and ccu.constraint_schema = tc.constraint_schema
    where tc.table_schema = %s and tc.table_name = %s
        and tc.constraint_type in ('PRIMARY KEY', 'FOREIGN KEY')
    group by kcu.column_name
)
select
    cols.column_name,
    cols.data_type,
    cols.is_nullable,
    coalesce(cols.column_default, '') as column_default,
    coalesce(keys.key_info, '') as keys
from cols
left join keys on keys.column_name = cols.column_name
order by cols.ordinal_position
"""


class LakebaseQueryError(RuntimeError):
    """Raised when a Lakebase query is rejected before it reaches Postgres."""


def _identity_from_token(token: str) -> str:
    """Return the Postgres role name carried by a Databricks OAuth token.

    Databricks maps a user's Postgres role to their UPN and a service principal's to its
    client id, which are exactly the claims below. Decoding is for role naming only and is
    never used for authorization -- Postgres re-validates the token on connect.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:  # noqa: BLE001 - opaque tokens are a config problem, not a crash
        raise LakebaseQueryError(
            "Could not determine the Lakebase Postgres role from the Databricks token."
        ) from exc
    for claim in ("upn", "preferred_username", "unique_name", "appid", "azp"):
        value = claims.get(claim)
        if isinstance(value, str) and value:
            return value
    raise LakebaseQueryError("The Databricks token carries no usable identity claim.")


def _fetch_postgres_credential(settings: Settings, headers: dict[str, str]) -> str:
    url = (settings.databricks_host or "").rstrip("/") + _CREDENTIAL_PATH
    response = httpx.post(
        url, headers=headers, json={"endpoint": settings.lakebase_endpoint}, timeout=30
    )
    if response.status_code != 200:
        raise LakebaseQueryError(
            f"Could not obtain a Lakebase credential ({response.status_code})."
        )
    token = response.json().get("token")
    if not isinstance(token, str) or not token:
        raise LakebaseQueryError("Lakebase returned an empty credential.")
    return token


def _guard_read_only(sql: str) -> str:
    """Reject anything that is not a single read-only statement.

    Postgres also enforces read-only on the connection; this is the cheaper, clearer failure
    and it blocks multi-statement input outright.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise LakebaseQueryError("The query was empty.")
    if ";" in statement:
        raise LakebaseQueryError("Only a single SQL statement is allowed.")
    if not statement.lower().startswith(("select", "with")):
        raise LakebaseQueryError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    return statement


def _qualified(table_schema: str, table_name: str) -> tuple[str, str]:
    """Split a ``schema.table`` value the model may have put in either argument."""
    schema = table_schema.strip().strip('"')
    table = table_name.strip().strip('"')
    if "." in table:
        schema, _, table = table.rpartition(".")
        schema = schema.strip().strip('"')
    if not table:
        raise LakebaseQueryError("A table name is required.")
    return schema, table


def _render(columns: list[str], rows: list[tuple[Any, ...]], truncated: bool) -> str:
    if not rows:
        return "The query returned no rows."
    lines = [" | ".join(columns)]
    lines += [" | ".join("" if v is None else str(v) for v in row) for row in rows]
    if truncated:
        lines.append(f"... first {_MAX_ROWS} rows shown")
    return "\n".join(lines)


def _execute(
    settings: Settings,
    user: str,
    password: str,
    sql: str,
    params: tuple[Any, ...] | None = None,
) -> str:
    """Run one read-only statement. Synchronous: psycopg's async mode is unusable on Windows."""
    import psycopg

    with psycopg.connect(
        host=settings.lakebase_host,
        port=settings.lakebase_port,
        user=user,
        password=password,
        dbname=settings.lakebase_database,
        sslmode="require",
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
    ) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            if cur.description is None:
                return "The query returned no result set."
            columns = [d.name for d in cur.description]
            rows = cur.fetchmany(_MAX_ROWS + 1)
    truncated = len(rows) > _MAX_ROWS
    return _render(columns, rows[:_MAX_ROWS], truncated)


def make_lakebase_tools(
    settings: Settings, *, credential: TokenCredential | None = None
) -> list[FunctionTool]:
    """Build the read-only Lakebase tools bound to one request's identity."""
    headers = token_header_provider(settings, credential)

    async def _run(sql: str, params: tuple[Any, ...] | None = None) -> str:
        current = headers()
        bearer = current.get("Authorization", "").removeprefix("Bearer ").strip()
        user = _identity_from_token(bearer)
        password = await asyncio.to_thread(_fetch_postgres_credential, settings, current)
        return await asyncio.to_thread(_execute, settings, user, password, sql, params)

    async def list_lakebase_tables() -> str:
        """List the tables available in Lakebase."""
        excluded = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
        sql = (
            "select table_schema, table_name from information_schema.tables "
            f"where table_schema not in ({excluded}) order by 1, 2"
        )
        try:
            return await _run(sql)
        except LakebaseQueryError as exc:
            return f"Lakebase is unavailable: {exc}"

    async def describe_lakebase_table(
        table_schema: Annotated[str, "Schema name, as returned by list_lakebase_tables."],
        table_name: Annotated[str, "Table name, as returned by list_lakebase_tables."],
    ) -> str:
        try:
            schema, table = _qualified(table_schema, table_name)
            # Bound parameters: the names reach Postgres as values, never as SQL text.
            result = await _run(_DESCRIBE_SQL, (schema, table, schema, table))
        except LakebaseQueryError as exc:
            return f"Could not describe the table: {exc}"
        except Exception as exc:  # noqa: BLE001 - return the error so the model can correct it
            logger.warning("Lakebase describe failed: %s", exc)
            return f"Lakebase describe failed: {exc}"
        if result == "The query returned no rows.":
            return (
                f"No table named {schema}.{table} is visible to you. Call "
                "'list_lakebase_tables' to see the tables you can read."
            )
        return f"{schema}.{table}\n{result}"

    async def query_lakebase(
        sql: Annotated[str, "A single read-only Postgres SELECT statement."],
    ) -> str:
        try:
            return await _run(_guard_read_only(sql))
        except LakebaseQueryError as exc:
            return f"Query rejected: {exc}"
        except Exception as exc:  # noqa: BLE001 - return the error so the model can correct it
            logger.warning("Lakebase query failed: %s", exc)
            return f"Lakebase query failed: {exc}"

    return [
        FunctionTool(
            name="list_lakebase_tables",
            description=(
                "List the operational (row-level, current-state) tables you can read. Call "
                "this first whenever you do not already know what tables exist or which one "
                "holds the records the question is about."
            ),
            func=list_lakebase_tables,
        ),
        FunctionTool(
            name="describe_lakebase_table",
            description=(
                "Show one operational table's columns, data types, nullability, defaults, "
                "primary key and foreign keys. Call this for every table you plan to query "
                "so you use real column names and join keys instead of guessing."
            ),
            func=describe_lakebase_table,
        ),
        FunctionTool(
            name="query_lakebase",
            description=(
                "Run a single read-only SELECT against the operational database and return "
                "the rows. Use it for record-level and current-state questions: finding a "
                "specific record, checking a status, or filtering and counting rows "
                "exactly. Describe the tables first so the column names are real. Writes "
                "are rejected."
            ),
            func=query_lakebase,
        ),
    ]
