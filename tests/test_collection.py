"""Tests for collection validation."""

import io
import json
import tarfile

import pytest

from galaxy_publisher.collection import CollectionValidationError, extract_manifest_from_tarball


def create_test_tarball(manifest_data: dict) -> bytes:
    """Create a test collection tarball with MANIFEST.json.

    Args:
        manifest_data: Data to include in MANIFEST.json

    Returns:
        Tarball bytes
    """
    tarball_io = io.BytesIO()

    with tarfile.open(fileobj=tarball_io, mode="w:gz") as tar:
        # Create MANIFEST.json at root
        manifest_json = json.dumps(manifest_data).encode()
        manifest_io = io.BytesIO(manifest_json)
        manifest_info = tarfile.TarInfo(name="MANIFEST.json")
        manifest_info.size = len(manifest_json)
        tar.addfile(manifest_info, manifest_io)

        # Create galaxy.yml (not required for this test but typical)
        galaxy_yml = b"namespace: myorg\nname: mycollection\nversion: 1.0.0\n"
        galaxy_io = io.BytesIO(galaxy_yml)
        galaxy_info = tarfile.TarInfo(name="galaxy.yml")
        galaxy_info.size = len(galaxy_yml)
        tar.addfile(galaxy_info, galaxy_io)

    return tarball_io.getvalue()


def test_extract_manifest_valid() -> None:
    """Test extracting valid MANIFEST.json from tarball."""
    manifest_data = {
        "collection_info": {
            "namespace": "myorg",
            "name": "mycollection",
            "version": "1.0.0",
            "authors": ["Test Author"],
        }
    }

    tarball_bytes = create_test_tarball(manifest_data)

    result = extract_manifest_from_tarball(tarball_bytes)

    assert result["namespace"] == "myorg"
    assert result["name"] == "mycollection"


def test_extract_manifest_missing_manifest() -> None:
    """Test that missing MANIFEST.json raises error."""
    tarball_io = io.BytesIO()

    with tarfile.open(fileobj=tarball_io, mode="w:gz") as tar:
        # Create only galaxy.yml
        galaxy_yml = b"namespace: myorg\nname: mycollection\nversion: 1.0.0\n"
        galaxy_io = io.BytesIO(galaxy_yml)
        galaxy_info = tarfile.TarInfo(name="galaxy.yml")
        galaxy_info.size = len(galaxy_yml)
        tar.addfile(galaxy_info, galaxy_io)

    tarball_bytes = tarball_io.getvalue()

    with pytest.raises(CollectionValidationError, match="MANIFEST.json not found"):
        extract_manifest_from_tarball(tarball_bytes)


def test_extract_manifest_missing_collection_info() -> None:
    """Test that missing collection_info field raises error."""
    manifest_data = {"other_field": "value"}

    tarball_bytes = create_test_tarball(manifest_data)

    with pytest.raises(CollectionValidationError, match="missing 'collection_info'"):
        extract_manifest_from_tarball(tarball_bytes)


def test_extract_manifest_missing_namespace() -> None:
    """Test that missing namespace field raises error."""
    manifest_data = {"collection_info": {"name": "mycollection", "version": "1.0.0"}}

    tarball_bytes = create_test_tarball(manifest_data)

    with pytest.raises(CollectionValidationError, match="missing 'collection_info.namespace'"):
        extract_manifest_from_tarball(tarball_bytes)


def test_extract_manifest_missing_name() -> None:
    """Test that missing name field raises error."""
    manifest_data = {"collection_info": {"namespace": "myorg", "version": "1.0.0"}}

    tarball_bytes = create_test_tarball(manifest_data)

    with pytest.raises(CollectionValidationError, match="missing 'collection_info.name'"):
        extract_manifest_from_tarball(tarball_bytes)


def test_extract_manifest_invalid_tarball() -> None:
    """Test that invalid tarball raises error."""
    invalid_tarball = b"not a tarball"

    with pytest.raises(CollectionValidationError, match="Invalid tarball"):
        extract_manifest_from_tarball(invalid_tarball)


def test_extract_manifest_invalid_json() -> None:
    """Test that invalid JSON in MANIFEST.json raises error."""
    tarball_io = io.BytesIO()

    with tarfile.open(fileobj=tarball_io, mode="w:gz") as tar:
        # Create invalid JSON
        invalid_json = b"{ invalid json }"
        manifest_io = io.BytesIO(invalid_json)
        manifest_info = tarfile.TarInfo(name="MANIFEST.json")
        manifest_info.size = len(invalid_json)
        tar.addfile(manifest_info, manifest_io)

    tarball_bytes = tarball_io.getvalue()

    with pytest.raises(CollectionValidationError, match="Invalid MANIFEST.json"):
        extract_manifest_from_tarball(tarball_bytes)
