"""Tests for FastAPI endpoints."""

import tempfile
import typing as t
import unittest.mock

import pytest
import yaml
from fastapi.testclient import TestClient

from galaxy_publisher.auth import ISSUER_ID
from galaxy_publisher.config import AuthorizationRule
from galaxy_publisher.main import app


@pytest.fixture
def test_config_file(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[str]:
    """Create a temporary test configuration file."""
    # Set env var for server token
    monkeypatch.setenv("TEST_SERVER_TOKEN", "test-token-value")

    config_data = {
        "settings": {"audience": "https://example.com"},
        "oidc_issuers": {
            "github": {
                "issuer_url": "https://token.actions.githubusercontent.com",
                "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            }
        },
        "servers": {
            "test_server": {"base_url": "https://galaxy.example.com", "token": "TEST_SERVER_TOKEN"}
        },
        "authorization_rules": [
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["test_server"],
                "allowed_collections": ["myorg.collection"],
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml") as f:
        yaml.dump(config_data, f)
        config_path = f.name

        # Set environment variable
        monkeypatch.setenv("CONFIG_PATH", config_path)

        yield config_path


def test_health_endpoint(test_config_file: str) -> None:
    """Test health endpoint."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ansible-galaxy-publisher"
    assert "version" in data


def test_health_endpoint_no_auth(test_config_file: str) -> None:
    """Test that health endpoint doesn't require authentication."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_discovery_no_auth(test_config_file: str) -> None:
    """Test API discovery endpoint without authentication."""
    with TestClient(app) as client:
        response = client.get("/api/v1/test_server/api/")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_publish_collection_no_auth(test_config_file: str) -> None:
    """Test publish endpoint without authentication."""
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/test_server/api/v3/artifacts/collections/", content=b"test-data"
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_collection_status_no_auth(test_config_file: str) -> None:
    """Test collection status endpoint without authentication."""
    with TestClient(app) as client:
        response = client.get("/api/v1/test_server/api/v3/imports/collections/task123/")

    assert response.status_code == 401


def test_unknown_endpoint(test_config_file: str) -> None:
    """Test that unknown endpoints return 404."""
    client = TestClient(app)
    response = client.get("/unknown/endpoint")

    assert response.status_code == 404


def test_unknown_server_in_discovery(test_config_file: str) -> None:
    """Test API discovery with unknown server."""

    # Need to use context manager to trigger lifespan
    with TestClient(app) as client:
        # Mock authentication to pass, but use unknown server
        mock_extract_path = "galaxy_publisher.main.extract_token_from_header"
        mock_validate_path = "galaxy_publisher.main.validate_jwt"
        mock_find_rule_path = "galaxy_publisher.main.find_authorization_rule"
        with unittest.mock.patch(mock_extract_path) as mock_extract:
            with unittest.mock.patch(mock_validate_path) as mock_validate:
                with unittest.mock.patch(mock_find_rule_path) as mock_find_rule:
                    mock_extract.return_value = "test-token"
                    mock_validate.return_value = {
                        ISSUER_ID: "github",
                        "repository": "myorg/myrepo",
                    }
                    mock_find_rule.return_value = AuthorizationRule(
                        oidc_issuer="github",
                        claims={"repository": "myorg/myrepo"},
                        servers=["unknown_server"],
                        allowed_collections=["myorg.collection"],
                    )

                    response = client.get(
                        "/api/v1/unknown_server/api/", headers={"Authorization": "Token test-token"}
                    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_catchall_handles_api_discovery(test_config_file: str) -> None:
    """Test that catch-all handles /api/ endpoint."""
    with TestClient(app) as client:
        response = client.get("/api/v1/test_server/api/")
        # Should require auth
        assert response.status_code == 401


def test_catchall_handles_task_status_url(test_config_file: str) -> None:
    """Test that catch-all handles task status URL."""
    with TestClient(app) as client:
        response = client.get("/api/v1/test_server/api/v3/imports/collections/task123/")
        # Should require auth
        assert response.status_code == 401


def test_catchall_handles_alternate_task_url_format(test_config_file: str) -> None:
    """Test that catch-all handles alternate task URL format (future Galaxy-NG versions)."""
    with TestClient(app) as client:
        # Hypothetical alternate format that some Galaxy-NG version might use
        response = client.get("/api/v1/test_server/api/v3/tasks/task123/")
        # Should require auth
        assert response.status_code == 401


def test_catchall_handles_any_get_endpoint(test_config_file: str) -> None:
    """Test that catch-all handles arbitrary GET endpoints."""
    with TestClient(app) as client:
        # Any GET endpoint should be proxied
        response = client.get("/api/v1/test_server/api/v3/some/future/endpoint/")
        # Should require auth
        assert response.status_code == 401


def test_post_endpoint_not_affected_by_catchall(test_config_file: str) -> None:
    """Test that POST endpoint still requires specific route."""
    with TestClient(app) as client:
        # POST to collections endpoint should still work
        response = client.post(
            "/api/v1/test_server/api/v3/artifacts/collections/",
            content=b"test-data",
        )
        # Should require auth
        assert response.status_code == 401

        # POST to other endpoints should return 405 Method Not Allowed
        # (catch-all only handles GET, so POST is not allowed)
        response = client.post(
            "/api/v1/test_server/api/v3/some/other/endpoint/",
            content=b"test-data",
        )
        assert response.status_code == 405


@pytest.mark.asyncio
async def test_publish_rewrites_task_url_with_leading_slash(
    test_config_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that task URLs starting with '/' are rewritten to include proxy prefix.

    When Galaxy returns a task URL starting with '/', ansible-galaxy's urljoin
    will strip the server_id prefix unless we rewrite it to include the full path.
    """
    with TestClient(app) as client:
        # Mock dependencies
        mock_extract_path = "galaxy_publisher.main.extract_token_from_header"
        mock_validate_path = "galaxy_publisher.main.validate_jwt"
        mock_find_rule_path = "galaxy_publisher.main.find_authorization_rule"
        mock_verify_path = "galaxy_publisher.main.verify_server_access"
        mock_extract_tarball_path = "galaxy_publisher.main.extract_tarball_from_multipart"
        mock_extract_manifest_path = "galaxy_publisher.main.extract_manifest_from_tarball"
        mock_validate_collection_path = "galaxy_publisher.main.validate_collection_name"
        mock_proxy_path = "galaxy_publisher.main.proxy_request"

        with unittest.mock.patch(mock_extract_path) as mock_extract:
            with unittest.mock.patch(mock_validate_path) as mock_validate:
                with unittest.mock.patch(mock_find_rule_path) as mock_find_rule:
                    with unittest.mock.patch(mock_verify_path):
                        with unittest.mock.patch(mock_extract_tarball_path) as mock_tarball:
                            with unittest.mock.patch(mock_extract_manifest_path) as mock_manifest:
                                with unittest.mock.patch(mock_validate_collection_path):
                                    with unittest.mock.patch(mock_proxy_path) as mock_proxy:
                                        mock_extract.return_value = "test-token"
                                        mock_validate.return_value = {
                                            ISSUER_ID: "github",
                                            "repository": "myorg/myrepo",
                                        }
                                        mock_find_rule.return_value = AuthorizationRule(
                                            oidc_issuer="github",
                                            claims={"repository": "myorg/myrepo"},
                                            servers=["test_server"],
                                            allowed_collections=["myorg.collection"],
                                        )
                                        mock_tarball.return_value = b"tarball-data"
                                        mock_manifest.return_value = {
                                            "namespace": "myorg",
                                            "name": "collection",
                                        }

                                        # Galaxy returns task URL with leading slash
                                        galaxy_response = (
                                            b'{"task": "/api/v3/imports/collections/12345/"}'
                                        )
                                        mock_proxy.return_value = (
                                            202,
                                            {"content-type": "application/json"},
                                            galaxy_response,
                                        )

                                        response = client.post(
                                            "/api/v1/test_server/api/v3/artifacts/collections/",
                                            headers={"Authorization": "Token test-token"},
                                            content=b"collection-tarball",
                                        )

        assert response.status_code == 202
        data = response.json()
        # Task URL should be rewritten to include proxy prefix
        assert data["task"] == "/api/v1/test_server/api/v3/imports/collections/12345/"


@pytest.mark.asyncio
async def test_publish_preserves_task_url_without_leading_slash(
    test_config_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that task URLs without leading '/' are preserved as-is.

    If Galaxy returns a relative URL (no leading slash), we should preserve it
    since urljoin will append it correctly.
    """
    with TestClient(app) as client:
        # Mock dependencies
        mock_extract_path = "galaxy_publisher.main.extract_token_from_header"
        mock_validate_path = "galaxy_publisher.main.validate_jwt"
        mock_find_rule_path = "galaxy_publisher.main.find_authorization_rule"
        mock_verify_path = "galaxy_publisher.main.verify_server_access"
        mock_extract_tarball_path = "galaxy_publisher.main.extract_tarball_from_multipart"
        mock_extract_manifest_path = "galaxy_publisher.main.extract_manifest_from_tarball"
        mock_validate_collection_path = "galaxy_publisher.main.validate_collection_name"
        mock_proxy_path = "galaxy_publisher.main.proxy_request"

        with unittest.mock.patch(mock_extract_path) as mock_extract:
            with unittest.mock.patch(mock_validate_path) as mock_validate:
                with unittest.mock.patch(mock_find_rule_path) as mock_find_rule:
                    with unittest.mock.patch(mock_verify_path):
                        with unittest.mock.patch(mock_extract_tarball_path) as mock_tarball:
                            with unittest.mock.patch(mock_extract_manifest_path) as mock_manifest:
                                with unittest.mock.patch(mock_validate_collection_path):
                                    with unittest.mock.patch(mock_proxy_path) as mock_proxy:
                                        mock_extract.return_value = "test-token"
                                        mock_validate.return_value = {
                                            ISSUER_ID: "github",
                                            "repository": "myorg/myrepo",
                                        }
                                        mock_find_rule.return_value = AuthorizationRule(
                                            oidc_issuer="github",
                                            claims={"repository": "myorg/myrepo"},
                                            servers=["test_server"],
                                            allowed_collections=["myorg.collection"],
                                        )
                                        mock_tarball.return_value = b"tarball-data"
                                        mock_manifest.return_value = {
                                            "namespace": "myorg",
                                            "name": "collection",
                                        }

                                        # Galaxy returns task URL without leading slash (relative)
                                        galaxy_response = (
                                            b'{"task": "api/v3/imports/collections/12345/"}'
                                        )
                                        mock_proxy.return_value = (
                                            202,
                                            {"content-type": "application/json"},
                                            galaxy_response,
                                        )

                                        response = client.post(
                                            "/api/v1/test_server/api/v3/artifacts/collections/",
                                            headers={"Authorization": "Token test-token"},
                                            content=b"collection-tarball",
                                        )

        assert response.status_code == 202
        data = response.json()
        # Task URL should be preserved as-is (no leading slash)
        assert data["task"] == "api/v3/imports/collections/12345/"
