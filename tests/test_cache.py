"""Tests for caching infrastructure."""

import time
import unittest.mock

import pytest
from httpx import AsyncClient, Response

from galaxy_publisher.cache import JWKSCache, OAuthTokenCache


@pytest.mark.asyncio
async def test_jwks_cache_miss() -> None:
    """Test JWKS cache miss fetches from URL."""
    cache = JWKSCache()

    jwks_data = {"keys": [{"kty": "RSA", "kid": "test-key"}]}

    with unittest.mock.patch.object(AsyncClient, "get") as mock_get:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = jwks_data
        mock_get.return_value = mock_response

        result = await cache.get(
            issuer_url="https://issuer.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks",
        )

    assert result == jwks_data
    mock_get.assert_called_once_with("https://issuer.example.com/.well-known/jwks")


@pytest.mark.asyncio
async def test_jwks_cache_hit() -> None:
    """Test JWKS cache hit returns cached value."""
    cache = JWKSCache()

    jwks_data = {"keys": [{"kty": "RSA", "kid": "test-key"}]}

    with unittest.mock.patch.object(AsyncClient, "get") as mock_get:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = jwks_data
        mock_get.return_value = mock_response

        # First call - cache miss
        result1 = await cache.get(
            issuer_url="https://issuer.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks",
        )

        # Second call - cache hit
        result2 = await cache.get(
            issuer_url="https://issuer.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks",
        )

    assert result1 == jwks_data
    assert result2 == jwks_data
    # Should only fetch once
    mock_get.assert_called_once()


@pytest.mark.asyncio
async def test_jwks_cache_expiry() -> None:
    """Test JWKS cache expiry refetches after TTL."""
    cache = JWKSCache()

    jwks_data1 = {"keys": [{"kty": "RSA", "kid": "test-key-1"}]}
    jwks_data2 = {"keys": [{"kty": "RSA", "kid": "test-key-2"}]}

    with unittest.mock.patch.object(AsyncClient, "get") as mock_get:
        mock_response1 = unittest.mock.AsyncMock(spec=Response)
        mock_response1.json.return_value = jwks_data1
        mock_response1.raise_for_status = unittest.mock.Mock()

        mock_response2 = unittest.mock.AsyncMock(spec=Response)
        mock_response2.json.return_value = jwks_data2
        mock_response2.raise_for_status = unittest.mock.Mock()

        mock_get.side_effect = [mock_response1, mock_response2]

        # First call
        result1 = await cache.get(
            issuer_url="https://issuer.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks",
            ttl=1,  # 1 second TTL
        )

        # Wait for expiry
        time.sleep(1.1)

        # Second call - should refetch
        result2 = await cache.get(
            issuer_url="https://issuer.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks",
            ttl=1,
        )

    assert result1 == jwks_data1
    assert result2 == jwks_data2
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_oauth_token_cache_miss() -> None:
    """Test OAuth token cache miss exchanges credentials."""
    cache = OAuthTokenCache()

    token_data = {"access_token": "test-token-123", "expires_in": 3600}

    with unittest.mock.patch.object(AsyncClient, "post") as mock_post:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = token_data
        mock_post.return_value = mock_response

        result = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

    assert result == ("test-token-123", "Bearer")
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == "https://auth.example.com/token"
    assert call_args[1]["data"]["grant_type"] == "client_credentials"
    assert call_args[1]["data"]["client_id"] == "client-id"
    assert call_args[1]["data"]["client_secret"] == "client-secret"


@pytest.mark.asyncio
async def test_oauth_token_cache_hit() -> None:
    """Test OAuth token cache hit returns cached token."""
    cache = OAuthTokenCache()

    token_data = {"access_token": "test-token-123", "expires_in": 3600}

    with unittest.mock.patch.object(AsyncClient, "post") as mock_post:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = token_data
        mock_post.return_value = mock_response

        # First call - cache miss
        result1 = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

        # Second call - cache hit
        result2 = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

    assert result1 == ("test-token-123", "Bearer")
    assert result2 == ("test-token-123", "Bearer")
    # Should only exchange once
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_oauth_token_cache_expiry_buffer() -> None:
    """Test OAuth token cache refetches before expiry (5-min buffer)."""
    cache = OAuthTokenCache()

    token_data1 = {"access_token": "test-token-1", "expires_in": 6}  # 6 seconds
    token_data2 = {"access_token": "test-token-2", "expires_in": 3600, "token_type": "Other"}

    with unittest.mock.patch.object(AsyncClient, "post") as mock_post:
        mock_response1 = unittest.mock.AsyncMock(spec=Response)
        mock_response1.json.return_value = token_data1
        mock_response1.raise_for_status = unittest.mock.Mock()

        mock_response2 = unittest.mock.AsyncMock(spec=Response)
        mock_response2.json.return_value = token_data2
        mock_response2.raise_for_status = unittest.mock.Mock()

        mock_post.side_effect = [mock_response1, mock_response2]

        # First call
        result1 = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

        # Wait (token expires in 6s, but 5-min buffer means it's "expired" after 1s)
        time.sleep(1.1)

        # Second call - should refetch due to buffer
        result2 = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

    assert result1 == ("test-token-1", "Bearer")
    assert result2 == ("test-token-2", "Other")
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_oauth_token_missing_access_token() -> None:
    """Test OAuth token exchange fails if access_token missing."""
    cache = OAuthTokenCache()

    token_data = {"token_type": "Bearer"}  # Missing access_token

    with unittest.mock.patch.object(AsyncClient, "post") as mock_post:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = token_data
        mock_post.return_value = mock_response

        with pytest.raises(ValueError, match="OAuth response missing access_token"):
            await cache.get_token(
                server_id="test_server",
                client_id="client-id",
                client_secret="client-secret",
                auth_url="https://auth.example.com/token",
            )


@pytest.mark.asyncio
async def test_oauth_token_defaults_expires_in() -> None:
    """Test OAuth token defaults to 1 hour if expires_in not provided."""
    cache = OAuthTokenCache()

    token_data = {"access_token": "test-token-123"}  # No expires_in

    with unittest.mock.patch.object(AsyncClient, "post") as mock_post:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.json.return_value = token_data
        mock_post.return_value = mock_response

        result = await cache.get_token(
            server_id="test_server",
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://auth.example.com/token",
        )

    assert result == ("test-token-123", "Bearer")
