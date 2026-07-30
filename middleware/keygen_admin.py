"""Admin helpers for WordPress/Keygen dashboard endpoints."""

import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional


def safe_slug(value: str, fallback: str = "merebhub-product") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or fallback


def normalize_keygen_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if isinstance(value, (uuid.UUID, datetime, date, Decimal)):
            data[key] = str(value)
    return data


async def get_columns(conn: Any, table: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1
        """,
        table,
    )
    return {str(row["column_name"]) for row in rows}


def json_metadata(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return json.dumps(parsed)
        except json.JSONDecodeError:
            return json.dumps({"note": value})
    return "{}"


async def insert_dynamic(
    conn: Any,
    table: str,
    columns: Iterable[str],
    values: dict[str, Any],
) -> dict[str, Any]:
    available = await get_columns(conn, table)
    insert_columns = [column for column in columns if column in available]
    args = [values[column] for column in insert_columns]
    placeholders = ", ".join(f"${idx}" for idx in range(1, len(args) + 1))
    quoted_columns = ", ".join(insert_columns)
    row = await conn.fetchrow(
        f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders}) RETURNING *",
        *args,
    )
    return normalize_keygen_row(row)


async def update_dynamic(
    conn: Any,
    table: str,
    row_id: str,
    account_id: str,
    values: dict[str, Any],
) -> Optional[dict[str, Any]]:
    available = await get_columns(conn, table)
    update_values = {key: value for key, value in values.items() if key in available}
    if "updated_at" in available:
        update_values["updated_at"] = datetime.utcnow()
    if not update_values:
        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1 AND account_id = $2", uuid.UUID(row_id), uuid.UUID(account_id))
        return normalize_keygen_row(row) if row else None
    assignments = ", ".join(f"{column} = ${idx}" for idx, column in enumerate(update_values, start=3))
    row = await conn.fetchrow(
        f"UPDATE {table} SET {assignments} WHERE id = $1 AND account_id = $2 RETURNING *",
        uuid.UUID(row_id),
        uuid.UUID(account_id),
        *update_values.values(),
    )
    return normalize_keygen_row(row) if row else None
