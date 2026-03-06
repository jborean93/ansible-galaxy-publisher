"""Tests for HTTP proxy logic."""

import time
import unittest.mock

import pytest
from httpx import AsyncClient, Response

from galaxy_publisher.cache import OAuthTokenCache, _CachedItem
from galaxy_publisher.config import OAuthSecret, Server
from galaxy_publisher.proxy import get_server_token, proxy_request


@pytest.mark.asyncio
async def test_get_server_token_with_token_auth() -> None:
    """Test getting token from server with token auth."""
    server = Server(base_url="https://galaxy.example.com", token="test-token-123")

    oauth_cache = OAuthTokenCache()

    token, auth_type = await get_server_token(server, oauth_cache)

    assert token == "test-token-123"
    assert auth_type == "Token"


@pytest.mark.asyncio
async def test_get_server_token_with_oauth_redhat() -> None:
    """Test getting token from server with OAuth (Red Hat)."""
    server = Server(
        base_url="https://console.redhat.com/api/automation-hub/",
        oauth_secret=OAuthSecret(
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
        ),
    )

    oauth_cache = OAuthTokenCache()

    # Pre-populate cache with OAuth token for this server
    oauth_cache._cache[str(id(server))] = _CachedItem(
        value=("oauth-access-token", "Bearer"),
        expires_at=time.time() + 3600,
    )

    token, auth_type = await get_server_token(server, oauth_cache)

    assert token == "oauth-access-token"
    assert auth_type == "Bearer"


@pytest.mark.asyncio
async def test_get_server_token_with_oauth_generic() -> None:
    """Test getting token from server with OAuth (generic)."""
    server = Server(
        base_url="https://galaxy.example.com",
        oauth_secret=OAuthSecret(
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://galaxy.example.com/token",
        ),
    )

    oauth_cache = OAuthTokenCache()

    # Pre-populate cache with OAuth token for this server
    oauth_cache._cache[str(id(server))] = _CachedItem(
        value=("oauth-access-token", "Bearer"),
        expires_at=time.time() + 3600,
    )

    token, auth_type = await get_server_token(server, oauth_cache)

    assert token == "oauth-access-token"
    assert auth_type == "Bearer"


@pytest.mark.asyncio
async def test_get_server_token_invalid_config() -> None:
    server = Server(base_url="https://galaxy.example.com")

    oauth_cache = OAuthTokenCache()

    with pytest.raises(
        ValueError, match="Server must have either token or oauth_secret configured"
    ):
        await get_server_token(server, oauth_cache)


@pytest.mark.asyncio
async def test_proxy_request_get() -> None:
    """Test proxying GET request."""
    with unittest.mock.patch.object(AsyncClient, "request") as mock_request:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.content = b'{"status": "ok"}'
        mock_request.return_value = mock_response

        status, headers, body = await proxy_request(
            method="GET",
            target_url="https://galaxy.example.com/api/",
            headers={"User-Agent": "test", "Authorization": "Token old-token"},
            body=None,
            auth_token="new-token",
            auth_type="Token",
        )

    assert status == 200
    assert headers == {"content-type": "application/json"}
    assert body == b'{"status": "ok"}'

    # Verify request
    mock_request.assert_called_once()
    call_args = mock_request.call_args
    assert call_args[1]["method"] == "GET"
    assert call_args[1]["url"] == "https://galaxy.example.com/api/"
    assert call_args[1]["headers"]["Authorization"] == "Token new-token"
    assert call_args[1]["headers"]["User-Agent"] == "test"
    assert "Host" not in call_args[1]["headers"]
    assert call_args[1]["content"] is None


@pytest.mark.asyncio
async def test_proxy_request_post() -> None:
    """Test proxying POST request."""
    with unittest.mock.patch.object(AsyncClient, "request") as mock_request:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.status_code = 202
        mock_response.headers = {"content-type": "application/json", "location": "/task/123"}
        mock_response.content = b'{"task": "/task/123"}'
        mock_request.return_value = mock_response

        status, headers, body = await proxy_request(
            method="POST",
            target_url="https://galaxy.example.com/api/v3/artifacts/collections/",
            headers={"Content-Type": "application/octet-stream"},
            body=b"collection-data",
            auth_token="bearer-token",
            auth_type="Bearer",
        )

    assert status == 202
    assert "location" in headers
    assert body == b'{"task": "/task/123"}'

    # Verify request
    mock_request.assert_called_once()
    call_args = mock_request.call_args
    assert call_args[1]["method"] == "POST"
    assert call_args[1]["url"] == "https://galaxy.example.com/api/v3/artifacts/collections/"
    assert call_args[1]["headers"]["Authorization"] == "Bearer bearer-token"
    assert call_args[1]["content"] == b"collection-data"


@pytest.mark.asyncio
async def test_proxy_request_filters_headers() -> None:
    """Test that certain headers are filtered out."""
    with unittest.mock.patch.object(AsyncClient, "request") as mock_request:
        mock_response = unittest.mock.AsyncMock(spec=Response)
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "application/json",
            "transfer-encoding": "chunked",  # Should be filtered
            "connection": "keep-alive",  # Should be filtered
        }
        mock_response.content = b"{}"
        mock_request.return_value = mock_response

        status, headers, body = await proxy_request(
            method="GET",
            target_url="https://galaxy.example.com/api/",
            headers={
                "User-Agent": "test",
                "Host": "old-host",  # Should be filtered from request
                "Authorization": "Token old-token",  # Should be replaced
            },
            body=None,
            auth_token="new-token",
            auth_type="Token",
        )

    # Response headers should not include transfer-encoding or connection
    assert "transfer-encoding" not in headers
    assert "connection" not in headers
    assert "content-type" in headers

    # Request headers should not include Host, and Authorization should be replaced
    call_args = mock_request.call_args
    assert "Host" not in call_args[1]["headers"]
    assert call_args[1]["headers"]["Authorization"] == "Token new-token"
