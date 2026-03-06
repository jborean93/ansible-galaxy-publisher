"""Configuration loading and models."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import typing as t

import yaml


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Global settings."""

    audience: str

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create Settings from dictionary.

        Args:
            data: Dictionary containing settings data

        Returns:
            Validated Settings instance

        Raises:
            ValueError: If validation fails
        """
        audience = data.get("audience", None)
        if not audience:
            raise ValueError("Settings.audience is required")
        return cls(audience=audience)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class OIDCIssuer:
    """OIDC issuer configuration."""

    issuer_url: str
    jwks_url: str

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create OIDCIssuer from dictionary.

        Args:
            data: Dictionary containing issuer data

        Returns:
            Validated OIDCIssuer instance

        Raises:
            ValueError: If validation fails
        """
        issuer_url = data.get("issuer_url", None)
        if not issuer_url:
            raise ValueError("OIDCIssuer.issuer_url is required")

        jwks_url = data.get("jwks_url", None)
        if not jwks_url:
            raise ValueError("OIDCIssuer.jwks_url is required")

        return cls(issuer_url=issuer_url, jwks_url=jwks_url)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class OAuthSecret:
    """OAuth secret configuration."""

    client_id: str
    client_secret: str
    auth_url: str

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create OAuthSecret from dictionary.

        Args:
            data: Dictionary containing OAuth secret data

        Returns:
            Validated OAuthSecret instance

        Raises:
            ValueError: If validation fails
        """
        client_id = data.get("client_id", None)
        if not client_id:
            raise ValueError("OAuthSecret.client_id is required")

        client_id_resolved = os.environ.get(client_id, None)
        if not client_id_resolved:
            raise ValueError(f"OAuthSecret.client_id environment variable '{client_id}' not set")

        client_secret = data.get("client_secret", None)
        if not client_secret:
            raise ValueError("OAuthSecret.client_secret is required")

        client_secret_resolved = os.environ.get(client_secret, None)
        if not client_secret_resolved:
            raise ValueError(
                f"OAuthSecret.client_secret environment variable '{client_secret}' not set"
            )

        auth_url = data.get("auth_url", None)
        if not auth_url:
            raise ValueError("OAuthSecret.auth_url is required")

        return cls(
            client_id=client_id_resolved,
            client_secret=client_secret_resolved,
            auth_url=auth_url,
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Server:
    """Galaxy/Automation Hub server configuration."""

    base_url: str
    token: str | None = None
    oauth_secret: OAuthSecret | None = None

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create Server from dictionary.

        Args:
            data: Dictionary containing server data

        Returns:
            Validated Server instance

        Raises:
            ValueError: If validation fails
        """
        base_url = data.get("base_url", None)
        if not base_url:
            raise ValueError("Server.base_url is required")

        token = None
        oauth_secret = None

        if token_env := data.get("token", None):
            token = os.environ.get(token_env, None)
            if not token:
                raise ValueError(f"Server.token environment variable '{token_env}' not set")

        if oauth_secret_raw := data.get("oauth_secret", None):
            oauth_secret = OAuthSecret.from_dict(oauth_secret_raw)

        if token and oauth_secret:
            raise ValueError("Server cannot have both token and oauth_secret")
        if not token and not oauth_secret:
            raise ValueError("Server must have either token or oauth_secret")

        return cls(base_url=base_url, token=token, oauth_secret=oauth_secret)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizationRule:
    """Authorization rule for matching OIDC claims."""

    oidc_issuer: str
    claims: dict[str, str]
    servers: list[str]
    allowed_collections: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create AuthorizationRule from dictionary.

        Args:
            data: Dictionary containing authorization rule data

        Returns:
            Validated AuthorizationRule instance

        Raises:
            ValueError: If validation fails
        """
        oidc_issuer = data.get("oidc_issuer", None)
        if not oidc_issuer:
            raise ValueError("AuthorizationRule.oidc_issuer is required")

        claims = data.get("claims", None)
        if not claims:
            raise ValueError("AuthorizationRule.claims is required")

        servers = data.get("servers", None)
        if not servers:
            raise ValueError("AuthorizationRule.servers is required")

        allowed_collections = data.get("allowed_collections", None)
        if not allowed_collections:
            raise ValueError(
                "AuthorizationRule.allowed_collections is required and must be a "
                "non-empty list of exact collection names (e.g., ['namespace.collection'])"
            )

        return cls(
            oidc_issuer=oidc_issuer,
            claims=claims,
            servers=servers,
            allowed_collections=allowed_collections,
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Top-level configuration container."""

    settings: Settings
    oidc_issuers: dict[str, OIDCIssuer]
    servers: dict[str, Server]
    authorization_rules: list[AuthorizationRule]

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> t.Self:
        """Create Config from dictionary.

        Args:
            data: Dictionary containing config data

        Returns:
            Validated Config instance

        Raises:
            ValueError: If validation fails
        """
        settings_data = data.get("settings", None)
        if not settings_data:
            raise ValueError("Config.settings is required")

        settings = Settings.from_dict(settings_data)

        oidc_issuers_data = data.get("oidc_issuers", None)
        if not oidc_issuers_data:
            raise ValueError("Config.oidc_issuers is required")
        oidc_issuers = {
            issuer_id: OIDCIssuer.from_dict(issuer_data)
            for issuer_id, issuer_data in oidc_issuers_data.items()
        }

        servers_data = data.get("servers", None)
        if not servers_data:
            raise ValueError("Config.servers is required")
        servers = {
            server_id: Server.from_dict(server_data)
            for server_id, server_data in servers_data.items()
        }

        authorization_rules_data = data.get("authorization_rules", None)
        if authorization_rules_data is None:
            raise ValueError("Config.authorization_rules is required")
        authorization_rules = [
            AuthorizationRule.from_dict(rule_data) for rule_data in authorization_rules_data
        ]

        return cls(
            settings=settings,
            oidc_issuers=oidc_issuers,
            servers=servers,
            authorization_rules=authorization_rules,
        )


def load_config(config_path: str | pathlib.Path) -> Config:
    """Load configuration from YAML file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Loaded and validated Config object

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If configuration is invalid
        yaml.YAMLError: If YAML parsing fails
    """
    config_path = pathlib.Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return Config.from_dict(data)
