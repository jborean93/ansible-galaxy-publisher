"""JWT authentication and validation."""

from __future__ import annotations

import logging
import typing as t

import jwt
from jwt import PyJWK
from jwt.exceptions import InvalidTokenError as JWTInvalidTokenError

from galaxy_publisher.cache import JWKSCache
from galaxy_publisher.config import Config

logger = logging.getLogger(__name__)

# Claim key for storing OIDC issuer ID
ISSUER_ID = "_oidc_issuer_id"


class AuthenticationError(Exception):
    """Base authentication error."""

    pass


class InvalidTokenError(AuthenticationError):
    """Token is invalid."""

    pass


class ExpiredTokenError(AuthenticationError):
    """Token is expired."""

    pass


def extract_token_from_header(authorization: str | None) -> str:
    """Extract token from Authorization header.

    Args:
        authorization: Authorization header value

    Returns:
        Token value

    Raises:
        InvalidTokenError: If header format is invalid
    """
    if not authorization:
        raise InvalidTokenError("Missing Authorization header")

    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        raise InvalidTokenError("Invalid Authorization header format")

    scheme, token = parts
    if scheme.lower() != "token":
        raise InvalidTokenError(f"Unsupported authorization scheme: {scheme}")

    return token


def decode_jwt_unverified(token: str) -> dict[str, t.Any]:
    """Decode JWT without verification to extract header and payload.

    Args:
        token: JWT token string

    Returns:
        Dictionary containing 'header' and 'payload'

    Raises:
        InvalidTokenError: If token cannot be decoded
    """
    try:
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
            },
        )
        return {"header": header, "payload": payload}
    except JWTInvalidTokenError as e:
        raise InvalidTokenError(f"Failed to decode JWT: {e}") from e


async def validate_jwt(
    token: str,
    jwks_cache: JWKSCache,
    config: Config,
) -> dict[str, t.Any]:
    """Validate JWT token with JWKS signature verification.

    Args:
        token: JWT token string
        jwks_cache: JWKS cache instance
        config: Configuration object

    Returns:
        Validated claims dictionary

    Raises:
        InvalidTokenError: If token is invalid
        ExpiredTokenError: If token is expired
        AuthenticationError: If validation fails
    """

    # Decode without verification to get issuer
    try:
        unverified = decode_jwt_unverified(token)
        payload = unverified["payload"]
    except InvalidTokenError:
        raise

    # Get issuer from claims
    issuer = payload.get("iss")
    if not issuer:
        raise InvalidTokenError("Token missing 'iss' claim")

    # Find matching OIDC issuer in config
    oidc_issuer = None
    oidc_issuer_id = None
    for issuer_id, issuer_config in config.oidc_issuers.items():
        if issuer_config.issuer_url == issuer:
            oidc_issuer = issuer_config
            oidc_issuer_id = issuer_id
            break

    if not oidc_issuer:
        raise InvalidTokenError(f"Unknown OIDC issuer: {issuer}")

    # Fetch JWKS
    try:
        jwks_data = await jwks_cache.get(oidc_issuer.issuer_url, oidc_issuer.jwks_url)
    except Exception as e:
        raise AuthenticationError(f"Failed to fetch JWKS: {e}") from e

    # Verify signature and validate claims
    try:
        # Get the token header to find the key ID
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # Get the signing key from JWKS
        signing_key = None
        if kid:
            # Try to find key by kid
            for key_dict in jwks_data.get("keys", []):
                if key_dict.get("kid") == kid:
                    signing_key = PyJWK.from_dict(key_dict)
                    break
            if not signing_key:
                raise InvalidTokenError(f"Key with kid '{kid}' not found in JWKS")
        else:
            # No kid in header, use first key from JWKS
            if not jwks_data.get("keys"):
                raise InvalidTokenError("JWKS contains no keys")

            signing_key = PyJWK.from_dict(jwks_data["keys"][0])

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=config.settings.audience,
            issuer=oidc_issuer.issuer_url,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as e:
        raise ExpiredTokenError("Token has expired") from e
    except JWTInvalidTokenError as e:
        raise InvalidTokenError(f"Token validation failed: {e}") from e

    # Add issuer ID to claims for authorization
    claims[ISSUER_ID] = oidc_issuer_id

    return claims
