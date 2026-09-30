"""Upload validation for the M11D operations console.

New in M11D. Pure functions (no I/O beyond the byte sequences handed in, no
heavy imports): filename safety, extension/declared-media-type/file-signature
agreement, and structural sanity checks for the three supported document
types (PDF, PNG, JPEG) that are detectable without decoding the image.

Every rejection is an `OperationsRequestRejectedError` carrying a stable,
input-free code -- never the submitted name or bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.operations import UploadLimits

__all__ = [
    "EXTENSION_MEDIA_TYPES",
    "ValidatedUploadName",
    "validate_upload_filename",
    "detect_media_type",
    "validate_upload_content",
    "FORBIDDEN_FIELD_PATTERN",
    "reject_forbidden_form_fields",
]


EXTENSION_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_SAFE_NAME_PATTERN = re.compile(r"^[\w][\w .()\-\[\]]*$", flags=re.UNICODE)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND_TAIL = b"IEND\xaeB`\x82"

# Payment-, bank- and ERP-shaped form fields/actions are never part of an
# upload submission (M11D task §17 N / the repository's no-payment rule).
FORBIDDEN_FIELD_PATTERN = re.compile(
    r"(pay|bank|erp|iban|swift|routing|account[_-]?number|transfer|wire|post[_-]?to)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidatedUploadName:
    display_name: str
    extension: str
    media_type: str


def validate_upload_filename(raw_filename: Optional[str], limits: UploadLimits) -> ValidatedUploadName:
    """The submitted filename is only ever a *display label*; storage paths
    are generated from controlled identities. Anything that could be read as
    a path component (separators, `..`, NUL/control characters, a leading
    dot or space) is rejected rather than "cleaned", so there is nothing
    ambiguous to reason about downstream."""

    if raw_filename is None or not raw_filename.strip():
        raise OperationsRequestRejectedError("FILENAME_MISSING")

    if len(raw_filename) > limits.maximum_filename_length:
        raise OperationsRequestRejectedError("FILENAME_TOO_LONG")

    if any(ord(character) < 32 or ord(character) == 127 for character in raw_filename):
        raise OperationsRequestRejectedError("FILENAME_INVALID")

    if "/" in raw_filename or "\\" in raw_filename or ".." in raw_filename or raw_filename != raw_filename.strip():
        raise OperationsRequestRejectedError("FILENAME_INVALID")

    if _SAFE_NAME_PATTERN.fullmatch(raw_filename) is None:
        raise OperationsRequestRejectedError("FILENAME_INVALID")

    dot_index = raw_filename.rfind(".")
    extension = raw_filename[dot_index:].lower() if dot_index > 0 else ""

    if extension not in limits.allowed_extensions or extension not in EXTENSION_MEDIA_TYPES:
        raise OperationsRequestRejectedError("UNSUPPORTED_FILE_TYPE")

    return ValidatedUploadName(
        display_name=raw_filename, extension=extension, media_type=EXTENSION_MEDIA_TYPES[extension]
    )


def detect_media_type(head: bytes) -> Optional[str]:
    if head.startswith(b"%PDF-"):
        return "application/pdf"

    if head.startswith(_PNG_SIGNATURE):
        return "image/png"

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    return None


def validate_upload_content(
    *,
    name: ValidatedUploadName,
    declared_media_type: Optional[str],
    head: bytes,
    tail: bytes,
    size: int,
    limits: UploadLimits,
) -> None:
    """Extension, declared media type and file signature must all agree,
    and the file must look structurally complete."""

    if size <= 0:
        raise OperationsRequestRejectedError("EMPTY_FILE")

    if size > limits.maximum_file_bytes:
        raise OperationsRequestRejectedError("FILE_TOO_LARGE", http_status=413)

    declared = (declared_media_type or "").split(";")[0].strip().lower()

    if declared and declared != name.media_type:
        raise OperationsRequestRejectedError("MEDIA_TYPE_MISMATCH", http_status=415)

    detected = detect_media_type(head)

    if detected is None:
        raise OperationsRequestRejectedError("UNSUPPORTED_FILE_TYPE", http_status=415)

    if detected != name.media_type:
        raise OperationsRequestRejectedError("FILE_SIGNATURE_MISMATCH", http_status=415)

    if detected == "application/pdf":
        if b"%%EOF" not in tail:
            raise OperationsRequestRejectedError("MALFORMED_FILE")
    elif detected == "image/png":
        if size < len(_PNG_SIGNATURE) + 25 or head[12:16] != b"IHDR" or not tail.rstrip(b"\x00").endswith(_PNG_IEND_TAIL):
            raise OperationsRequestRejectedError("MALFORMED_FILE")
    else:  # image/jpeg
        if size < 4 or not tail.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise OperationsRequestRejectedError("MALFORMED_FILE")


def reject_forbidden_form_fields(field_names: list[str]) -> None:
    """Only the `file` field is accepted. Any other field is refused before
    anything is stored; payment/bank/ERP-shaped names get their own code."""

    for field_name in field_names:
        if FORBIDDEN_FIELD_PATTERN.search(field_name):
            raise OperationsRequestRejectedError("PAYMENT_FIELD_PROHIBITED")

    for field_name in field_names:
        if field_name != "file":
            raise OperationsRequestRejectedError("FIELD_NOT_ALLOWED")
