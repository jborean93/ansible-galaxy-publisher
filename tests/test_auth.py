"""Tests for JWT authentication."""

import time
import typing as t
import unittest.mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from galaxy_publisher.auth import (
    ISSUER_ID,
    ExpiredTokenError,
    InvalidTokenError,
    decode_jwt_unverified,
    extract_token_from_header,
    validate_jwt,
)
from galaxy_publisher.cache import JWKSCache, _CachedItem
from galaxy_publisher.config import Config, OIDCIssuer, Server, Settings


def create_test_config() -> Config:
    """Create a test Config with standard settings for auth tests.

    Returns:
        Config object with test issuer and server
    """
    return Config(
        settings=Settings(audience="https://example.com"),
        oidc_issuers={
            "test_issuer": OIDCIssuer(
                issuer_url="https://issuer.example.com",
                jwks_url="https://issuer.example.com/.well-known/jwks",
            )
        },
        servers={"test_server": Server(base_url="https://galaxy.example.com", token="test-token")},
        authorization_rules=[],
    )


def generate_rsa_keypair() -> tuple[str, str]:
    """Generate RSA keypair for testing.

    Returns:
        Tuple of (private_key_pem, public_key_pem)
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    return private_pem, public_pem


def test_extract_token_from_header_valid() -> None:
    """Test extracting token from valid Authorization header."""
    token = extract_token_from_header("Token test-token-123")
    assert token == "test-token-123"


def test_extract_token_from_header_case_insensitive() -> None:
    """Test that token scheme is case-insensitive."""
    token = extract_token_from_header("token test-token-123")
    assert token == "test-token-123"


def test_extract_token_from_header_missing() -> None:
    """Test that missing header raises error."""
    with pytest.raises(InvalidTokenError, match="Missing Authorization header"):
        extract_token_from_header(None)


def test_extract_token_from_header_invalid_format() -> None:
    """Test that invalid header format raises error."""
    with pytest.raises(InvalidTokenError, match="Invalid Authorization header format"):
        extract_token_from_header("InvalidFormat")


def test_extract_token_from_header_unsupported_scheme() -> None:
    """Test that unsupported scheme raises error."""
    with pytest.raises(InvalidTokenError, match="Unsupported authorization scheme: Bearer"):
        extract_token_from_header("Bearer test-token-123")


def test_decode_jwt_unverified() -> None:
    """Test decoding JWT without verification."""
    private_key, _ = generate_rsa_keypair()

    payload = {
        "iss": "https://issuer.example.com",
        "sub": "user123",
        "exp": int(time.time()) + 3600,
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")

    result = decode_jwt_unverified(token)

    assert "header" in result
    assert "payload" in result
    assert result["header"]["alg"] == "RS256"
    assert result["payload"]["iss"] == "https://issuer.example.com"
    assert result["payload"]["sub"] == "user123"


def test_decode_jwt_unverified_invalid() -> None:
    """Test that invalid JWT raises error."""
    with pytest.raises(InvalidTokenError, match="Failed to decode JWT"):
        decode_jwt_unverified("invalid-token")


@pytest.mark.asyncio
async def test_validate_jwt_success() -> None:
    """Test successful JWT validation."""
    private_key, public_key = generate_rsa_keypair()

    # Create test config
    config = create_test_config()

    # Create token
    now = int(time.time())
    payload = {
        "iss": "https://issuer.example.com",
        "aud": "https://example.com",
        "sub": "user123",
        "exp": now + 3600,
        "iat": now,
        "nbf": now,
        "repository": "myorg/myrepo",
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")

    # Pre-populate JWKS cache
    jwks_cache = JWKSCache()
    jwks_cache._cache["https://issuer.example.com"] = _CachedItem(
        value={
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "kid": "test-key",
                    "n": "mock",
                    "e": "AQAB",
                }
            ]
        },
        expires_at=time.time() + 3600,
    )

    # Mock jwt.decode to use our public key
    with unittest.mock.patch("galaxy_publisher.auth.jwt.decode") as mock_decode:
        mock_decode.return_value = {
            "iss": "https://issuer.example.com",
            "aud": "https://example.com",
            "sub": "user123",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "repository": "myorg/myrepo",
        }

        claims = await validate_jwt(token, jwks_cache, config)

    assert claims["iss"] == "https://issuer.example.com"
    assert claims["aud"] == "https://example.com"
    assert claims["sub"] == "user123"
    assert claims["repository"] == "myorg/myrepo"
    assert claims[ISSUER_ID] == "test_issuer"


@pytest.mark.asyncio
async def test_validate_jwt_missing_issuer() -> None:
    """Test that token without issuer claim raises error."""
    private_key, _ = generate_rsa_keypair()

    # Create test config
    config = create_test_config()

    # Create token without issuer
    payload = {"aud": "https://example.com", "sub": "user123", "exp": int(time.time()) + 3600}

    token = jwt.encode(payload, private_key, algorithm="RS256")

    jwks_cache = JWKSCache()

    with pytest.raises(InvalidTokenError, match="Token missing 'iss' claim"):
        await validate_jwt(token, jwks_cache, config)


@pytest.mark.asyncio
async def test_validate_jwt_unknown_issuer() -> None:
    """Test that token from unknown issuer raises error."""
    private_key, _ = generate_rsa_keypair()

    # Create test config
    config = create_test_config()

    # Create token with unknown issuer
    payload = {
        "iss": "https://unknown-issuer.example.com",
        "aud": "https://example.com",
        "sub": "user123",
        "exp": int(time.time()) + 3600,
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")

    jwks_cache = JWKSCache()

    with pytest.raises(InvalidTokenError, match="Unknown OIDC issuer"):
        await validate_jwt(token, jwks_cache, config)


@pytest.mark.asyncio
async def test_validate_jwt_expired() -> None:
    """Test that expired token raises error."""
    private_key, public_key = generate_rsa_keypair()

    # Create test config
    config = create_test_config()

    # Create expired token
    now = int(time.time())
    payload = {
        "iss": "https://issuer.example.com",
        "aud": "https://example.com",
        "sub": "user123",
        "exp": now - 3600,  # Expired 1 hour ago
        "iat": now - 7200,
        "nbf": now - 7200,
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")

    # Pre-populate JWKS cache with actual public key
    # Load the public key and convert to JWK format
    from jwt.algorithms import RSAAlgorithm

    public_key_obj = t.cast(RSAPublicKey, serialization.load_pem_public_key(public_key.encode()))
    public_key_jwk = RSAAlgorithm.to_jwk(public_key_obj, as_dict=True)

    jwks_cache = JWKSCache()
    jwks_cache._cache["https://issuer.example.com"] = _CachedItem(
        value={
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "kid": "test-key-1",
                    **public_key_jwk,
                }
            ]
        },
        expires_at=time.time() + 3600,
    )

    # Should raise ExpiredTokenError for expired token
    with pytest.raises(ExpiredTokenError, match="Token has expired"):
        await validate_jwt(token, jwks_cache, config)
