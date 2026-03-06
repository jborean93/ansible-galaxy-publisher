"""Pytest fixtures for integration testing."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tarfile
import tempfile
import time
import typing as t

import httpx
import pytest
import yaml

from tests.jwt_utils import JWTTestHelper


class ServerInfo:
    """Information about a running test server."""

    def __init__(self, process: subprocess.Popen[bytes], host: str = "127.0.0.1") -> None:
        """Initialize server info.

        Args:
            process: Server subprocess
            host: Host the server is listening on
        """
        self.process = process
        self.host = host
        self._port: int | None = None

    @property
    def port(self) -> int:
        """Get the port the server is listening on.

        Returns:
            Port number

        Raises:
            RuntimeError: If port hasn't been detected yet
        """
        if self._port is None:
            raise RuntimeError("Server port not yet detected")
        return self._port

    @property
    def url(self) -> str:
        """Get the base URL of the server.

        Returns:
            Base URL (e.g., "http://127.0.0.1:12345")
        """
        return f"http://{self.host}:{self.port}"


def _wait_for_server_port(process: subprocess.Popen[bytes], timeout: float = 3.0) -> int:
    """Wait for uvicorn server to start and extract its port from output.

    Args:
        process: Server subprocess
        timeout: Maximum time to wait in seconds

    Returns:
        Port number the server is listening on

    Raises:
        RuntimeError: If server fails to start or port can't be detected
    """
    start_time = time.time()
    port_pattern = re.compile(rb"Uvicorn running on http://127\.0\.0\.1:(\d+)")

    while time.time() - start_time < timeout:
        if process.stderr:
            # Read available stderr without blocking
            try:
                # Use a small timeout to avoid blocking forever
                line = process.stderr.readline()
                if line:
                    match = port_pattern.search(line)
                    if match:
                        return int(match.group(1))
            except Exception:
                pass

        # Check if process has died
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"Server process died\nSTDOUT: {stdout.decode()}\nSTDERR: {stderr.decode()}"
            )

        time.sleep(0.05)

    raise RuntimeError("Failed to detect server port within timeout")


@pytest.fixture(scope="session")
def jwt_helper() -> t.Iterator[JWTTestHelper]:
    """Create JWT test helper for the session.

    Yields:
        JWTTestHelper instance
    """
    # Use a shared key file so mock server can use same keys
    key_file = pathlib.Path(__file__).parent / "fixtures" / "keys" / "test_rsa_key.pem"
    helper = JWTTestHelper(key_file=str(key_file))
    yield helper
    # Cleanup key file
    if key_file.exists():
        key_file.unlink()


@pytest.fixture(scope="session")
def mock_galaxy_server(
    jwt_helper: JWTTestHelper,
) -> t.Iterator[ServerInfo]:
    """Start mock Galaxy server as subprocess with dynamic port.

    Args:
        jwt_helper: JWT helper (ensures key file exists before starting server)

    Yields:
        ServerInfo with process and port information
    """
    # Prepare environment for coverage in subprocess
    env = os.environ.copy()

    # Enable coverage in subprocess
    project_root = pathlib.Path(__file__).parent.parent
    env["COVERAGE_PROCESS_START"] = str(project_root / "pyproject.toml")

    # Add tests directory to PYTHONPATH for sitecustomize.py
    pythonpath = env.get("PYTHONPATH", "")
    tests_dir = str(pathlib.Path(__file__).parent)
    env["PYTHONPATH"] = f"{tests_dir}:{pythonpath}" if pythonpath else tests_dir

    # Start the mock server with port 0 (OS will assign available port)
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "tests.mock_galaxy_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            "0",  # Let OS pick an available port
        ],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start and extract the port
    try:
        port = _wait_for_server_port(process)
    except RuntimeError:
        process.kill()
        raise

    server_info = ServerInfo(process)
    server_info._port = port

    # Verify server is responding
    url = f"{server_info.url}/health"
    for _ in range(30):  # 30 attempts = 3 seconds
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.1)
    else:
        process.kill()
        raise RuntimeError(f"Mock Galaxy server not responding at {url}")

    yield server_info

    # Cleanup
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="session")
def test_config_file(
    mock_galaxy_server: ServerInfo,
) -> t.Iterator[str]:
    """Create test configuration file.

    Args:
        mock_galaxy_server: Mock Galaxy server info

    Yields:
        Path to config file
    """
    config_data = {
        "settings": {"audience": "https://example.com"},
        "oidc_issuers": {
            "github": {
                "issuer_url": "https://token.actions.githubusercontent.com",
                "jwks_url": f"{mock_galaxy_server.url}/.well-known/jwks",
            }
        },
        "servers": {
            "test_galaxy": {
                "base_url": mock_galaxy_server.url,
                "token": "TEST_GALAXY_TOKEN",
            }
        },
        "authorization_rules": [
            {
                "oidc_issuer": "github",
                "claims": {"repository": "myorg/*"},
                "servers": ["test_galaxy"],
                "allowed_collections": ["testnamespace.testcollection"],
            }
        ],
    }

    # Set environment variable for token
    os.environ["TEST_GALAXY_TOKEN"] = "test-galaxy-token-value"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml") as f:
        yaml.dump(config_data, f)
        f.flush()  # Ensure data is written
        config_path = f.name

        yield config_path

    # Cleanup
    os.environ.pop("TEST_GALAXY_TOKEN", None)


@pytest.fixture(scope="session")
def proxy_server(
    test_config_file: str,
    mock_galaxy_server: ServerInfo,
) -> t.Iterator[ServerInfo]:
    """Start proxy server as subprocess with dynamic port.

    Args:
        test_config_file: Path to config file
        mock_galaxy_server: Mock Galaxy server (dependency)

    Yields:
        ServerInfo with process and port information
    """
    # Prepare environment for coverage in subprocess
    env = os.environ.copy()
    env["CONFIG_PATH"] = test_config_file

    # Enable coverage in subprocess
    project_root = pathlib.Path(__file__).parent.parent
    env["COVERAGE_PROCESS_START"] = str(project_root / "pyproject.toml")

    # Add tests directory to PYTHONPATH for sitecustomize.py
    pythonpath = env.get("PYTHONPATH", "")
    tests_dir = str(pathlib.Path(__file__).parent)
    env["PYTHONPATH"] = f"{tests_dir}:{pythonpath}" if pythonpath else tests_dir

    # Start proxy with port 0 (OS will assign available port)
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "galaxy_publisher.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "0",  # Let OS pick an available port
        ],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start and extract the port
    try:
        port = _wait_for_server_port(process)
    except RuntimeError as e:
        # Get error output
        stdout, stderr = process.communicate(timeout=1)
        process.kill()
        error_msg = (
            f"Proxy server failed to start: {e}\n"
            f"STDOUT: {stdout.decode() if stdout else ''}\n"
            f"STDERR: {stderr.decode() if stderr else ''}"
        )
        raise RuntimeError(error_msg) from e

    server_info = ServerInfo(process)
    server_info._port = port

    # Verify server is responding
    url = f"{server_info.url}/health"
    for _ in range(30):  # 30 attempts = 3 seconds
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.1)
    else:
        # Get error output
        stdout, stderr = process.communicate(timeout=1)
        process.kill()
        error_msg = (
            f"Proxy server not responding at {url}\n"
            f"STDOUT: {stdout.decode() if stdout else ''}\n"
            f"STDERR: {stderr.decode() if stderr else ''}"
        )
        raise RuntimeError(error_msg)

    yield server_info

    # Cleanup
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture
def test_collection() -> t.Iterator[pathlib.Path]:
    """Build a test collection tarball.

    Yields:
        Path to collection tarball
    """
    # Create temporary directory for build
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = pathlib.Path(tmpdir)
        collection_dir = tmpdir_path / "testnamespace-testcollection-1.0.0"
        collection_dir.mkdir()

        # Copy collection files
        source_dir = pathlib.Path(__file__).parent / "fixtures" / "test-collection"
        for file in source_dir.iterdir():
            if file.is_file():
                (collection_dir / file.name).write_text(file.read_text())

        # Create MANIFEST.json
        manifest = {
            "collection_info": {
                "namespace": "testnamespace",
                "name": "testcollection",
                "version": "1.0.0",
                "authors": ["Test Author <test@example.com>"],
                "readme": "README.md",
                "tags": ["test"],
                "description": "Test collection",
                "license": ["MIT"],
            },
            "file_manifest_file": {
                "name": "FILES.json",
                "ftype": "file",
                "chksum_type": "sha256",
                "chksum_sha256": "placeholder",
            },
            "format": 1,
        }

        manifest_path = collection_dir / "MANIFEST.json"
        with open(manifest_path, "w") as f:
            import json

            json.dump(manifest, f)

        # Create FILES.json
        files_json = {"files": [], "format": 1}
        files_path = collection_dir / "FILES.json"
        with open(files_path, "w") as f:
            import json

            json.dump(files_json, f)

        # Create tarball with files at root
        tarball_path = tmpdir_path / "testnamespace-testcollection-1.0.0.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            # Add each file at root level (not in subdirectory)
            for file_path in collection_dir.iterdir():
                tar.add(file_path, arcname=file_path.name)

        yield tarball_path
