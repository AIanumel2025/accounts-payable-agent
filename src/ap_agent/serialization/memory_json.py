"""Canonical JSON serialization for Phase 7 operational memory.

Source: notebook cell 88 ("PHASE 7 — REPLACEMENT CELL 5"), the active
`memory_json_safe`, `canonical_json_bytes`, `enum_text`, `optional_text`,
`optional_decimal` and `normalized_field_value` functions, extracted
verbatim (they are pure, dependency-free functions: no config, no I/O, no
notebook globals).

These are the building blocks the notebook used to:
  - turn any dataclass/Enum/UUID/Decimal/date/datetime/Path graph into a
    JSON-safe `dict`/`list`/scalar tree (`memory_json_safe`), preserving
    Decimal precision (`format(value, "f")`, never `float(...)` — task §6.4:
    "Never convert monetary Decimal values through float"), UUID identity,
    enum values, and ISO-8601 timestamps;
  - hash that tree deterministically regardless of key insertion order
    (`canonical_json_bytes`, `sort_keys=True`), which is what makes
    `sha256(canonical_json_bytes(payload))` a stable content hash across
    Python dict-construction order and across a database round trip.

`ap_agent.repositories.mapping` and `ap_agent.services.memory_service`
both depend on this module (task §2 dependency direction:
"models/configuration -> serialization and database foundation ->
repository -> memory service").
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

__all__ = [
    "memory_json_safe",
    "canonical_json_bytes",
    "canonical_payload_sha256",
    "enum_text",
    "optional_text",
    "optional_decimal",
    "normalized_field_value",
]


def memory_json_safe(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, Enum):
        return memory_json_safe(value.value)

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return format(value, "f")

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value):
        return {
            field_definition.name: memory_json_safe(
                getattr(value, field_definition.name)
            )
            for field_definition in fields(value)
        }

    if isinstance(value, dict):
        return {str(key): memory_json_safe(item_value) for key, item_value in value.items()}

    if isinstance(value, (tuple, list, set)):
        return [memory_json_safe(item) for item in value]

    if isinstance(value, (str, int, float, bool)):
        return value

    raise TypeError(f"Unsupported memory value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_payload_sha256(value: Any) -> str:
    """`sha256(canonical_json_bytes(value)).hexdigest()`.

    New in M8: the notebook computed this inline at each of its three call
    sites (`payload_sha256 = sha256(canonical_json_bytes(complete_payload)).hexdigest()`
    in `build_invoice_memory_record`, and again, identically, in
    `retrieve_matched_invoice_memory`'s `recalculated_hash`); named here so
    both `ap_agent.services.memory_service` call sites share one
    implementation instead of repeating the two-call idiom.
    """

    from hashlib import sha256

    return sha256(canonical_json_bytes(value)).hexdigest()


def enum_text(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)

    return str(value)


def optional_text(value: Any) -> str | None:
    if value is None:
        return None

    text = enum_text(value).strip()

    return text or None


def optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None

    return Decimal(str(value))


def normalized_field_value(invoice_record: Any, requested_field_name: str) -> Any:
    for normalized_field in invoice_record.fields:
        field_name = enum_text(normalized_field.field_name)

        if field_name == requested_field_name:
            return normalized_field.normalized_value

    return None
