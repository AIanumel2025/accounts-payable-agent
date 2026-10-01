"""Test-only Clerk stand-in: an RSA key pair, a static JWKS document and a
token minter. No network, no real Clerk instance, no real identifier."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ap_agent.auth.clerk import ClerkTokenVerifier, JwksCache
from ap_agent.config.deployment import ClerkConfig

ISSUER = "https://clerk.example.test"
PARTY = "https://app.example.test"


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class TestKey:
    __test__ = False

    def __init__(self, kid: str = "key-1") -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.public_pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def jwk(self) -> dict[str, Any]:
        numbers = self.private.public_key().public_numbers()
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid, "n": _b64(numbers.n), "e": _b64(numbers.e)}

    def mint(
        self,
        *,
        user: str = "user_test_1",
        org: Optional[str] = "org_test_1",
        issuer: str = ISSUER,
        azp: Optional[str] = PARTY,
        expires_in: int = 60,
        not_before_offset: int = 0,
        layout: str = "v2",
        extra: Optional[dict[str, Any]] = None,
        algorithm: str = "RS256",
        kid: Optional[str] = "default",
        key: Any = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": user,
            "iss": issuer,
            "iat": now,
            "nbf": now + not_before_offset,
            "exp": now + expires_in,
            "sid": "sess_test_1",
        }

        if azp is not None:
            claims["azp"] = azp

        if org is not None:
            if layout == "v2":
                claims["o"] = {"id": org, "rol": "admin", "slg": "test-org"}
            else:
                claims["org_id"] = org
                claims["org_role"] = "org:admin"

        claims.update(extra or {})
        headers = {} if kid is None else {"kid": self.kid if kid == "default" else kid}

        return jwt.encode(claims, key or self.private_pem, algorithm=algorithm, headers=headers)


def clerk_config(**overrides: Any) -> ClerkConfig:
    values: dict[str, Any] = {
        "issuer": ISSUER,
        "jwks_url": f"{ISSUER}/.well-known/jwks.json",
        "authorized_parties": (PARTY,),
        "leeway_seconds": 0,
    }
    values.update(overrides)
    return ClerkConfig(**values)


def static_verifier(*keys: TestKey, config: Optional[ClerkConfig] = None) -> ClerkTokenVerifier:
    document = {"keys": [key.jwk() for key in keys]}
    resolved = config or clerk_config()
    return ClerkTokenVerifier(resolved, jwks=JwksCache(resolved, fetcher=lambda _url, _timeout: document))


def tampered(token: str) -> str:
    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["sub"] = "user_attacker"
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"
