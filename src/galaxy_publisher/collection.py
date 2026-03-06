"""Collection validation and metadata extraction."""

from __future__ import annotations

import email
import io
import json
import tarfile
import typing as t


class CollectionValidationError(Exception):
    """Collection validation failed."""

    pass


def extract_manifest_from_tarball(file_content: bytes) -> dict[str, t.Any]:
    """Extract MANIFEST.json from collection tarball.

    Args:
        file_content: Raw tarball bytes

    Returns:
        Dictionary containing 'namespace' and 'name'

    Raises:
        CollectionValidationError: If tarball is invalid or MANIFEST.json not found
    """
    try:
        # Open tarball
        tarball_io = io.BytesIO(file_content)
        with tarfile.open(fileobj=tarball_io, mode="r:gz") as tar:
            # Look for MANIFEST.json
            manifest_member = None
            for member in tar.getmembers():
                if member.name == "MANIFEST.json":
                    manifest_member = member
                    break

            if not manifest_member:
                raise CollectionValidationError("MANIFEST.json not found in tarball")

            # Extract and parse MANIFEST.json
            manifest_file = tar.extractfile(manifest_member)
            if not manifest_file:
                raise CollectionValidationError("Failed to extract MANIFEST.json")

            manifest_data = json.load(manifest_file)

            # Get collection info
            if "collection_info" not in manifest_data:
                raise CollectionValidationError("MANIFEST.json missing 'collection_info' field")

            collection_info = manifest_data["collection_info"]

            if "namespace" not in collection_info:
                raise CollectionValidationError(
                    "MANIFEST.json missing 'collection_info.namespace' field"
                )

            if "name" not in collection_info:
                raise CollectionValidationError(
                    "MANIFEST.json missing 'collection_info.name' field"
                )

            return {
                "namespace": collection_info["namespace"],
                "name": collection_info["name"],
            }

    except tarfile.TarError as e:
        raise CollectionValidationError(f"Invalid tarball: {e}") from e
    except json.JSONDecodeError as e:
        raise CollectionValidationError(f"Invalid MANIFEST.json: {e}") from e


def extract_tarball_from_multipart(body: bytes, content_type: str) -> bytes:
    """Extract tarball from multipart/form-data request.

    Args:
        body: Request body bytes
        content_type: Content-Type header value

    Returns:
        Raw tarball bytes

    Raises:
        CollectionValidationError: If multipart parsing fails or file not found
    """
    try:
        # Parse multipart using email module
        # Add Content-Type header for email parser
        message_bytes = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        msg = email.message_from_bytes(message_bytes)

        # Find the file part
        if msg.is_multipart():
            for part in msg.walk():
                # Look for the file part (usually has filename or name="file")
                content_disposition = part.get("Content-Disposition", "")
                is_file = (
                    "filename=" in content_disposition
                    or part.get_content_type() == "application/octet-stream"
                )
                if is_file:
                    payload = part.get_payload(decode=True)
                    if payload and isinstance(payload, bytes):
                        return payload

        raise CollectionValidationError("No file found in multipart request")

    except Exception as e:
        raise CollectionValidationError(f"Failed to parse multipart request: {e}") from e
