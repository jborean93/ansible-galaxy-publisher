"""JWT utilities for testing."""

from __future__ import annotations

import pathlib
import time
import typing as t

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey


class JWTTestHelper:
    """Helper class for creating and managing test JWTs."""

    def __init__(self, key_file: str | None = None) -> None:
        """Initialize with RSA keypair.

        Args:
            key_file: Optional path to load/save keys. If file exists, loads from it.
                      If file doesn't exist, generates new keys and saves them.
        """
        self.key_file = key_file
        if key_file and pathlib.Path(key_file).exists():
            self.private_key, self.public_key = self._load_keypair(key_file)
        else:
            self.private_key, self.public_key = self._generate_keypair()
            if key_file:
                self._save_keypair(key_file)

    def _generate_keypair(self) -> tuple[RSAPrivateKey, RSAPublicKey]:
        """Generate RSA keypair for signing JWTs.

        Returns:
            Tuple of (private_key, public_key)
        """
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        return private_key, public_key

    def _save_keypair(self, key_file: str) -> None:
        """Save keypair to file.

        Args:
            key_file: Path to save private key
        """
        private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        pathlib.Path(key_file).write_bytes(private_pem)

    def _load_keypair(self, key_file: str) -> tuple[RSAPrivateKey, RSAPublicKey]:
        """Load keypair from file.

        Args:
            key_file: Path to load private key from

        Returns:
            Tuple of (private_key, public_key)
        """
        private_pem = pathlib.Path(key_file).read_bytes()
        private_key = serialization.load_pem_private_key(
            private_pem,
            password=None,
        )
        assert isinstance(private_key, RSAPrivateKey)
        public_key = private_key.public_key()
        return private_key, public_key

    def create_token(
        self,
        issuer: str = "https://token.actions.githubusercontent.com",
        audience: str = "https://example.com",
        subject: str = "repo:myorg/myrepo:ref:refs/heads/main",
        repository: str = "myorg/myrepo",
        ref: str = "refs/heads/main",
        **extra_claims: t.Any,
    ) -> str:
        """Create a signed JWT token.

        Args:
            issuer: Token issuer (iss claim)
            audience: Token audience (aud claim)
            subject: Token subject (sub claim)
            repository: Repository claim for GitHub
            ref: Git ref claim for GitHub
            **extra_claims: Additional claims to include

        Returns:
            Signed JWT token string
        """
        now = int(time.time())
        payload = {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "exp": now + 3600,  # Expires in 1 hour
            "iat": now,
            "nbf": now,
            "repository": repository,
            "ref": ref,
            **extra_claims,
        }

        private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        return jwt.encode(payload, private_pem, algorithm="RS256")

    def create_expired_token(
        self,
        issuer: str = "https://token.actions.githubusercontent.com",
        audience: str = "https://example.com",
        **extra_claims: t.Any,
    ) -> str:
        """Create an expired JWT token.

        Args:
            issuer: Token issuer
            audience: Token audience
            **extra_claims: Additional claims

        Returns:
            Expired signed JWT token
        """
        now = int(time.time())
        payload = {
            "iss": issuer,
            "aud": audience,
            "sub": "repo:test/test:ref:refs/heads/main",
            "exp": now - 3600,  # Expired 1 hour ago
            "iat": now - 7200,
            "nbf": now - 7200,
            "repository": "test/test",
            **extra_claims,
        }

        private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        return jwt.encode(payload, private_pem, algorithm="RS256")

    def get_jwks(self) -> dict[str, t.Any]:
        """Get JWKS (JSON Web Key Set) for the public key.

        Returns:
            JWKS dictionary with public key
        """
        # Convert PEM to JWK format
        # For testing, we'll use PyJWT's built-in conversion
        from jwt.algorithms import RSAAlgorithm

        public_key_jwk = RSAAlgorithm.to_jwk(self.public_key, as_dict=True)

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "kid": "test-key-1",
                    **public_key_jwk,
                }
            ]
        }
