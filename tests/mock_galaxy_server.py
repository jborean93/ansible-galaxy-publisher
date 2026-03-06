"""Mock Galaxy server for integration testing."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import tarfile
import typing as t
import uuid

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from tests.jwt_utils import JWTTestHelper

# Global state for tasks and JWT helper
_tasks: dict[str, dict[str, t.Any]] = {}
_jwt_helper: JWTTestHelper | None = None


def set_jwt_helper(helper: JWTTestHelper) -> None:
    """Set the global JWT helper (called from tests)."""
    global _jwt_helper
    _jwt_helper = helper


def get_jwt_helper() -> JWTTestHelper:
    """Get or create the global JWT helper."""
    global _jwt_helper
    if _jwt_helper is None:
        # Check if key file is specified via environment variable
        key_file_env = os.environ.get("MOCK_GALAXY_KEY_FILE")
        if key_file_env:
            _jwt_helper = JWTTestHelper(key_file=key_file_env)
        else:
            # Use shared key file
            key_file = pathlib.Path(__file__).parent / "fixtures" / "keys" / "test_rsa_key.pem"
            _jwt_helper = JWTTestHelper(key_file=str(key_file))
    return _jwt_helper


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> t.AsyncIterator[None]:
    """Lifespan context manager for the app."""
    # Initialize JWT helper on startup if not set
    if _jwt_helper is None:
        get_jwt_helper()
    yield
    # Cleanup on shutdown
    _tasks.clear()


app = FastAPI(lifespan=lifespan)


@app.get("/.well-known/jwks")
async def jwks_endpoint() -> dict[str, t.Any]:
    """JWKS endpoint for token verification.

    Returns:
        JWKS with public keys
    """
    jwt_helper = get_jwt_helper()
    return jwt_helper.get_jwks()


@app.get("/api/")
async def api_discovery() -> dict[str, t.Any]:
    """Galaxy API discovery endpoint.

    Returns:
        API version information
    """
    return {
        "available_versions": {"v3": "v3/"},
        "current_version": "v3",
        "description": "Mock Galaxy API",
    }


@app.post("/api/v3/artifacts/collections/")
async def publish_collection(request: Request, file: UploadFile | None = File(None)) -> Response:
    """Accept collection upload and return task.

    Handles both multipart/form-data (httpx tests) and raw body (ansible-galaxy CLI).

    Args:
        request: HTTP request
        file: Optional uploaded file (from multipart)

    Returns:
        Task creation response

    Raises:
        HTTPException: If file validation fails
    """
    # Handle multipart upload (httpx tests with files= parameter)
    if file is None:
        raise HTTPException(status_code=400, detail="File upload is required")

    # Verify filename
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    # Verify content type (should be octet-stream or gzip)
    if file.content_type != "application/octet-stream":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid content type: {file.content_type}, "
                "expected application/octet-stream or application/gzip"
            ),
        )

    # Read the file content
    content = await file.read()

    # Validate content
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # Verify it's a valid tar.gz
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            # Verify MANIFEST.json exists
            manifest_found = False
            for member in tar.getmembers():
                if member.name == "MANIFEST.json":
                    manifest_found = True
                    break

            if not manifest_found:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid collection: MANIFEST.json not found in tarball",
                )
    except tarfile.TarError as e:
        raise HTTPException(status_code=400, detail=f"Invalid tarball: {e}")

    # Create a task
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "id": task_id,
        "state": "completed",
        "finished_at": "2024-01-01T00:00:00Z",
        "messages": [],
    }

    return JSONResponse(
        status_code=202,
        content={"task": f"/api/v3/imports/collections/{task_id}/"},
        headers={"Location": f"/api/v3/imports/collections/{task_id}/"},
    )


@app.get("/api/v3/imports/collections/{task_id}/")
async def get_task_status(task_id: str) -> dict[str, t.Any]:
    """Get collection import task status.

    Args:
        task_id: Task identifier

    Returns:
        Task status information
    """
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Return and remove task (simulate one-time status check)
    return _tasks.pop(task_id)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        Health status
    """
    return {"status": "healthy", "service": "mock-galaxy"}


if __name__ == "__main__":
    import argparse

    import uvicorn

    # Parse command line arguments (same as uvicorn CLI)
    parser = argparse.ArgumentParser(description="Run mock Galaxy server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument(
        "--port", type=int, default=8001, help="Port to bind to (0 for OS-assigned)"
    )
    args = parser.parse_args()

    # uvicorn.run() will print the actual port via its standard logging
    # Format: "Uvicorn running on http://127.0.0.1:{port}"
    uvicorn.run(app, host=args.host, port=args.port)
