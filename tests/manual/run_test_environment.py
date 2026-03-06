#!/usr/bin/env python3
"""Manual test environment setup script.

This script sets up a complete test environment with:
- Mock Galaxy server with JWKS endpoint
- Proxy server with generated configuration
- JWT token for authentication
- ansible.cfg for ansible-galaxy CLI

Press Ctrl+C to stop and cleanup.
"""

import argparse
import getpass
import logging
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import tempfile
import time

import httpx
import yaml

# Add parent directory to path to import jwt_utils
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from jwt_utils import JWTTestHelper  # type: ignore[import-not-found]

logger = logging.getLogger(pathlib.Path(__file__).name)


class TestEnvironment:
    """Manages the test environment lifecycle."""

    def __init__(
        self,
        audience: str,
        claim_value: str,
        server_url: str,
        allowed_collections: list[str],
        galaxy_token: str,
        log_level: str,
    ) -> None:
        """Initialize test environment.

        Args:
            audience: OIDC audience value
            claim_value: Value for test_oidc_claim
            server_url: Galaxy server base URL
            allowed_collections: List of allowed collection names (namespace.name)
            galaxy_token: Galaxy server API token
            log_level: Logging level (INFO or DEBUG)
        """
        self.audience = audience
        self.claim_value = claim_value
        self.server_url = server_url
        self.allowed_collections = allowed_collections
        self.galaxy_token = galaxy_token
        self.log_level = log_level

        # Runtime state
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.temp_path: pathlib.Path | None = None
        self.jwt_helper: JWTTestHelper | None = None
        self.jwt_token: str | None = None
        self.mock_galaxy_process: subprocess.Popen[bytes] | None = None
        self.mock_galaxy_port: int | None = None
        self.proxy_process: subprocess.Popen[bytes] | None = None
        self.proxy_port: int | None = None
        self.config_path: pathlib.Path | None = None

    def setup(self) -> None:
        """Set up the complete test environment."""
        logger.info("Setting up test environment...")

        # Create temporary directory
        self.temp_dir = tempfile.TemporaryDirectory(prefix="galaxy-publisher-test-")
        self.temp_path = pathlib.Path(self.temp_dir.name)
        logger.info("Created temporary directory: %s", self.temp_path)

        # Generate JWKS keypair and save to file
        logger.info("Generating JWKS keypair...")
        key_file = self.temp_path / "test_rsa_key.pem"
        self.jwt_helper = JWTTestHelper(key_file=str(key_file))
        logger.debug("Private key saved to: %s", key_file)

        # Create JWT token with claim
        logger.info("Creating JWT token with claim test_oidc_claim=%s", self.claim_value)
        self.jwt_token = self.jwt_helper.create_token(
            test_oidc_claim=self.claim_value,
            aud=self.audience,
            iss="https://test.oidc.issuer",
        )
        token_file = self.temp_path / "token.jwt"
        token_file.write_text(self.jwt_token)
        logger.debug("JWT token saved to: %s", token_file)

        # Start mock Galaxy server
        self._start_mock_galaxy_server()

        # Create servers.yml configuration
        self._create_servers_config()

        # Start proxy server
        self._start_proxy_server()

        # Display startup information
        self._display_info()

    def _start_mock_galaxy_server(self) -> None:
        """Start the mock Galaxy server with JWKS endpoint."""
        logger.info("Starting mock Galaxy server...")

        assert self.temp_path is not None
        key_file = self.temp_path / "test_rsa_key.pem"

        # Start with port 0 to let OS assign
        cmd = [
            "python",
            "-m",
            "tests.mock_galaxy_server",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ]

        self.mock_galaxy_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "MOCK_GALAXY_KEY_FILE": str(key_file)},
        )

        # Wait for server to start and get port
        self.mock_galaxy_port = self._wait_for_server_port(self.mock_galaxy_process, "mock Galaxy")
        logger.info("Mock Galaxy server started on port %d", self.mock_galaxy_port)

        # Verify JWKS endpoint is available
        jwks_url = f"http://127.0.0.1:{self.mock_galaxy_port}/.well-known/jwks"
        for _ in range(30):
            try:
                response = httpx.get(jwks_url, timeout=1.0)
                if response.status_code == 200:
                    logger.debug("JWKS endpoint available at %s", jwks_url)
                    break
            except (httpx.ConnectError, httpx.ReadTimeout):
                time.sleep(0.1)
        else:
            raise RuntimeError("Mock Galaxy server JWKS endpoint not responding")

    def _start_proxy_server(self) -> None:
        """Start the proxy server."""
        logger.info("Starting proxy server...")

        # Start with port 0 to let OS assign
        cmd = [
            "python",
            "-m",
            "uvicorn",
            "galaxy_publisher.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--log-level",
            self.log_level.lower(),
        ]

        env = {
            **os.environ,
            "CONFIG_PATH": str(self.config_path),
            "TEST_GALAXY_TOKEN": self.galaxy_token,
        }

        self.proxy_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Wait for server to start and get port
        self.proxy_port = self._wait_for_server_port(self.proxy_process, "proxy")
        logger.info("Proxy server started on port %d", self.proxy_port)

    def _create_servers_config(self) -> None:
        """Create servers.yml configuration file."""
        logger.info("Creating servers.yml configuration...")

        config = {
            "settings": {"audience": self.audience},
            "oidc_issuers": {
                "test_issuer": {
                    "issuer_url": "https://test.oidc.issuer",
                    "jwks_url": f"http://127.0.0.1:{self.mock_galaxy_port}/.well-known/jwks",
                }
            },
            "servers": {
                "test_server": {
                    "base_url": self.server_url,
                    "token": "TEST_GALAXY_TOKEN",
                }
            },
            "authorization_rules": [
                {
                    "oidc_issuer": "test_issuer",
                    "claims": {"test_oidc_claim": self.claim_value},
                    "servers": ["test_server"],
                    "allowed_collections": self.allowed_collections,
                }
            ],
        }

        assert self.temp_path is not None
        self.config_path = self.temp_path / "servers.yml"
        with open(self.config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        logger.debug("Configuration saved to: %s", self.config_path)

    def _wait_for_server_port(self, process: subprocess.Popen[bytes], name: str) -> int:
        """Wait for server to start and extract the port number.

        Args:
            process: Server subprocess
            name: Server name for logging

        Returns:
            Port number

        Raises:
            RuntimeError: If server fails to start
        """
        import re

        # Wait for "Uvicorn running on http://127.0.0.1:PORT" message
        assert process.stderr is not None
        start_time = time.time()
        while time.time() - start_time < 10:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise RuntimeError(
                    f"{name} server died\n"
                    f"STDOUT: {stdout.decode() if stdout else ''}\n"
                    f"STDERR: {stderr.decode() if stderr else ''}"
                )

            line = process.stderr.readline().decode()
            if not line:
                time.sleep(0.1)
                continue

            # Look for Uvicorn port message
            match = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", line)
            if match:
                return int(match.group(1))

        raise RuntimeError(f"{name} server failed to start within 10 seconds")

    def _display_info(self) -> None:
        """Display startup information to user."""
        assert self.temp_path is not None
        assert self.jwt_token is not None

        # Mask token for display (show first/last 4 chars)
        token_display = (
            f"{self.galaxy_token[:4]}...{self.galaxy_token[-4:]}"
            if len(self.galaxy_token) > 8
            else "****"
        )

        # Build the proxy URL
        proxy_url = f"http://127.0.0.1:{self.proxy_port}/api/v1/test_server"
        token_file = self.temp_path / "token.jwt"

        print("\n" + "=" * 70)
        print("🚀 Test Environment Started")
        print("=" * 70)
        print(f"\n📁 Working Directory: {self.temp_path}")
        print("\n🔐 OIDC Configuration:")
        print(f"   Audience:     {self.audience}")
        print("   Issuer:       https://test.oidc.issuer")
        print(f"   Claim:        test_oidc_claim={self.claim_value}")
        print("\n🌐 Servers:")
        print(f"   Mock Galaxy:  http://127.0.0.1:{self.mock_galaxy_port}")
        print(f"   Proxy:        http://127.0.0.1:{self.proxy_port}")
        print(f"   Target:       {self.server_url}")
        print(f"   Token:        {token_display}")
        print("\n📦 Allowed Collections:")
        for collection in self.allowed_collections:
            print(f"   • {collection}")
        print("\n🔧 Configuration Files:")
        print(f"   servers.yml:  {self.config_path}")
        print(f"   JWT token:    {token_file}")
        print("\n📝 To publish a collection, run:")
        print("\n   ansible-galaxy collection publish \\")
        print(f"     --server {proxy_url} \\")
        print(f'     --token "$(cat {token_file})" \\')
        print("     <collection-tarball.tar.gz>")
        print("\n💡 Tips:")
        print("   • Collection name must exactly match allowed collections above")
        print("   • Build collection with: ansible-galaxy collection build")
        print("   • Check proxy logs above for request/response details")
        print("\n⏸️  Press Ctrl+C to stop and cleanup...")
        print("=" * 70 + "\n")

    def wait_for_interrupt(self) -> None:
        """Wait for Ctrl+C signal."""
        try:
            # Keep processes alive and show their output
            signal.signal(signal.SIGINT, lambda s, f: None)  # Ignore first Ctrl+C

            logger.info("Test environment running. Press Ctrl+C to stop...")

            # Stream proxy output to console
            if self.proxy_process and self.proxy_process.stderr:
                try:
                    for line in self.proxy_process.stderr:
                        print(line.decode(), end="")
                except KeyboardInterrupt:
                    pass

        except KeyboardInterrupt:
            pass

        logger.info("Shutting down...")

    def cleanup(self) -> None:
        """Clean up all resources."""
        logger.info("Cleaning up test environment...")

        # Stop proxy server
        if self.proxy_process:
            logger.debug("Stopping proxy server...")
            self.proxy_process.terminate()
            try:
                self.proxy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proxy_process.kill()
                self.proxy_process.wait()

        # Stop mock Galaxy server
        if self.mock_galaxy_process:
            logger.debug("Stopping mock Galaxy server...")
            self.mock_galaxy_process.terminate()
            try:
                self.mock_galaxy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mock_galaxy_process.kill()
                self.mock_galaxy_process.wait()

        # Clean up temporary directory
        if self.temp_dir:
            logger.debug("Removing temporary directory...")
            self.temp_dir.cleanup()

        logger.info("Cleanup complete")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Run a complete test environment for Galaxy Publisher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with default settings (token from env or prompted)
  %(prog)s myorg.mycollection

  # Specify token on command line
  %(prog)s --token your-token myorg.mycollection

  # Custom audience and claim
  %(prog)s --audience myapp.example.com --claim myrepo myorg.mycollection

  # Multiple collections with debug logging
  %(prog)s --log-level DEBUG myorg.collection1 myorg.collection2
        """,
    )

    parser.add_argument(
        "--audience",
        default="galaxy-test.oidc.publisher",
        help="OIDC audience value (default: galaxy-test.oidc.publisher)",
    )

    parser.add_argument(
        "--claim",
        default=None,
        help="Value for test_oidc_claim (default: random string)",
    )

    parser.add_argument(
        "--server",
        default="https://galaxy.ansible.com",
        help="Galaxy server base URL (default: https://galaxy.ansible.com)",
    )

    parser.add_argument(
        "--token",
        default=os.environ.get("TEST_GALAXY_TOKEN"),
        help="Galaxy server API token (default: TEST_GALAXY_TOKEN env var, or prompt)",
    )

    parser.add_argument(
        "--log-level",
        choices=["INFO", "DEBUG"],
        default="INFO",
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "allowed_collections",
        nargs="+",
        help="Allowed collection names (e.g., myorg.mycollection)",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate collection names (no wildcards allowed)
    for collection in args.allowed_collections:
        if "*" in collection:
            parser.error(
                f"Collection name cannot contain wildcards: {collection}\n"
                f"Use specific collection names like 'namespace.name'"
            )
        if "." not in collection:
            parser.error(f"Collection name must be in format 'namespace.name': {collection}")

    # Get Galaxy token (from arg, env, or prompt)
    galaxy_token = args.token
    if not galaxy_token:
        try:
            galaxy_token = getpass.getpass("Enter Galaxy server API token: ")
            if not galaxy_token:
                parser.error("Galaxy token is required")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted")
            sys.exit(1)

    # Validate server and token before starting environment
    logger.info("Validating server connection and token...")
    api_url = f"{args.server.rstrip('/')}/api/"

    try:
        response = httpx.get(
            api_url,
            headers={"Authorization": f"Token {galaxy_token}"},
            timeout=10.0,
            follow_redirects=True,
        )

        if response.status_code == 401:
            print(f"\n❌ Authentication failed: Invalid token for {args.server}")
            print(f"\nServer response (HTTP {response.status_code}):")
            print("-" * 70)
            try:
                # Try to pretty-print JSON response
                import json

                response_data = response.json()
                print(json.dumps(response_data, indent=2))
            except Exception:
                # Fall back to raw text
                print(response.text)
            print("-" * 70)
            print("\n💡 Check that your token is valid and has the correct permissions.")
            sys.exit(1)
        elif response.status_code >= 400:
            print(f"\n❌ Server request failed: {api_url}")
            print(f"HTTP {response.status_code}: {response.reason_phrase}")
            if response.text:
                print(f"\nResponse:\n{response.text[:500]}")
            sys.exit(1)
        else:
            logger.info("✓ Server connection successful (HTTP %d)", response.status_code)

    except httpx.ConnectError as e:
        print(f"\n❌ Failed to connect to server: {args.server}")
        print(f"Error: {e}")
        print("\n💡 Check that the server URL is correct and accessible.")
        sys.exit(1)
    except httpx.TimeoutException:
        print(f"\n❌ Connection timeout to server: {args.server}")
        print("\n💡 Check your network connection and server availability.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error validating server: {e}")
        sys.exit(1)

    # Generate random claim if not specified
    claim_value = args.claim or secrets.token_hex(8)

    # Create and setup environment
    env = TestEnvironment(
        audience=args.audience,
        claim_value=claim_value,
        server_url=args.server,
        allowed_collections=args.allowed_collections,
        galaxy_token=galaxy_token,
        log_level=args.log_level,
    )

    try:
        env.setup()
        env.wait_for_interrupt()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("Failed to start test environment: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        env.cleanup()


if __name__ == "__main__":
    main()
