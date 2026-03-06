"""FastAPI application for Ansible Galaxy Publisher proxy."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import typing as t

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

from galaxy_publisher._version import __version__
from galaxy_publisher.auth import (
    ISSUER_ID,
    AuthenticationError,
    ExpiredTokenError,
    InvalidTokenError,
    extract_token_from_header,
    validate_jwt,
)
from galaxy_publisher.authorization import (
    AuthorizationError,
    find_authorization_rule,
    validate_collection_name,
    verify_server_access,
)
from galaxy_publisher.cache import JWKSCache, OAuthTokenCache
from galaxy_publisher.collection import (
    CollectionValidationError,
    extract_manifest_from_tarball,
    extract_tarball_from_multipart,
)
from galaxy_publisher.config import AuthorizationRule, Config, Server, load_config
from galaxy_publisher.proxy import get_server_token, proxy_request

logger = logging.getLogger(__name__)

# Global caches
jwks_cache = JWKSCache()
oauth_cache = OAuthTokenCache()

# Global config (set during lifespan)
app_config: Config | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> t.AsyncIterator[None]:
    """Application lifespan manager."""
    global app_config

    # Startup: Load configuration
    config_path = os.environ.get("CONFIG_PATH", "config/servers.yml")
    logger.info("Loading configuration from %s", config_path)

    try:
        app_config = load_config(config_path)
        logger.info(
            "Configuration loaded: %d servers, %d OIDC issuers, %d authorization rules",
            len(app_config.servers),
            len(app_config.oidc_issuers),
            len(app_config.authorization_rules),
        )
    except Exception:
        logger.exception("Failed to load configuration from %s", config_path)
        raise

    yield

    # Shutdown: Nothing to clean up (in-memory caches)
    logger.info("Shutting down")


app = FastAPI(
    title="Ansible Galaxy Publisher",
    description="Authenticated proxy for Ansible Galaxy/Automation Hub APIs",
    version=__version__,
    lifespan=lifespan,
)


def _rewrite_task_urls(response_body: bytes, proxy_path: str) -> bytes:
    """Rewrite task URLs in publish response to include proxy prefix.

    When Galaxy returns task URLs starting with '/', they need to be rewritten
    to include the proxy prefix '/api/v1/{server_id}' so that ansible-galaxy's
    urljoin doesn't strip the server_id from the path.

    For example:
        Galaxy returns: {"task": "/api/v3/imports/collections/12345/"}
        Client needs:   {"task": "/api/v1/test_server/api/v3/imports/collections/12345/"}

    Args:
        response_body: Response body bytes from Galaxy
        proxy_path: Proxy path to include in rewritten URLs

    Returns:
        Modified response body with rewritten URLs
    """
    try:
        data = json.loads(response_body)

        # Check if response contains task URL that needs rewriting
        if (
            isinstance(data, dict)
            and (task_url := data.get("task"))
            and isinstance(task_url, str)
            and task_url.startswith("/")
        ):
            # Rewrite to include proxy prefix
            data["task"] = f"{proxy_path}{task_url}"
            logger.info("Rewrote task URL: %s -> %s", task_url, data["task"])
            return json.dumps(data).encode("utf-8")

    except (json.JSONDecodeError, KeyError, TypeError):
        # If we can't parse or modify, return original
        pass

    return response_body


async def _validate_auth_token(
    authorization: str | None,
    server_id: str,
    config: Config,
) -> AuthorizationRule:
    """Validate authentication token and return authorization rule.

    Args:
        authorization: Authorization header value
        server_id: Server identifier
        config: Application configuration

    Returns:
        Authorization rule that matches the token claims

    Raises:
        HTTPException: If authentication or authorization fails
    """
    # Extract and validate OIDC token
    try:
        token = extract_token_from_header(authorization)
        claims = await validate_jwt(token, jwks_cache, config)

        logger.info("JWT validated: claims=%r", claims)

    except ExpiredTokenError as e:
        logger.exception("Token expired for server=%s", server_id)
        raise HTTPException(status_code=401, detail=str(e)) from e
    except InvalidTokenError as e:
        logger.exception("Invalid token for server=%s", server_id)
        raise HTTPException(status_code=401, detail=str(e)) from e
    except AuthenticationError as e:
        logger.exception("Authentication error for server=%s", server_id)
        raise HTTPException(status_code=401, detail=str(e)) from e

    # Find authorization rule
    oidc_issuer_id = claims.get(ISSUER_ID)
    if not oidc_issuer_id:
        logger.error("Missing OIDC issuer ID in claims")
        raise HTTPException(status_code=500, detail="Missing OIDC issuer ID in claims")

    rule = find_authorization_rule(claims, oidc_issuer_id, config)
    if not rule:
        logger.warning(
            "No authorization rule matched: issuer=%s, repo=%s",
            oidc_issuer_id,
            claims.get("repository"),
        )
        raise HTTPException(
            status_code=403,
            detail="No authorization rule matches your identity claims",
        )

    logger.info("Authorization granted for server=%s", server_id)

    # Verify server access
    try:
        verify_server_access(rule, server_id)
    except AuthorizationError as e:
        logger.exception("Server access denied: server=%s, allowed=%s", server_id, rule.servers)
        raise HTTPException(status_code=403, detail=str(e)) from e

    return rule


async def _validate_request(
    authorization: str | None,
    server_id: str,
) -> tuple[Server, AuthorizationRule]:
    """Validate request and return config, server, and auth rule.

    Args:
        authorization: Authorization header value
        server_id: Server identifier

    Returns:
        Tuple of (config, server, auth_rule)

    Raises:
        HTTPException: If validation fails
    """
    if app_config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    if server_id not in app_config.servers:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")

    rule = await _validate_auth_token(authorization, server_id, app_config)
    server = app_config.servers[server_id]

    return server, rule


async def _proxy_to_server(
    server: Server,
    url_path: str,
    method: str,
    request: Request,
    body: bytes | None = None,
) -> Response:
    """Common helper to proxy requests to Galaxy server.

    Args:
        server: Server configuration object
        url_path: URL path to append to server base URL (e.g., "/api/")
        method: HTTP method (GET, POST, etc.)
        request: FastAPI request object for headers
        body: Optional request body (uses request.body() if None)

    Returns:
        FastAPI Response

    Raises:
        HTTPException: If proxy fails
    """

    # Get server token
    try:
        auth_token, auth_type = await get_server_token(server, oauth_cache)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get server authentication: {e}"
        ) from e

    # Build target URL
    target_url = f"{server.base_url.rstrip('/')}{url_path}"

    # Get request body if not provided
    if body is None:
        body = await request.body()

    # Proxy request
    try:
        status, headers, response_body = await proxy_request(
            method=method,
            target_url=target_url,
            headers=dict(request.headers),
            body=body,
            auth_token=auth_token,
            auth_type=auth_type,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to proxy request: {e}") from e

    return Response(content=response_body, status_code=status, headers=headers)


@app.get("/health")
async def health() -> dict[str, t.Any]:
    """Health check endpoint.

    Returns:
        Health status information
    """
    return {
        "status": "healthy",
        "service": "ansible-galaxy-publisher",
        "version": __version__,
        "messages": [],  # FUTURE: Add any relevant health messages or warnings here
    }


@app.post("/api/v1/{server_id}/api/v3/artifacts/collections/")
async def publish_collection(
    server_id: str,
    request: Request,
    authorization: str | None = Header(None),
) -> Response:
    """Publish collection endpoint (POST).

    Args:
        server_id: Server identifier
        request: FastAPI request object
        authorization: Authorization header

    Returns:
        Proxied response from Galaxy

    Raises:
        HTTPException: If authentication, authorization, or validation fails
    """
    server, rule = await _validate_request(authorization, server_id)

    # Read request body (may be raw tarball or multipart)
    body = await request.body()

    # Extract tarball from multipart if needed
    content_type = request.headers.get("content-type", "")
    try:
        tarball_bytes = extract_tarball_from_multipart(body, content_type)
    except CollectionValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Extract and validate collection name from tarball
    try:
        manifest = extract_manifest_from_tarball(tarball_bytes)
        namespace = manifest["namespace"]
        name = manifest["name"]
    except CollectionValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Validate collection name against allowed collections
    try:
        validate_collection_name(namespace, name, rule.allowed_collections)
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    # Proxy to server (forward original body, not extracted tarball)
    response = await _proxy_to_server(
        server=server,
        url_path="/api/v3/artifacts/collections/",
        method="POST",
        request=request,
        body=body,
    )

    # Rewrite task URLs in response to include proxy prefix
    # This fixes urljoin behavior when task URL starts with '/'
    modified_body = _rewrite_task_urls(response.body, f"/api/v1/{server_id}")

    # Update Content-Length if body was modified
    response_headers = dict(response.headers)
    if modified_body != response.body and "content-length" in response_headers:
        response_headers["content-length"] = str(len(modified_body))

    return Response(
        content=modified_body,
        status_code=response.status_code,
        headers=response_headers,
    )


@app.get("/api/v1/{server_id}/{path:path}")
async def proxy_get_request(
    server_id: str,
    path: str,
    request: Request,
    authorization: str | None = Header(None),
) -> Response:
    """Proxy all GET requests to Galaxy server.

    This catch-all route handles all GET requests including:
    - API discovery (/api/)
    - Collection import status (/api/v3/imports/collections/{task_id}/)
    - Any other Galaxy-NG endpoints

    Args:
        server_id: Server identifier
        path: Request path after server_id
        request: FastAPI request object
        authorization: Authorization header

    Returns:
        Proxied response from Galaxy

    Raises:
        HTTPException: If authentication or authorization fails
    """
    server, _ = await _validate_request(authorization, server_id)

    # Reconstruct the full path (path variable doesn't include leading /)
    url_path = f"/{path}" if not path.startswith("/") else path

    # Preserve query parameters if present
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    return await _proxy_to_server(
        server=server,
        url_path=url_path,
        method="GET",
        request=request,
    )
