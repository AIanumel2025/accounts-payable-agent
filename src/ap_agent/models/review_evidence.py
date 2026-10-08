"""Evidence choices offered to a human reviewer when correcting a case (M11E.5).

A correction must cite evidence (`require_correction_evidence`). Until M11E.5 the only choices were opaque UUIDs taken from
already-extracted fields and financial checks, so a reviewer could not tell which UUID supported which value, and a value the
extractor never found (a missing supplier name) had nothing to cite. This module defines

  * `ReviewEvidenceOption` -- a safe, structured description of one selectable evidence reference (id, type, human label,
    optional page number and a short sanitised text snippet), and
  * the case-bound **source-document** evidence reference: a stable id derived from the case's document identity and the
    stored SHA-256 of the original invoice. It is valid only for that document: another document, or the same document with a
    different hash, derives a different id, so a forged or cross-case id is simply not in the case's available set.

Nothing here carries or derives from an object-store key, filesystem path, host name, credential or tenant identifier. The
derived id is a one-way UUIDv5, so it does not reveal the hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

__all__ = [
    "EVIDENCE_TYPE_EXTRACTED_FIELD",
    "EVIDENCE_TYPE_FINANCIAL_CHECK",
    "EVIDENCE_TYPE_SOURCE_DOCUMENT",
    "SOURCE_DOCUMENT_LABEL",
    "SNIPPET_MAXIMUM_LENGTH",
    "ReviewEvidenceOption",
    "source_document_evidence_id",
    "source_document_evidence_option",
    "sanitize_snippet",
    "field_evidence_option",
    "financial_check_evidence_option",
    "generic_evidence_option",
]

EVIDENCE_TYPE_SOURCE_DOCUMENT = "SOURCE_DOCUMENT"
EVIDENCE_TYPE_EXTRACTED_FIELD = "EXTRACTED_FIELD"
EVIDENCE_TYPE_FINANCIAL_CHECK = "FINANCIAL_CHECK"

SOURCE_DOCUMENT_LABEL = "Original source invoice — SHA-256 verified"
SNIPPET_MAXIMUM_LENGTH = 80

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


@dataclass(frozen=True)
class ReviewEvidenceOption:
    reference_id: str
    evidence_type: str
    label: str
    page_number: Optional[int] = None
    snippet: Optional[str] = None


def source_document_evidence_id(document_id: UUID, source_document_sha256: str) -> Optional[str]:
    """The case-bound source-document evidence id, or None when the stored hash is not a SHA-256 (nothing is invented)."""

    if not isinstance(source_document_sha256, str) or _SHA256.fullmatch(source_document_sha256.strip()) is None:
        return None

    return str(uuid5(NAMESPACE_URL, f"ap-agent/source-document-evidence/{document_id}/{source_document_sha256.strip().lower()}"))


def source_document_evidence_option(document_id: UUID, source_document_sha256: str) -> Optional[ReviewEvidenceOption]:
    reference_id = source_document_evidence_id(document_id, source_document_sha256)

    if reference_id is None:
        return None

    return ReviewEvidenceOption(
        reference_id=reference_id, evidence_type=EVIDENCE_TYPE_SOURCE_DOCUMENT, label=SOURCE_DOCUMENT_LABEL
    )


def sanitize_snippet(text: Any) -> Optional[str]:
    """A short single-line excerpt of OCR text: control characters removed, whitespace collapsed, length capped."""

    if not isinstance(text, str):
        return None

    cleaned = " ".join(_CONTROL.sub(" ", text).split())

    if not cleaned:
        return None

    return cleaned if len(cleaned) <= SNIPPET_MAXIMUM_LENGTH else cleaned[: SNIPPET_MAXIMUM_LENGTH - 1].rstrip() + "…"


def _field_label(field_name: str) -> str:
    return " ".join(word.capitalize() for word in field_name.split("_") if word)


def field_evidence_option(reference_id: str, *, field_name: str, page_number: Any, raw_text: Any) -> ReviewEvidenceOption:
    page = page_number if isinstance(page_number, int) and not isinstance(page_number, bool) and page_number >= 1 else None
    label = f"Extracted evidence — {_field_label(field_name)}" + (f", page {page}" if page is not None else "")

    return ReviewEvidenceOption(
        reference_id=reference_id,
        evidence_type=EVIDENCE_TYPE_EXTRACTED_FIELD,
        label=label,
        page_number=page,
        snippet=sanitize_snippet(raw_text),
    )


def financial_check_evidence_option(reference_id: str) -> ReviewEvidenceOption:
    return ReviewEvidenceOption(
        reference_id=reference_id, evidence_type=EVIDENCE_TYPE_FINANCIAL_CHECK, label="Evidence for a financial check"
    )


def generic_evidence_option(reference_id: str) -> ReviewEvidenceOption:
    """Fallback for a reference id that has no stored description (e.g. a context built without options)."""

    return ReviewEvidenceOption(reference_id=reference_id, evidence_type=EVIDENCE_TYPE_EXTRACTED_FIELD, label="Evidence reference")
