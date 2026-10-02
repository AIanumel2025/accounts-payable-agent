"""Independent verification of Clerk session tokens (M11E).

FastAPI never trusts a verification performed by Next.js: every request's
bearer token is verified here against the issuer's published signing keys.

Checked on every token: RS256 only (any other algorithm, including `none`
and symmetric algorithms, is refused before any key is used), signature,
issuer, `exp`, `nbf` and `iat` (all required, with a small leeway), a
non-empty subject, the authorized party (`azp`) against the configured
front-end origins, the audience when one is configured, and an active
organization. Failures raise `ClerkTokenError` carrying a stable code and
never any part of the token or its payload.

Signing keys are fetched from the JWKS endpoint, cached for a bounded time
and refreshed when an unknown key id appears (key rotation), with a minimum
interval between refreshes so a stream of forged key ids cannot make the API
hammer the identity provider. If a refresh fails, still-cached keys keep
verifying; an unknown key id then fails closed as
`AuthenticationUnavailableError`.

`PyJWT` (with `cryptography`) is imported lazily, only when a token is
verified.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from ap_agent.config.deployment import ClerkConfig
from ap_agent.exceptions import AuthenticationUnavailableError

__all__ = [
    "ALLOWED_ALGORITHMS",
    "ClerkTokenError",
    "VerifiedClerkSession",
    "JwksCache",
    "ClerkTokenVerifier",
    "fetch_jwks_document",
]

ALLOWED_ALGORITHMS = ("RS256",)

_MAXIMUM_JWKS_BYTES = 256 * 1024
_MAXIMUM_TOKEN_CHARACTERS = 8192


class ClerkTokenError(Exception):
    """The token was rejected. `code` is stable and safe to return."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedClerkSession:
    # Identifiers are excluded from repr(): Clerk IDs are never logged.
    user_id: str = field(repr=False)
    organization_id: str = field(repr=False)
    session_id: Optional[str] = field(repr=False)
    issued_at: datetime
    expires_at: datetime


def fetch_jwks_document(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    """Default JWKS fetcher: one bounded HTTP GET with a size cap."""

    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - scheme validated by config
        body = response.read(_MAXIMUM_JWKS_BYTES + 1)

    if len(body) > _MAXIMUM_JWKS_BYTES:
        raise ValueError("JWKS document too large")

    document = json.loads(body)

    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("JWKS document malformed")

    return document


class JwksCache:
    def __init__(
        self,
        config: ClerkConfig,
        *,
        fetcher: Optional[Callable[[str, float], Mapping[str, Any]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._fetcher = fetcher or fetch_jwks_document
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: Optional[float] = None
        self._last_attempt_at: Optional[float] = None
        self._last_attempt_ok = False

    def _refresh(self, now: float) -> None:
        from jwt import PyJWK

        self._last_attempt_at = now
        document = self._fetcher(self._config.jwks_url, self._config.jwks_timeout_seconds)
        keys: dict[str, Any] = {}

        for entry in document.get("keys", []):
            if not isinstance(entry, dict) or entry.get("kty") != "RSA" or not entry.get("kid"):
                continue

            if entry.get("use", "sig") != "sig":
                continue

            try:
                keys[str(entry["kid"])] = PyJWK.from_dict(entry, algorithm="RS256")
            except Exception:  # noqa: BLE001 - one bad key must not poison the set
                continue

        if not keys:
            raise ValueError("JWKS contained no usable signing key")

        self._keys = keys
        self._fetched_at = now

    def get_key(self, key_id: str) -> Any:
        with self._lock:
            now = self._clock()
            fresh = self._fetched_at is not None and (now - self._fetched_at) < self._config.jwks_cache_seconds

            if fresh and key_id in self._keys:
                return self._keys[key_id].key

            may_refresh = (
                self._last_attempt_at is None
                or (now - self._last_attempt_at) >= self._config.jwks_minimum_refresh_seconds
            )

            if may_refresh:
                try:
                    self._refresh(now)
                    self._last_attempt_ok = True
                except Exception:  # noqa: BLE001 - fall back to still-cached keys below
                    self._last_attempt_ok = False

            entry = self._keys.get(key_id)

            if entry is not None:
                return entry.key

            if self._last_attempt_ok:
                # The key set was fetched recently and does not contain this id.
                raise ClerkTokenError("TOKEN_SIGNING_KEY_UNKNOWN")

            raise AuthenticationUnavailableError("signing keys unavailable")


class ClerkTokenVerifier:
    def __init__(
        self,
        config: ClerkConfig,
        *,
        jwks: Optional[JwksCache] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._jwks = jwks or JwksCache(config)
        self._clock = clock

    def verify(self, token: str) -> VerifiedClerkSession:
        import jwt

        if not token or len(token) > _MAXIMUM_TOKEN_CHARACTERS:
            raise ClerkTokenError("TOKEN_MALFORMED")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise ClerkTokenError("TOKEN_MALFORMED") from error

        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise ClerkTokenError("TOKEN_ALGORITHM_NOT_ALLOWED")

        key_id = header.get("kid")

        if not isinstance(key_id, str) or not key_id:
            raise ClerkTokenError("TOKEN_MALFORMED")

        key = self._jwks.get_key(key_id)

        options: dict[str, Any] = {
            "require": ["exp", "nbf", "iat", "sub", "iss"],
            "verify_aud": self._config.audience is not None,
        }

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self._config.issuer,
                audience=self._config.audience,
                leeway=self._config.leeway_seconds,
                options=options,
            )
        except jwt.ExpiredSignatureError as error:
            raise ClerkTokenError("TOKEN_EXPIRED") from error
        except jwt.ImmatureSignatureError as error:
            raise ClerkTokenError("TOKEN_NOT_YET_VALID") from error
        except jwt.InvalidIssuerError as error:
            raise ClerkTokenError("TOKEN_ISSUER_INVALID") from error
        except jwt.InvalidAudienceError as error:
            raise ClerkTokenError("TOKEN_AUDIENCE_INVALID") from error
        except jwt.InvalidSignatureError as error:
            raise ClerkTokenError("TOKEN_SIGNATURE_INVALID") from error
        except jwt.PyJWTError as error:
            raise ClerkTokenError("TOKEN_INVALID") from error

        authorized_party = claims.get("azp")

        if not isinstance(authorized_party, str) or authorized_party.rstrip("/") not in {
            party.rstrip("/") for party in self._config.authorized_parties
        }:
            raise ClerkTokenError("TOKEN_AUTHORIZED_PARTY_INVALID")

        user_id = claims.get("sub")

        if not isinstance(user_id, str) or not user_id.strip():
            raise ClerkTokenError("TOKEN_SUBJECT_MISSING")

        organization_id = _organization_id(claims)

        if organization_id is None:
            raise ClerkTokenError("ORGANIZATION_REQUIRED")

        session_id = claims.get("sid")

        return VerifiedClerkSession(
            user_id=user_id,
            organization_id=organization_id,
            session_id=session_id if isinstance(session_id, str) else None,
            issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
        )


def _organization_id(claims: Mapping[str, Any]) -> Optional[str]:
    """Active organization from either session-token layout: version 2
    (`o: {"id": ...}`) or version 1 (`org_id`). Roles and metadata in the
    token are deliberately never read."""

    organization = claims.get("o")

    if isinstance(organization, dict):
        value = organization.get("id")
    else:
        value = claims.get("org_id")

    return value if isinstance(value, str) and value.strip() else None
