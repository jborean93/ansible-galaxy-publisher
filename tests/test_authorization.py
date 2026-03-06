"""Tests for authorization logic."""

import pytest

from galaxy_publisher.auth import ISSUER_ID
from galaxy_publisher.authorization import (
    AuthorizationError,
    find_authorization_rule,
    validate_collection_name,
    verify_server_access,
)
from galaxy_publisher.config import AuthorizationRule, Config, OIDCIssuer, Server, Settings


def create_test_config(authorization_rules: list[AuthorizationRule]) -> Config:
    """Create a test Config with standard settings and custom authorization rules.

    Args:
        authorization_rules: List of authorization rules to include

    Returns:
        Config object with test settings
    """
    return Config(
        settings=Settings(audience="https://example.com"),
        oidc_issuers={
            "github": OIDCIssuer(
                issuer_url="https://token.actions.githubusercontent.com",
                jwks_url="https://token.actions.githubusercontent.com/.well-known/jwks",
            )
        },
        servers={"test_server": Server(base_url="https://galaxy.example.com", token="test-token")},
        authorization_rules=authorization_rules,
    )


def test_find_authorization_rule_exact_match() -> None:
    """Test finding rule with exact claim match."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo", "ref": "refs/heads/main"},
                servers=["test_server"],
                allowed_collections=["myorg.collection"],
            )
        ]
    )

    claims = {
        "repository": "myorg/myrepo",
        "ref": "refs/heads/main",
        ISSUER_ID: "github",
    }

    rule = find_authorization_rule(claims, "github", config)

    assert rule is not None
    assert rule.oidc_issuer == "github"
    assert rule.claims == {"repository": "myorg/myrepo", "ref": "refs/heads/main"}


def test_find_authorization_rule_wildcard_match() -> None:
    """Test finding rule with wildcard claim match."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/*", "ref": "refs/tags/v*"},
                servers=["test_server"],
                allowed_collections=["myorg.collection"],
            )
        ]
    )

    claims = {
        "repository": "myorg/ansible-collection",
        "ref": "refs/tags/v1.2.3",
        ISSUER_ID: "github",
    }

    rule = find_authorization_rule(claims, "github", config)

    assert rule is not None
    assert rule.claims == {"repository": "myorg/*", "ref": "refs/tags/v*"}


def test_find_authorization_rule_no_match_issuer() -> None:
    """Test that wrong issuer returns None."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo"},
                servers=["test_server"],
                allowed_collections=["myorg.collection"],
            )
        ]
    )

    claims = {"repository": "myorg/myrepo", ISSUER_ID: "other_issuer"}

    rule = find_authorization_rule(claims, "other_issuer", config)

    assert rule is None


def test_find_authorization_rule_no_match_claims() -> None:
    """Test that non-matching claims return None."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo", "ref": "refs/heads/main"},
                servers=["test_server"],
                allowed_collections=["myorg.collection"],
            )
        ],
    )

    claims = {
        "repository": "myorg/other",  # Different repo
        "ref": "refs/heads/main",
        ISSUER_ID: "github",
    }

    rule = find_authorization_rule(claims, "github", config)

    assert rule is None


def test_find_authorization_rule_missing_claim() -> None:
    """Test that missing required claim returns None."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo", "ref": "refs/heads/main"},
                servers=["test_server"],
                allowed_collections=["myorg.collection"],
            )
        ]
    )

    claims = {
        "repository": "myorg/myrepo",
        # Missing 'ref' claim
        ISSUER_ID: "github",
    }

    rule = find_authorization_rule(claims, "github", config)

    assert rule is None


def test_find_authorization_rule_first_match() -> None:
    """Test that first matching rule is returned."""
    config = create_test_config(
        [
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo"},
                servers=["server1"],
                allowed_collections=["myorg.collection1"],
            ),
            AuthorizationRule(
                oidc_issuer="github",
                claims={"repository": "myorg/myrepo"},
                servers=["server2"],
                allowed_collections=["myorg.collection2"],
            ),
        ],
    )

    claims = {"repository": "myorg/myrepo", ISSUER_ID: "github"}

    rule = find_authorization_rule(claims, "github", config)

    assert rule is not None
    assert rule.servers == ["server1"]  # First rule matched


def test_verify_server_access_allowed() -> None:
    """Test that allowed server passes verification."""
    rule = AuthorizationRule(
        oidc_issuer="github",
        claims={"repository": "myorg/myrepo"},
        servers=["server1", "server2"],
        allowed_collections=["myorg.collection"],
    )

    # Should not raise
    verify_server_access(rule, "server1")
    verify_server_access(rule, "server2")


def test_verify_server_access_denied() -> None:
    """Test that disallowed server raises error."""
    rule = AuthorizationRule(
        oidc_issuer="github",
        claims={"repository": "myorg/myrepo"},
        servers=["server1"],
        allowed_collections=["myorg.collection"],
    )

    with pytest.raises(AuthorizationError, match="Not authorized to access server"):
        verify_server_access(rule, "server2")


def test_validate_collection_name_exact_match() -> None:
    """Test collection validation with exact match."""
    # Should not raise
    validate_collection_name("myorg", "mycollection", ["myorg.mycollection"])

    # Should raise - different collection
    with pytest.raises(AuthorizationError, match="Not authorized to publish collection"):
        validate_collection_name("myorg", "othercollection", ["myorg.mycollection"])

    # Should raise - different namespace
    with pytest.raises(AuthorizationError, match="Not authorized to publish collection"):
        validate_collection_name("otherorg", "mycollection", ["myorg.mycollection"])


def test_validate_collection_name_multiple_allowed() -> None:
    """Test collection validation with multiple allowed collections."""
    allowed = ["myorg.collection1", "myorg.collection2", "otherorg.specific"]

    # Should not raise - all exact matches
    validate_collection_name("myorg", "collection1", allowed)
    validate_collection_name("myorg", "collection2", allowed)
    validate_collection_name("otherorg", "specific", allowed)

    # Should raise - not in list
    with pytest.raises(AuthorizationError, match="Not authorized to publish collection"):
        validate_collection_name("myorg", "collection3", allowed)

    with pytest.raises(AuthorizationError, match="Not authorized to publish collection"):
        validate_collection_name("otherorg", "other", allowed)
