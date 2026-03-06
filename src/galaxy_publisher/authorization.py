"""Authorization logic for matching OIDC claims and rules."""

from __future__ import annotations

import fnmatch
import typing as t

from galaxy_publisher.config import AuthorizationRule, Config


class AuthorizationError(Exception):
    """Authorization failed."""

    pass


def find_authorization_rule(
    claims: dict[str, t.Any],
    oidc_issuer_id: str,
    config: Config,
) -> AuthorizationRule | None:
    """Find matching authorization rule for claims.

    Args:
        claims: JWT claims dictionary
        oidc_issuer_id: OIDC issuer identifier
        config: Configuration object

    Returns:
        Matching AuthorizationRule or None if no match
    """

    for rule in config.authorization_rules:
        # Check if issuer matches
        if rule.oidc_issuer != oidc_issuer_id:
            continue

        # Check if all claim patterns match
        all_claims_match = True
        for claim_name, claim_pattern in rule.claims.items():
            if claim_name not in claims:
                all_claims_match = False
                break

            claim_value = str(claims[claim_name])
            if not fnmatch.fnmatch(claim_value, claim_pattern):
                all_claims_match = False
                break

        if all_claims_match:
            return rule

    return None


def verify_server_access(rule: AuthorizationRule, server_id: str) -> None:
    """Verify that rule allows access to server.

    Args:
        rule: Authorization rule
        server_id: Server identifier

    Raises:
        AuthorizationError: If server access not allowed
    """
    if server_id not in rule.servers:
        raise AuthorizationError(
            f"Not authorized to access server '{server_id}'. "
            f"Allowed servers: {', '.join(rule.servers)}"
        )


def validate_collection_name(namespace: str, name: str, allowed_collections: list[str]) -> None:
    """Validate that collection is allowed by authorization rule.

    Args:
        namespace: Collection namespace
        name: Collection name
        allowed_collections: List of allowed collection names (exact matches only)

    Raises:
        AuthorizationError: If collection not allowed

    Examples:
        >>> validate_collection_name("myorg", "mycol", ["myorg.mycol"])
        >>> validate_collection_name("myorg", "mycol", ["myorg.other"])
        Traceback (most recent call last):
            ...
        AuthorizationError: ...
    """
    collection_full_name = f"{namespace}.{name}"

    # Only exact matches allowed (no wildcards)
    if collection_full_name in allowed_collections:
        return

    raise AuthorizationError(
        f"Not authorized to publish collection '{collection_full_name}'. "
        f"Allowed collections: {', '.join(allowed_collections)}"
    )
