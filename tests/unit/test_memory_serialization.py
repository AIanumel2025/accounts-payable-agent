"""M8D unit tests: canonical JSON serialization and Decimal fidelity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.serialization.memory_json import (
    canonical_json_bytes,
    canonical_payload_sha256,
    enum_text,
    memory_json_safe,
    normalized_field_value,
    optional_decimal,
    optional_text,
)

pytestmark = pytest.mark.unit


class _Status(str, Enum):
    SUCCEEDED = "SUCCEEDED"


@dataclass(frozen=True)
class _Inner:
    value: Decimal


@dataclass(frozen=True)
class _Outer:
    status: _Status
    inner: _Inner
    items: tuple[int, ...]


def test_memory_json_safe_preserves_decimal_as_exact_string_never_float():
    result = memory_json_safe(Decimal("69.220"))

    assert result == "69.220"
    assert isinstance(result, str)


def test_memory_json_safe_preserves_decimal_trailing_zeros():
    # format(value, "f") preserves the Decimal's own precision.
    assert memory_json_safe(Decimal("50.10")) == "50.10"
    assert memory_json_safe(Decimal("0.00")) == "0.00"


def test_memory_json_safe_handles_uuid_enum_date_datetime_path():
    identifier = uuid4()
    when = date(2002, 6, 28)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert memory_json_safe(identifier) == str(identifier)
    assert memory_json_safe(_Status.SUCCEEDED) == "SUCCEEDED"
    assert memory_json_safe(when) == "2002-06-28"
    assert memory_json_safe(stamp) == stamp.isoformat()
    assert memory_json_safe(Path("/tmp/x.png")) == "/tmp/x.png"


def test_memory_json_safe_none_stays_none():
    assert memory_json_safe(None) is None


def test_memory_json_safe_dataclass_tuple_list_set_recursively():
    outer = _Outer(status=_Status.SUCCEEDED, inner=_Inner(value=Decimal("1.50")), items=(1, 2, 3))

    result = memory_json_safe(outer)

    assert result == {
        "status": "SUCCEEDED",
        "inner": {"value": "1.50"},
        "items": [1, 2, 3],
    }


def test_memory_json_safe_rejects_unsupported_types():
    class Unsupported:
        pass

    with pytest.raises(TypeError):
        memory_json_safe(Unsupported())


def test_canonical_json_bytes_is_independent_of_dict_key_order():
    a = canonical_json_bytes({"x": 1, "y": 2})
    b = canonical_json_bytes({"y": 2, "x": 1})

    assert a == b


def test_canonical_payload_sha256_matches_manual_sha256():
    import hashlib

    payload = {"a": 1, "b": [1, 2, 3]}
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    assert canonical_payload_sha256(payload) == expected


def test_canonical_payload_sha256_changes_when_content_changes():
    first = canonical_payload_sha256({"total": "69.22"})
    second = canonical_payload_sha256({"total": "69.23"})

    assert first != second


def test_enum_text_and_optional_text():
    assert enum_text(_Status.SUCCEEDED) == "SUCCEEDED"
    assert enum_text("plain") == "plain"
    assert optional_text(None) is None
    assert optional_text("  ") is None
    assert optional_text(_Status.SUCCEEDED) == "SUCCEEDED"


def test_optional_decimal():
    assert optional_decimal(None) is None
    assert optional_decimal("69.22") == Decimal("69.22")
    assert optional_decimal(Decimal("1")) == Decimal("1")


def test_normalized_field_value_finds_matching_field_and_defaults_to_none():
    @dataclass
    class _Field:
        field_name: str
        normalized_value: object

    @dataclass
    class _Record:
        fields: tuple

    record = _Record(fields=(_Field(field_name="INVOICE_NUMBER", normalized_value="308044"),))

    assert normalized_field_value(record, "INVOICE_NUMBER") == "308044"
    assert normalized_field_value(record, "TOTAL_AMOUNT") is None
