"""HTTP proxy logic for forwarding requests to Galaxy/Automation Hub."""

from __future__ import annotations

import logging

import httpx

from galaxy_publisher.cache import OAuthTokenCache
from galaxy_publisher.config import Server

logger = logging.getLogger(__name__)


async def get_server_token(
    server: Server,
    oauth_cache: OAuthTokenCache,
) -> tuple[str, str]:
    """Get authentication token for server.

    Args:
        server: Server configuration
        oauth_cache: OAuth token cache

    Returns:
        Tuple of (token_value, auth_type) where auth_type is "Token" or "Bearer"

    Raises:
        ValueError: If server auth configuration is invalid
    """
    if server.token:
        # Token-based auth
        return server.token, "Token"

    if server.oauth_secret:
        # OAuth-based auth
        # Get token from cache or exchange
        return await oauth_cache.get_token(
            server_id=str(id(server)),  # Use server object ID as cache key
            client_id=server.oauth_secret.client_id,
            client_secret=server.oauth_secret.client_secret,
            auth_url=server.oauth_secret.auth_url,
        )

    raise ValueError("Server must have either token or oauth_secret configured")


async def proxy_request(
    method: str,
    target_url: str,
    headers: dict[str, str],
    body: bytes | None,
    auth_token: str,
    auth_type: str,
) -> tuple[int, dict[str, str], bytes]:
    """Proxy HTTP request to Galaxy/Automation Hub.

    Args:
        method: HTTP method (GET, POST, etc.)
        target_url: Target URL to proxy to
        headers: Request headers (Authorization will be replaced)
        body: Request body bytes (None for GET)
        auth_token: Authentication token value
        auth_type: Authentication type ("Token" or "Bearer")

    Returns:
        Tuple of (status_code, response_headers, response_body)

    Raises:
        httpx.HTTPError: If request fails
    """
    # Build headers for Galaxy request
    galaxy_headers = {
        key: value for key, value in headers.items() if key.lower() not in ("authorization", "host")
    }

    # Add Galaxy authentication
    galaxy_headers["Authorization"] = f"{auth_type} {auth_token}"

    logger.info("Proxying %s to %s", method, target_url)

    # Make request
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(
            method=method,
            url=target_url,
            headers=galaxy_headers,
            content=body,
        )

    logger.info(
        "Galaxy response: status=%d, size=%d bytes",
        response.status_code,
        len(response.content),
    )

    # Filter hop-by-hop headers that apply only to the proxy-Galaxy connection:
    # - transfer-encoding: httpx decodes chunked/compressed responses when using .content
    # - connection: controls the proxy-Galaxy connection, not client-proxy connection
    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in ("transfer-encoding", "connection")
    }

    return response.status_code, response_headers, response.content
