"""M11E: independent Clerk session-token verification (no network)."""

from __future__ import annotations

import pytest

from ap_agent.auth.clerk import ClerkTokenError, ClerkTokenVerifier, JwksCache
from ap_agent.exceptions import AuthenticationUnavailableError
from tests.support.m11e_auth import ISSUER, PARTY, TestKey, clerk_config, static_verifier, tampered

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def key() -> TestKey:
    return TestKey()


@pytest.fixture(scope="module")
def other_key() -> TestKey:
    return TestKey(kid="key-2")


def _reject(verifier: ClerkTokenVerifier, token: str) -> str:
    with pytest.raises(ClerkTokenError) as caught:
        verifier.verify(token)

    return caught.value.code


def test_valid_token_yields_user_and_organization(key):
    session = static_verifier(key).verify(key.mint(user="user_a", org="org_a"))
    assert (session.user_id, session.organization_id) == ("user_a", "org_a")


def test_version_one_organization_claim_is_accepted(key):
    session = static_verifier(key).verify(key.mint(layout="v1", org="org_legacy"))
    assert session.organization_id == "org_legacy"


def test_identifiers_are_not_in_repr(key):
    session = static_verifier(key).verify(key.mint(user="user_secretish", org="org_secretish"))
    assert "secretish" not in repr(session)


def test_tampered_payload_is_rejected(key):
    assert _reject(static_verifier(key), tampered(key.mint())) == "TOKEN_SIGNATURE_INVALID"


def test_token_signed_by_another_key_is_rejected(key, other_key):
    forged = other_key.mint(kid=key.kid)
    assert _reject(static_verifier(key), forged) == "TOKEN_SIGNATURE_INVALID"


def test_expired_token_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(expires_in=-10)) == "TOKEN_EXPIRED"


def test_not_yet_valid_token_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(not_before_offset=300)) == "TOKEN_NOT_YET_VALID"


def test_wrong_issuer_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(issuer="https://evil.example.test")) == "TOKEN_ISSUER_INVALID"


def test_wrong_authorized_party_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(azp="https://evil.example.test")) == "TOKEN_AUTHORIZED_PARTY_INVALID"


def test_missing_authorized_party_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(azp=None)) == "TOKEN_AUTHORIZED_PARTY_INVALID"


def test_missing_active_organization_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(org=None)) == "ORGANIZATION_REQUIRED"


def test_audience_is_enforced_when_configured(key):
    verifier = static_verifier(key, config=clerk_config(audience="ap-agent"))
    assert _reject(verifier, key.mint()) == "TOKEN_INVALID"  # audience claim missing
    assert verifier.verify(key.mint(extra={"aud": "ap-agent"})).user_id == "user_test_1"
    assert _reject(verifier, key.mint(extra={"aud": "someone-else"})) == "TOKEN_AUDIENCE_INVALID"


def test_none_algorithm_is_refused(key):
    import base64
    import json

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    token = f"{part({'alg': 'none', 'kid': key.kid})}.{part({'sub': 'x', 'iss': ISSUER})}."
    assert _reject(static_verifier(key), token) == "TOKEN_ALGORITHM_NOT_ALLOWED"


def test_symmetric_algorithm_confusion_is_refused(key):
    import jwt

    token = jwt.encode(
        {"sub": "x", "iss": ISSUER, "azp": PARTY, "iat": 1, "nbf": 1, "exp": 4_000_000_000},
        "a" * 40,
        algorithm="HS256",
        headers={"kid": key.kid},
    )
    assert _reject(static_verifier(key), token) == "TOKEN_ALGORITHM_NOT_ALLOWED"


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "x" * 9000])
def test_malformed_tokens_are_rejected(key, token):
    assert _reject(static_verifier(key), token) == "TOKEN_MALFORMED"


def test_missing_key_id_is_rejected(key):
    assert _reject(static_verifier(key), key.mint(kid=None)) == "TOKEN_MALFORMED"


def test_unknown_key_id_is_rejected_as_invalid_not_unavailable(key):
    assert _reject(static_verifier(key), key.mint(kid="unknown")) == "TOKEN_SIGNING_KEY_UNKNOWN"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_key_rotation_is_picked_up_with_rate_limited_refresh(key, other_key):
    clock = _Clock()
    served = {"keys": [key.jwk()]}
    fetches = []

    def fetcher(url, timeout):
        fetches.append(url)
        return served

    config = clerk_config(jwks_minimum_refresh_seconds=30)
    verifier = ClerkTokenVerifier(config, jwks=JwksCache(config, fetcher=fetcher, clock=clock))

    assert verifier.verify(key.mint()).user_id
    assert len(fetches) == 1

    # The issuer rotates: the new key id is unknown until a refresh.
    served["keys"] = [other_key.jwk()]
    clock.now += 5
    assert _reject(verifier, other_key.mint()) == "TOKEN_SIGNING_KEY_UNKNOWN"  # within the minimum interval
    assert len(fetches) == 1

    clock.now += 60
    assert verifier.verify(other_key.mint()).user_id
    assert len(fetches) == 2
    # The retired key no longer verifies once the new set was fetched.
    clock.now += 31
    assert _reject(verifier, key.mint()) == "TOKEN_SIGNING_KEY_UNKNOWN"


def test_cached_keys_survive_a_failed_refresh(key):
    clock = _Clock()
    state = {"fail": False}

    def fetcher(url, timeout):
        if state["fail"]:
            raise OSError("network down")
        return {"keys": [key.jwk()]}

    config = clerk_config(jwks_cache_seconds=10, jwks_minimum_refresh_seconds=1)
    verifier = ClerkTokenVerifier(config, jwks=JwksCache(config, fetcher=fetcher, clock=clock))
    assert verifier.verify(key.mint()).user_id

    clock.now += 100  # cache expired, issuer unreachable
    state["fail"] = True
    assert verifier.verify(key.mint()).user_id


def test_no_keys_and_unreachable_issuer_fails_closed_as_unavailable(key):
    def fetcher(url, timeout):
        raise OSError("network down")

    config = clerk_config()
    verifier = ClerkTokenVerifier(config, jwks=JwksCache(config, fetcher=fetcher))

    with pytest.raises(AuthenticationUnavailableError):
        verifier.verify(key.mint())


def test_malformed_jwks_documents_are_not_trusted(key):
    config = clerk_config()
    verifier = ClerkTokenVerifier(config, jwks=JwksCache(config, fetcher=lambda u, t: {"keys": [{"kty": "oct", "kid": key.kid}]}))

    with pytest.raises(AuthenticationUnavailableError):
        verifier.verify(key.mint())
