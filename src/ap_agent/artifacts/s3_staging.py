"""Direct browser-to-S3 upload staging (M11E.1, AWS deployment).

Lambda cannot accept the application's 10 MB invoice in a synchronous request,
so the browser uploads straight to S3 with a short-lived presigned POST and
then asks the API to *finalize*. Nothing here is trusted until finalization
re-validates it.

Staging objects live under a prefix that is separate from immutable invoice
objects and expires automatically (bucket lifecycle rule):

    staging/<tenant-hex>/<intent-hex>

The key is built only from the authenticated tenant and a server-generated
random intent id: no customer file name, no client-chosen path. The presigned
POST policy pins the exact bucket, exact key, exact content type, exact
content length and the object metadata that finalization later checks, so the
browser cannot change any of them. The metadata binds the intent to the
authenticated tenant and actor, the declared SHA-256 and size, the (encoded)
display name and the time after which finalization is refused. Single use is
enforced by finalization itself (one deterministic idempotency key per intent,
and the staging object is deleted once its job exists).

`boto3` is not imported here; the store takes any S3 client exposing
`generate_presigned_post`, `head_object`, `get_object`, `delete_object`.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from ap_agent.exceptions import OperationsRequestRejectedError

__all__ = [
    "STAGING_PREFIX",
    "UPLOAD_POLICY_LIFETIME",
    "INTENT_FINALIZE_WINDOW",
    "SHA256_PATTERN",
    "UploadIntent",
    "StagedObject",
    "S3UploadStaging",
    "staging_key",
    "encode_display_name",
    "decode_display_name",
]

STAGING_PREFIX = "staging"

# The browser has this long to start the upload; it has the (longer) finalize
# window to call the API afterwards. Both are short on purpose.
UPLOAD_POLICY_LIFETIME = timedelta(minutes=5)
INTENT_FINALIZE_WINDOW = timedelta(minutes=30)

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def staging_key(tenant_hex: str, intent_hex: str) -> str:
    if not _HEX32.fullmatch(tenant_hex) or not _HEX32.fullmatch(intent_hex):
        raise ValueError("Staging key components must be 32 lowercase hex characters.")

    return f"{STAGING_PREFIX}/{tenant_hex}/{intent_hex}"


def encode_display_name(name: str) -> str:
    """S3 user metadata must be ASCII: carry the (already validated) display
    name as URL-safe base64."""

    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def decode_display_name(value: str) -> Optional[str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        return None


@dataclass(frozen=True)
class UploadIntent:
    intent_id: UUID
    url: str
    fields: dict[str, str]
    upload_expires_at: datetime
    finalize_expires_at: datetime


@dataclass(frozen=True)
class StagedObject:
    size: int
    content_type: str
    metadata: dict[str, str]


def _is_missing(error: BaseException) -> bool:
    response = getattr(error, "response", None)

    if not isinstance(response, dict):
        return False

    return (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        or response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}
    )


class S3UploadStaging:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    # -- intent ----------------------------------------------------------

    def create_intent(
        self,
        *,
        tenant_id: UUID,
        actor_id: str,
        intent_id: UUID,
        display_name: str,
        media_type: str,
        byte_size: int,
        sha256: str,
        now: datetime,
    ) -> UploadIntent:
        key = staging_key(tenant_id.hex, intent_id.hex)
        finalize_expires_at = now + INTENT_FINALIZE_WINDOW
        upload_expires_at = now + UPLOAD_POLICY_LIFETIME

        metadata = {
            "tenant": tenant_id.hex,
            "actor": actor_id,
            "intent": intent_id.hex,
            "sha256": sha256,
            "size": str(byte_size),
            "filename": encode_display_name(display_name),
            "expires": str(int(finalize_expires_at.timestamp())),
        }
        fields = {"Content-Type": media_type, **{f"x-amz-meta-{name}": value for name, value in metadata.items()}}
        # boto3 adds the exact `bucket` and `key` conditions itself.
        conditions: list[Any] = [
            ["content-length-range", byte_size, byte_size],
            *({name: value} for name, value in fields.items()),
        ]

        try:
            post = self._client.generate_presigned_post(
                Bucket=self._bucket,
                Key=key,
                Fields=fields,
                Conditions=conditions,
                ExpiresIn=int(UPLOAD_POLICY_LIFETIME.total_seconds()),
            )
        except Exception as error:  # noqa: BLE001 - never leak the message
            raise OperationsRequestRejectedError("STORAGE_UNAVAILABLE", http_status=503) from error

        return UploadIntent(
            intent_id=intent_id,
            url=str(post["url"]),
            fields={str(name): str(value) for name, value in post["fields"].items()},
            upload_expires_at=upload_expires_at,
            finalize_expires_at=finalize_expires_at,
        )

    # -- finalization ----------------------------------------------------

    def head(self, tenant_hex: str, intent_hex: str) -> Optional[StagedObject]:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=staging_key(tenant_hex, intent_hex))
        except Exception as error:  # noqa: BLE001
            if _is_missing(error):
                return None

            raise OperationsRequestRejectedError("STORAGE_UNAVAILABLE", http_status=503) from error

        return StagedObject(
            size=int(response.get("ContentLength", -1)),
            content_type=str(response.get("ContentType", "")),
            metadata={str(name).lower(): str(value) for name, value in (response.get("Metadata") or {}).items()},
        )

    def open_body(self, tenant_hex: str, intent_hex: str) -> Any:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=staging_key(tenant_hex, intent_hex))["Body"]
        except Exception as error:  # noqa: BLE001
            if _is_missing(error):
                raise OperationsRequestRejectedError("UPLOAD_INTENT_NOT_FOUND", http_status=404) from error

            raise OperationsRequestRejectedError("STORAGE_UNAVAILABLE", http_status=503) from error

    def delete(self, tenant_hex: str, intent_hex: str) -> None:
        """Best effort: the lifecycle rule removes whatever this misses."""

        try:
            self._client.delete_object(Bucket=self._bucket, Key=staging_key(tenant_hex, intent_hex))
        except Exception:  # noqa: BLE001
            return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
