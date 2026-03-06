"""Tests for redirect handling in proxy."""

import httpx
import pytest
import respx

from galaxy_publisher.proxy import proxy_request


@pytest.mark.asyncio
async def test_proxy_follows_redirects() -> None:
    """Test that proxy follows redirects from Galaxy server."""
    # Mock Galaxy server that redirects /api to /api/
    with respx.mock:
        # First request redirects
        respx.get("https://galaxy.example.com/api").mock(
            return_value=httpx.Response(
                status_code=301,
                headers={"Location": "/api/"},
            )
        )

        # Second request (after redirect) succeeds
        respx.get("https://galaxy.example.com/api/").mock(
            return_value=httpx.Response(
                status_code=200,
                json={"available_versions": {"v3": "v3/"}},
            )
        )

        # Proxy should follow redirect and return final 200 response
        status, headers, body = await proxy_request(
            method="GET",
            target_url="https://galaxy.example.com/api",
            headers={},
            body=None,
            auth_token="test-token",
            auth_type="Token",
        )

        assert status == 200
        assert b"available_versions" in body


@pytest.mark.asyncio
async def test_proxy_follows_multiple_redirects() -> None:
    """Test that proxy follows multiple redirects."""
    with respx.mock:
        # First redirect
        respx.get("https://galaxy.example.com/old").mock(
            return_value=httpx.Response(
                status_code=307,
                headers={"Location": "/intermediate"},
            )
        )

        # Second redirect
        respx.get("https://galaxy.example.com/intermediate").mock(
            return_value=httpx.Response(
                status_code=301,
                headers={"Location": "/final"},
            )
        )

        # Final response
        respx.get("https://galaxy.example.com/final").mock(
            return_value=httpx.Response(
                status_code=200,
                json={"result": "success"},
            )
        )

        status, headers, body = await proxy_request(
            method="GET",
            target_url="https://galaxy.example.com/old",
            headers={},
            body=None,
            auth_token="test-token",
            auth_type="Token",
        )

        assert status == 200
        assert b"success" in body


@pytest.mark.asyncio
async def test_proxy_preserves_query_params_through_redirect() -> None:
    """Test that query parameters are preserved when following redirects."""
    with respx.mock:
        # Request with query params redirects
        respx.get("https://galaxy.example.com/api?version=3").mock(
            return_value=httpx.Response(
                status_code=301,
                headers={"Location": "/api/?version=3"},
            )
        )

        # Final response
        respx.get("https://galaxy.example.com/api/?version=3").mock(
            return_value=httpx.Response(
                status_code=200,
                json={"version": "3"},
            )
        )

        status, headers, body = await proxy_request(
            method="GET",
            target_url="https://galaxy.example.com/api?version=3",
            headers={},
            body=None,
            auth_token="test-token",
            auth_type="Token",
        )

        assert status == 200
        assert b'"version":"3"' in body or b'"version": "3"' in body
