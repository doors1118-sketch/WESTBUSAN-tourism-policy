"""Assemble an immutable tourism dashboard release with every map bundle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_RELEASE_MANIFEST = "release-manifest.json"
_RELEASE_SCHEMA_VERSION = 1
_ENTRYPOINTS = (
    "index.html",
    "map/index.html",
    "vacant-map/index.html",
    "river-map/index.html",
)
_DASHBOARD_FILES = (
    "index.html",
    "app.css",
    "app.js",
    "data.json",
    "river-map/index.html",
    "river-map/map.css",
    "river-map/map.js",
)
_MAP_REFERENCES = (
    'data-map-src="map/index.html',
    'data-vacant-map-src="vacant-map/index.html',
    'data-river-map-src="river-map/index.html',
)


class DashboardReleaseError(RuntimeError):
    """A dashboard release input or assembled output failed validation."""


@dataclass(frozen=True, slots=True)
class DashboardReleaseBundle:
    """A complete immutable dashboard release ready for atomic activation."""

    directory: Path
    access_snapshot_id: str | None
    file_count: int

    @property
    def manifest(self) -> Path:
        return self.directory / _RELEASE_MANIFEST


def build_dashboard_release(
    *,
    dashboard_assets: Path,
    investment_map: Path,
    vacant_map: Path,
    output_directory: Path,
) -> DashboardReleaseBundle:
    """Copy dashboard assets and two generated maps into one verified release."""
    dashboard_assets = Path(dashboard_assets)
    investment_map = Path(investment_map)
    vacant_map = Path(vacant_map)
    output_directory = Path(output_directory)

    if not _dashboard_source_is_valid(dashboard_assets):
        raise DashboardReleaseError("dashboard_assets_invalid")
    investment_manifest = _validated_map_manifest(
        investment_map, length_field="byte_count"
    )
    if investment_manifest is None:
        raise DashboardReleaseError("investment_map_invalid")
    vacant_manifest = _validated_map_manifest(vacant_map, length_field="bytes")
    if vacant_manifest is None:
        raise DashboardReleaseError("vacant_map_invalid")

    access_snapshot_id = _matching_access_snapshot(investment_manifest, vacant_manifest)
    if output_directory.exists():
        raise DashboardReleaseError("output_directory_exists")
    output_directory.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    try:
        shutil.copytree(dashboard_assets, temporary, dirs_exist_ok=True)
        shutil.copytree(investment_map, temporary / "map")
        shutil.copytree(vacant_map, temporary / "vacant-map")
        if not _assembled_structure_is_valid(temporary):
            raise DashboardReleaseError("assembled_release_incomplete")
        _write_release_manifest(temporary, access_snapshot_id)
        _set_public_permissions(temporary)
        if not validate_dashboard_release(temporary):
            raise DashboardReleaseError("assembled_release_invalid")
        if output_directory.exists():
            raise DashboardReleaseError("output_directory_exists")
        os.rename(temporary, output_directory)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise

    manifest = _read_json(output_directory / _RELEASE_MANIFEST)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    assert isinstance(files, dict)
    return DashboardReleaseBundle(
        directory=output_directory,
        access_snapshot_id=access_snapshot_id,
        file_count=len(files),
    )


def validate_dashboard_release(directory: Path) -> bool:
    """Verify release membership, hashes, entrypoints, and map lineage."""
    directory = Path(directory)
    try:
        manifest = _read_json(directory / _RELEASE_MANIFEST)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != _RELEASE_SCHEMA_VERSION
            or manifest.get("entrypoints") != list(_ENTRYPOINTS)
            or not _assembled_structure_is_valid(directory)
        ):
            return False

        investment_manifest = _validated_map_manifest(
            directory / "map", length_field="byte_count"
        )
        vacant_manifest = _validated_map_manifest(
            directory / "vacant-map", length_field="bytes"
        )
        if investment_manifest is None or vacant_manifest is None:
            return False
        access_snapshot_id = _matching_access_snapshot(
            investment_manifest, vacant_manifest
        )
        if manifest.get("access_snapshot_id") != access_snapshot_id:
            return False

        expected = manifest.get("files")
        if not isinstance(expected, dict):
            return False
        actual = {
            path.relative_to(directory).as_posix(): path
            for path in directory.rglob("*")
            if path.is_file() and path.name != _RELEASE_MANIFEST
        }
        if set(expected) != set(actual):
            return False
        for name, evidence in expected.items():
            if not isinstance(evidence, dict):
                return False
            path = actual[name]
            body = path.read_bytes()
            if (
                evidence.get("bytes") != len(body)
                or evidence.get("sha256") != hashlib.sha256(body).hexdigest()
            ):
                return False
        return True
    except (DashboardReleaseError, OSError, TypeError, ValueError):
        return False


def _dashboard_source_is_valid(directory: Path) -> bool:
    if not directory.is_dir() or _tree_has_symlink(directory):
        return False
    if (directory / "map").exists() or (directory / "vacant-map").exists():
        return False
    return _dashboard_files_and_references_are_valid(directory)


def _assembled_structure_is_valid(directory: Path) -> bool:
    if not directory.is_dir() or _tree_has_symlink(directory):
        return False
    if not _dashboard_files_and_references_are_valid(directory):
        return False
    return all((directory / name).is_file() for name in _ENTRYPOINTS)


def _dashboard_files_and_references_are_valid(directory: Path) -> bool:
    if not all((directory / name).is_file() for name in _DASHBOARD_FILES):
        return False
    try:
        html = (directory / "index.html").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return all(reference in html for reference in _MAP_REFERENCES)


def _validated_map_manifest(
    directory: Path, *, length_field: str
) -> dict[str, Any] | None:
    directory = Path(directory)
    if not directory.is_dir() or _tree_has_symlink(directory):
        return None
    manifest = _read_json(directory / "manifest.json")
    if not isinstance(manifest, dict):
        return None
    entries = manifest.get("files")
    if not isinstance(entries, dict) or "index.html" not in entries:
        return None
    expected_files = set(entries) | {"manifest.json"}
    actual_files = {path.name for path in directory.iterdir() if path.is_file()}
    if expected_files != actual_files:
        return None
    if any(path.is_dir() for path in directory.iterdir()):
        return None
    for name, evidence in entries.items():
        if not _plain_file_name(name) or not isinstance(evidence, dict):
            return None
        try:
            body = (directory / name).read_bytes()
        except OSError:
            return None
        if (
            evidence.get(length_field) != len(body)
            or evidence.get("sha256") != hashlib.sha256(body).hexdigest()
        ):
            return None
    return manifest


def _matching_access_snapshot(
    investment_manifest: dict[str, Any], vacant_manifest: dict[str, Any]
) -> str | None:
    investment_snapshot = investment_manifest.get("access_snapshot_id")
    vacant_snapshot = vacant_manifest.get("access_snapshot_id")
    if investment_snapshot != vacant_snapshot:
        raise DashboardReleaseError("access_snapshot_mismatch")
    if investment_snapshot is not None and not isinstance(investment_snapshot, str):
        raise DashboardReleaseError("access_snapshot_invalid")
    return investment_snapshot


def _write_release_manifest(directory: Path, access_snapshot_id: str | None) -> None:
    files: dict[str, dict[str, object]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DashboardReleaseError("release_symlink_forbidden")
        if not path.is_file():
            continue
        name = path.relative_to(directory).as_posix()
        body = path.read_bytes()
        files[name] = {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    manifest = {
        "schema_version": _RELEASE_SCHEMA_VERSION,
        "access_snapshot_id": access_snapshot_id,
        "entrypoints": list(_ENTRYPOINTS),
        "files": files,
    }
    (directory / _RELEASE_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _plain_file_name(name: object) -> bool:
    if not isinstance(name, str) or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and len(path.parts) == 1 and path.name == name


def _tree_has_symlink(directory: Path) -> bool:
    return directory.is_symlink() or any(
        path.is_symlink() for path in directory.rglob("*")
    )


def _set_public_permissions(directory: Path) -> None:
    directory.chmod(0o755)
    for path in directory.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


__all__ = [
    "DashboardReleaseBundle",
    "DashboardReleaseError",
    "build_dashboard_release",
    "validate_dashboard_release",
]
