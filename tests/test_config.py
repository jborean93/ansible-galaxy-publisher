"""Tests for configuration loading."""

import os
import tempfile

import pytest
import yaml

from galaxy_publisher.config import (
    AuthorizationRule,
    Config,
    OAuthSecret,
    OIDCIssuer,
    Server,
    Settings,
    load_config,
)

# Integration tests for file loading


def test_load_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test loading a valid configuration file."""
    monkeypatch.setenv("TEST_VALID_TOKEN", "test-token-value")

    config_data = {
        "settings": {"audience": "https://example.com"},
        "oidc_issuers": {
            "github": {
                "issuer_url": "https://token.actions.githubusercontent.com",
                "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            }
        },
        "servers": {
            "test_server": {
                "base_url": "https://galaxy.example.com",
                "token": "TEST_VALID_TOKEN",
            }
        },
        "authorization_rules": [
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["test_server"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = f.name

    try:
        config = load_config(config_path)

        assert isinstance(config, Config)
        assert config.settings.audience == "https://example.com"
        assert "github" in config.oidc_issuers
        assert (
            config.oidc_issuers["github"].issuer_url
            == "https://token.actions.githubusercontent.com"
        )
        assert "test_server" in config.servers
        assert config.servers["test_server"].base_url == "https://galaxy.example.com"
        assert config.servers["test_server"].token == "test-token-value"
        assert len(config.authorization_rules) == 1
        assert config.authorization_rules[0].oidc_issuer == "github"
        assert config.authorization_rules[0].claims == {"repository": "myorg/myrepo"}
        assert config.authorization_rules[0].servers == ["test_server"]
        assert config.authorization_rules[0].allowed_collections == ["testnamespace.testcollection"]
    finally:
        os.unlink(config_path)


def test_load_config_with_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test loading configuration with environment variable resolution."""
    monkeypatch.setenv("TEST_TOKEN", "secret-token-123")
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id-456")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "client-secret-789")

    config_data = {
        "settings": {"audience": "https://example.com"},
        "oidc_issuers": {
            "github": {
                "issuer_url": "https://token.actions.githubusercontent.com",
                "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            }
        },
        "servers": {
            "server1": {"base_url": "https://galaxy1.example.com", "token": "TEST_TOKEN"},
            "server2": {
                "base_url": "https://galaxy2.example.com",
                "oauth_secret": {
                    "client_id": "TEST_CLIENT_ID",
                    "client_secret": "TEST_CLIENT_SECRET",
                    "auth_url": "https://auth.example.com/token",
                },
            },
        },
        "authorization_rules": [
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["server1", "server2"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = f.name

    try:
        config = load_config(config_path)

        assert config.servers["server1"].token == "secret-token-123"
        assert config.servers["server2"].oauth_secret is not None
        assert config.servers["server2"].oauth_secret.client_id == "client-id-456"
        assert config.servers["server2"].oauth_secret.client_secret == "client-secret-789"
    finally:
        os.unlink(config_path)


def test_load_config_missing_env_var() -> None:
    """Test that missing environment variable raises error."""
    config_data = {
        "settings": {"audience": "https://example.com"},
        "oidc_issuers": {
            "github": {
                "issuer_url": "https://token.actions.githubusercontent.com",
                "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            }
        },
        "servers": {
            "test_server": {
                "base_url": "https://galaxy.example.com",
                "token": "MISSING_ENV_VAR",
            }
        },
        "authorization_rules": [
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["test_server"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="environment variable 'MISSING_ENV_VAR' not set"):
            load_config(config_path)
    finally:
        os.unlink(config_path)


def test_load_config_file_not_found() -> None:
    """Test that missing config file raises error."""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.yml")


def test_load_config_invalid_yaml() -> None:
    """Test that invalid YAML raises error."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("invalid: yaml: content: [")
        config_path = f.name

    try:
        with pytest.raises(yaml.YAMLError):
            load_config(config_path)
    finally:
        os.unlink(config_path)


# Settings validation tests


def test_settings_missing_audience() -> None:
    """Test that Settings requires audience."""
    with pytest.raises(ValueError, match="Settings.audience is required"):
        Settings.from_dict({})


# OIDCIssuer validation tests


def test_oidc_issuer_missing_issuer_url() -> None:
    """Test that OIDCIssuer requires issuer_url."""
    with pytest.raises(ValueError, match="OIDCIssuer.issuer_url is required"):
        OIDCIssuer.from_dict({"jwks_url": "https://example.com/.well-known/jwks"})


def test_oidc_issuer_missing_jwks_url() -> None:
    """Test that OIDCIssuer requires jwks_url."""
    with pytest.raises(ValueError, match="OIDCIssuer.jwks_url is required"):
        OIDCIssuer.from_dict({"issuer_url": "https://example.com"})


# OAuthSecret validation tests


def test_oauth_secret_missing_client_id() -> None:
    """Test that OAuthSecret requires client_id."""
    with pytest.raises(ValueError, match="OAuthSecret.client_id is required"):
        OAuthSecret.from_dict(
            {"client_secret": "secret", "auth_url": "https://auth.example.com/token"}
        )


def test_oauth_secret_missing_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OAuthSecret requires client_secret."""
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id")

    with pytest.raises(ValueError, match="OAuthSecret.client_secret is required"):
        OAuthSecret.from_dict(
            {"client_id": "TEST_CLIENT_ID", "auth_url": "https://auth.example.com/token"}
        )


def test_oauth_secret_missing_auth_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OAuthSecret requires auth_url."""
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "secret")

    with pytest.raises(ValueError, match="OAuthSecret.auth_url is required"):
        OAuthSecret.from_dict(
            {"client_id": "TEST_CLIENT_ID", "client_secret": "TEST_CLIENT_SECRET"}
        )


def test_oauth_secret_client_id_env_var_not_set() -> None:
    """Test that OAuthSecret validates client_id environment variable exists."""
    with pytest.raises(
        ValueError,
        match="OAuthSecret.client_id environment variable 'MISSING_CLIENT_ID' not set",
    ):
        OAuthSecret.from_dict(
            {
                "client_id": "MISSING_CLIENT_ID",
                "client_secret": "TEST_SECRET",
                "auth_url": "https://auth.example.com/token",
            }
        )


def test_oauth_secret_client_secret_env_var_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OAuthSecret validates client_secret environment variable exists."""
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id")

    with pytest.raises(
        ValueError,
        match="OAuthSecret.client_secret environment variable 'MISSING_SECRET' not set",
    ):
        OAuthSecret.from_dict(
            {
                "client_id": "TEST_CLIENT_ID",
                "client_secret": "MISSING_SECRET",
                "auth_url": "https://auth.example.com/token",
            }
        )


# Server validation tests


def test_server_missing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Server requires base_url."""
    monkeypatch.setenv("TEST_TOKEN", "test-token")

    with pytest.raises(ValueError, match="Server.base_url is required"):
        Server.from_dict({"token": "TEST_TOKEN"})


def test_server_needs_auth_method() -> None:
    """Test that Server needs either token or oauth_secret."""
    with pytest.raises(ValueError, match="Server must have either token or oauth_secret"):
        Server.from_dict({"base_url": "https://galaxy.example.com"})


def test_server_cannot_have_both_token_and_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Server cannot have both token and oauth_secret."""
    monkeypatch.setenv("BOTH_TOKEN", "test-token")
    monkeypatch.setenv("BOTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("BOTH_CLIENT_SECRET", "client-secret")

    with pytest.raises(ValueError, match="Server cannot have both token and oauth_secret"):
        Server.from_dict(
            {
                "base_url": "https://galaxy.example.com",
                "token": "BOTH_TOKEN",
                "oauth_secret": {
                    "client_id": "BOTH_CLIENT_ID",
                    "client_secret": "BOTH_CLIENT_SECRET",
                    "auth_url": "https://auth.example.com/token",
                },
            }
        )


# AuthorizationRule validation tests


def test_authorization_rule_missing_oidc_issuer() -> None:
    """Test that AuthorizationRule requires oidc_issuer."""
    with pytest.raises(ValueError, match="AuthorizationRule.oidc_issuer is required"):
        AuthorizationRule.from_dict(
            {
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["test_server"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        )


def test_authorization_rule_missing_claims() -> None:
    """Test that AuthorizationRule requires claims."""
    with pytest.raises(ValueError, match="AuthorizationRule.claims is required"):
        AuthorizationRule.from_dict(
            {
                "oidc_issuer": "github",
                "servers": ["test_server"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        )


def test_authorization_rule_missing_servers() -> None:
    """Test that AuthorizationRule requires servers."""
    with pytest.raises(ValueError, match="AuthorizationRule.servers is required"):
        AuthorizationRule.from_dict(
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "allowed_collections": ["testnamespace.testcollection"],
            }
        )


def test_authorization_rule_missing_allowed_collections() -> None:
    """Test that AuthorizationRule requires allowed_collections."""
    with pytest.raises(ValueError, match="allowed_collections is required"):
        AuthorizationRule.from_dict(
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/myrepo"},
                "servers": ["test_server"],
            }
        )


# Config validation tests


def test_config_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Config requires settings."""
    monkeypatch.setenv("TEST_TOKEN", "test-token")

    with pytest.raises(ValueError, match="Config.settings is required"):
        Config.from_dict(
            {
                # Missing settings
                "oidc_issuers": {
                    "github": {
                        "issuer_url": "https://token.actions.githubusercontent.com",
                        "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
                    }
                },
                "servers": {
                    "test_server": {"base_url": "https://galaxy.example.com", "token": "TEST_TOKEN"}
                },
                "authorization_rules": [
                    {
                        "oidc_issuer": "github",
                        "claims": {"repository": "myorg/myrepo"},
                        "servers": ["test_server"],
                        "allowed_collections": ["testnamespace.testcollection"],
                    }
                ],
            }
        )


def test_config_missing_oidc_issuers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Config requires oidc_issuers."""
    monkeypatch.setenv("TEST_TOKEN", "test-token")

    with pytest.raises(ValueError, match="Config.oidc_issuers is required"):
        Config.from_dict(
            {
                "settings": {"audience": "https://example.com"},
                "servers": {
                    "test_server": {"base_url": "https://galaxy.example.com", "token": "TEST_TOKEN"}
                },
                "authorization_rules": [
                    {
                        "oidc_issuer": "github",
                        "claims": {"repository": "myorg/myrepo"},
                        "servers": ["test_server"],
                        "allowed_collections": ["testnamespace.testcollection"],
                    }
                ],
            }
        )


def test_config_missing_servers() -> None:
    """Test that Config requires servers."""
    with pytest.raises(ValueError, match="Config.servers is required"):
        Config.from_dict(
            {
                "settings": {"audience": "https://example.com"},
                "oidc_issuers": {
                    "github": {
                        "issuer_url": "https://token.actions.githubusercontent.com",
                        "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
                    }
                },
                "authorization_rules": [
                    {
                        "oidc_issuer": "github",
                        "claims": {"repository": "myorg/myrepo"},
                        "servers": ["test_server"],
                        "allowed_collections": ["testnamespace.testcollection"],
                    }
                ],
            }
        )


def test_config_missing_authorization_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Config requires authorization_rules field."""
    monkeypatch.setenv("TEST_TOKEN", "test-token")

    with pytest.raises(ValueError, match="Config.authorization_rules is required"):
        Config.from_dict(
            {
                "settings": {"audience": "https://example.com"},
                "oidc_issuers": {
                    "github": {
                        "issuer_url": "https://token.actions.githubusercontent.com",
                        "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
                    }
                },
                "servers": {
                    "test_server": {"base_url": "https://galaxy.example.com", "token": "TEST_TOKEN"}
                },
            }
        )


def test_config_empty_authorization_rules_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that empty authorization_rules list is valid."""
    monkeypatch.setenv("TEST_TOKEN", "test-token")

    config = Config.from_dict(
        {
            "settings": {"audience": "https://example.com"},
            "oidc_issuers": {
                "github": {
                    "issuer_url": "https://token.actions.githubusercontent.com",
                    "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
                }
            },
            "servers": {
                "test_server": {"base_url": "https://galaxy.example.com", "token": "TEST_TOKEN"}
            },
            "authorization_rules": [],
        }
    )

    assert config.authorization_rules == []
