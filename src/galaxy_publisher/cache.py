"""Caching infrastructure for JWKS and OAuth tokens."""

from __future__ import annotations

import dataclasses
import logging
import time
import typing as t

import httpx

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _CachedItem[T]:
    """Generic cached item with expiry.

    Type Parameters:
        T: Type of the cached value
    """

    value: T
    expires_at: float


class JWKSCache:
    """Cache for JWKS (JSON Web Key Sets) with TTL."""

    def __init__(self) -> None:
        """Initialize JWKS cache."""
        self._cache: dict[str, _CachedItem[dict[str, t.Any]]] = {}

    async def get(
        self,
        issuer_url: str,
        jwks_url: str,
        ttl: int = 86400,
    ) -> dict[str, t.Any]:
        """Get JWKS from cache or fetch from URL.

        Args:
            issuer_url: OIDC issuer URL (used as cache key)
            jwks_url: URL to fetch JWKS from
            ttl: Time-to-live in seconds (default: 24 hours)

        Returns:
            JWKS dictionary

        Raises:
            httpx.HTTPError: If fetching JWKS fails
        """
        now = time.time()

        # Check cache
        if issuer_url in self._cache:
            cached = self._cache[issuer_url]
            if cached.expires_at > now:
                logger.debug("JWKS cache hit: issuer=%s", issuer_url)
                return cached.value
            else:
                logger.debug("JWKS cache expired: issuer=%s", issuer_url)

        # Fetch from URL
        logger.info("Fetching JWKS from %s", jwks_url)

        async with httpx.AsyncClient() as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            jwks = t.cast(dict[str, t.Any], response.json())

        logger.info("JWKS fetched: %d keys", len(jwks.get("keys", [])))

        # Cache with TTL
        self._cache[issuer_url] = _CachedItem(value=jwks, expires_at=now + ttl)

        return jwks


class OAuthTokenCache:
    """Cache for OAuth access tokens with expiry tracking."""

    def __init__(self) -> None:
        """Initialize OAuth token cache."""
        self._cache: dict[str, _CachedItem[tuple[str, str]]] = {}

    async def get_token(
        self,
        server_id: str,
        client_id: str,
        client_secret: str,
        auth_url: str,
    ) -> tuple[str, str]:
        """Get OAuth access token from cache or exchange credentials.

        Args:
            server_id: Server identifier (used as cache key)
            client_id: OAuth client ID
            client_secret: OAuth client secret
            auth_url: OAuth token endpoint URL

        Returns:
            Tuple containing access token string and token type

        Raises:
            httpx.HTTPError: If token exchange fails
            ValueError: If response is invalid
        """
        now = time.time()

        # Check cache (with 5-minute buffer before expiry)
        if server_id in self._cache:
            cached = self._cache[server_id]
            if cached.expires_at > now + 300:  # 5-minute buffer
                logger.debug("OAuth token cache hit: server_id=%s", server_id)
                return cached.value
            else:
                logger.debug("OAuth token cache expired: server_id=%s", server_id)

        # Exchange credentials for token
        logger.info("Exchanging OAuth credentials at %s", auth_url)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            response.raise_for_status()
            token_data = t.cast(dict[str, t.Any], response.json())

        if "access_token" not in token_data:
            logger.error(
                "OAuth response missing access_token, got keys: %s", list(token_data.keys())
            )
            raise ValueError("OAuth response missing access_token")

        access_token = t.cast(str, token_data["access_token"])
        expires_in = t.cast(int, token_data.get("expires_in", 3600))  # Default 1 hour
        token_type = t.cast(str, token_data.get("token_type", "Bearer"))

        logger.info("OAuth token obtained, expires_in=%d", expires_in)

        # Cache with expiry
        self._cache[server_id] = _CachedItem(
            value=(access_token, token_type), expires_at=now + expires_in
        )

        return access_token, token_type
