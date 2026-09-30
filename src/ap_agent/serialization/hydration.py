"""Typed hydration of persisted phase payloads (M11D Core).

`memory_json_safe` (`ap_agent.serialization.memory_json`) turns the typed
Phase 4/5/6 results into the JSON stored in `invoice_memory_records`.
This module is its explicit inverse: a payload is rebuilt into the *same
frozen dataclasses* using the models' own type hints -- never by passing an
arbitrary dictionary into a typed handler.

Fail-closed by construction: a missing required key, an unknown key, an
unparsable value or a wrong container raises `HydrationError`. Decimals are
rebuilt from their exact string (never through `float`), so a hydrated
object re-serialises to byte-identical JSON; callers verify exactly that
(`assert_round_trip`).

Only the one genuinely ambiguous union in these models
(`NormalizedValue = str | date | Decimal | None`) needs help: it is decided
by the sibling `value_type` discriminator, never by guessing from the text.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from ap_agent.models.matching import MatchingResult
from ap_agent.models.normalization import (
    InvoiceFieldCandidate,
    NormalizationResult,
    NormalizedInvoiceField,
    NormalizedValueType,
)
from ap_agent.models.validation import FinancialValidationResult
from ap_agent.serialization.memory_json import memory_json_safe

__all__ = [
    "HydrationError",
    "hydrate_dataclass",
    "hydrate_normalization_result",
    "hydrate_financial_validation_result",
    "hydrate_matching_result",
    "assert_round_trip",
]


class HydrationError(ValueError):
    """A persisted payload cannot be rebuilt into its typed model."""


_DISCRIMINATED_VALUE_FIELDS = {
    NormalizedInvoiceField: "normalized_value",
    InvoiceFieldCandidate: "proposed_value",
}


def _hydrate_discriminated_value(value: Any, value_type: NormalizedValueType) -> Any:
    if value is None:
        return None

    if not isinstance(value, str):
        raise HydrationError("A normalized value is not a string.")

    if value_type == NormalizedValueType.DECIMAL:
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise HydrationError("A decimal value is malformed.") from error

    if value_type == NormalizedValueType.DATE:
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise HydrationError("A date value is malformed.") from error

    return value


def _hydrate(annotation: Any, value: Any) -> Any:
    if annotation is Any:
        return value

    origin = get_origin(annotation)

    if origin in (Union, types.UnionType):
        arguments = get_args(annotation)

        if value is None:
            if type(None) in arguments:
                return None
            raise HydrationError("A required value is null.")

        candidates = [argument for argument in arguments if argument is not type(None)]

        if len(candidates) != 1:
            raise HydrationError("An ambiguous union cannot be hydrated.")

        return _hydrate(candidates[0], value)

    if origin is tuple:
        if not isinstance(value, list):
            raise HydrationError("A tuple value is not a list.")

        arguments = get_args(annotation)

        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_hydrate(arguments[0], item) for item in value)

        if len(arguments) != len(value):
            raise HydrationError("A fixed-size tuple has the wrong length.")

        return tuple(_hydrate(argument, item) for argument, item in zip(arguments, value))

    if origin is dict:
        if not isinstance(value, dict):
            raise HydrationError("A mapping value is not an object.")

        key_type, value_type = get_args(annotation)
        return {_hydrate(key_type, key): _hydrate(value_type, item) for key, item in value.items()}

    if annotation is type(None):
        if value is not None:
            raise HydrationError("A null value is expected.")
        return None

    if isinstance(annotation, type):
        if dataclasses.is_dataclass(annotation):
            return hydrate_dataclass(annotation, value)

        if issubclass(annotation, Enum):
            try:
                return annotation(value)
            except ValueError as error:
                raise HydrationError(f"Unknown {annotation.__name__} value.") from error

        if annotation is UUID:
            try:
                return UUID(str(value))
            except ValueError as error:
                raise HydrationError("A UUID is malformed.") from error

        if annotation is Decimal:
            if not isinstance(value, str):
                raise HydrationError("A decimal is not stored as a string.")
            try:
                return Decimal(value)
            except InvalidOperation as error:
                raise HydrationError("A decimal is malformed.") from error

        if annotation is datetime:
            try:
                return datetime.fromisoformat(value)
            except (TypeError, ValueError) as error:
                raise HydrationError("A datetime is malformed.") from error

        if annotation is date:
            try:
                return date.fromisoformat(value)
            except (TypeError, ValueError) as error:
                raise HydrationError("A date is malformed.") from error

        if annotation is bool:
            if not isinstance(value, bool):
                raise HydrationError("A boolean is malformed.")
            return value

        if annotation is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise HydrationError("An integer is malformed.")
            return value

        if annotation is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HydrationError("A float is malformed.")
            return value

        if annotation is str:
            if not isinstance(value, str):
                raise HydrationError("A string is malformed.")
            return value

    raise HydrationError(f"Unsupported annotation {annotation!r}.")


def hydrate_dataclass(cls: type, payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise HydrationError(f"{cls.__name__} payload is not an object.")

    hints = typing.get_type_hints(cls)
    fields = {field.name: field for field in dataclasses.fields(cls) if field.init}

    unknown = set(payload) - set(fields)

    if unknown:
        raise HydrationError(f"{cls.__name__} payload has unknown keys.")

    discriminated = _DISCRIMINATED_VALUE_FIELDS.get(cls)
    arguments: dict[str, Any] = {}

    for name, field in fields.items():
        if name not in payload:
            if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
                raise HydrationError(f"{cls.__name__} payload is missing a required key.")
            continue

        if name == discriminated:
            arguments[name] = _hydrate_discriminated_value(
                payload[name], NormalizedValueType(payload["value_type"])
            )
            continue

        arguments[name] = _hydrate(hints[name], payload[name])

    return cls(**arguments)


def assert_round_trip(instance: Any, payload: Any) -> None:
    """The hydrated object must serialise back to exactly the stored
    payload, otherwise something was lost or altered in hydration."""

    if memory_json_safe(instance) != payload:
        raise HydrationError("Hydrated payload does not round-trip to the stored payload.")


def hydrate_normalization_result(payload: Any) -> NormalizationResult:
    result = hydrate_dataclass(NormalizationResult, payload)
    assert_round_trip(result, payload)
    return result


def hydrate_financial_validation_result(payload: Any) -> FinancialValidationResult:
    result = hydrate_dataclass(FinancialValidationResult, payload)
    assert_round_trip(result, payload)
    return result


def hydrate_matching_result(payload: Any) -> MatchingResult:
    result = hydrate_dataclass(MatchingResult, payload)
    assert_round_trip(result, payload)
    return result
