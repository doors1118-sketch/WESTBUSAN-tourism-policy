from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from westbusan.tourism_dashboard.release import (
    DashboardReleaseError,
    build_dashboard_release,
    validate_dashboard_release,
)

ROOT = Path(__file__).parents[2]
REAL_DASHBOARD_ASSETS = ROOT / "src" / "westbusan" / "tourism_dashboard" / "assets"


def _write_bundle(
    directory: Path,
    *,
    length_field: str,
    access_snapshot_id: str = "snapshot-current",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = b"<!doctype html><title>map</title>"
    (directory / "index.html").write_bytes(body)
    manifest = {
        "access_snapshot_id": access_snapshot_id,
        "files": {
            "index.html": {
                length_field: len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _dashboard_assets(directory: Path) -> Path:
    directory.mkdir()
    (directory / "index.html").write_text(
        """<!doctype html>
        <iframe data-map-src="map/index.html"></iframe>
        <iframe data-vacant-map-src="vacant-map/index.html"></iframe>
        <iframe data-river-map-src="river-map/index.html"></iframe>
        """,
        encoding="utf-8",
    )
    for name in ("app.css", "app.js", "data.json"):
        (directory / name).write_text("{}", encoding="utf-8")
    river = directory / "river-map"
    river.mkdir()
    for name in ("index.html", "map.css", "map.js"):
        (river / name).write_text(name, encoding="utf-8")
    return directory


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    assets = _dashboard_assets(tmp_path / "assets")
    investment = tmp_path / "investment"
    vacant = tmp_path / "vacant"
    _write_bundle(investment, length_field="byte_count")
    _write_bundle(vacant, length_field="bytes")
    return assets, investment, vacant


def test_build_dashboard_release_requires_and_preserves_all_three_maps(
    tmp_path: Path,
) -> None:
    assets, investment, vacant = _inputs(tmp_path)
    output = tmp_path / "release"

    release = build_dashboard_release(
        dashboard_assets=assets,
        investment_map=investment,
        vacant_map=vacant,
        output_directory=output,
    )

    assert release.directory == output
    assert release.access_snapshot_id == "snapshot-current"
    assert (output / "map/index.html").is_file()
    assert (output / "vacant-map/index.html").is_file()
    assert (output / "river-map/index.html").is_file()
    assert (output / "release-manifest.json").is_file()
    assert validate_dashboard_release(output) is True


def test_repository_dashboard_assets_build_with_verified_map_bundles(
    tmp_path: Path,
) -> None:
    investment = tmp_path / "investment"
    vacant = tmp_path / "vacant"
    _write_bundle(investment, length_field="byte_count")
    _write_bundle(vacant, length_field="bytes")

    release = build_dashboard_release(
        dashboard_assets=REAL_DASHBOARD_ASSETS,
        investment_map=investment,
        vacant_map=vacant,
        output_directory=tmp_path / "release",
    )

    assert validate_dashboard_release(release.directory) is True


def test_release_command_builds_and_validates_complete_release(tmp_path: Path) -> None:
    investment = tmp_path / "investment"
    vacant = tmp_path / "vacant"
    output = tmp_path / "release"
    _write_bundle(investment, length_field="byte_count")
    _write_bundle(vacant, length_field="bytes")
    script = ROOT / "scripts" / "build_tourism_dashboard_release.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT / "src"), environment.get("PYTHONPATH")))
    )

    built = subprocess.run(
        [
            sys.executable,
            str(script),
            "build",
            "--investment-map",
            str(investment),
            "--vacant-map",
            str(vacant),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    checked = subprocess.run(
        [sys.executable, str(script), "validate", "--release", str(output)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert built.returncode == 0, built.stderr
    assert json.loads(built.stdout)["status"] == "COMPLETED"
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "VALID"


def test_build_dashboard_release_fails_closed_when_a_map_is_missing(
    tmp_path: Path,
) -> None:
    assets, investment, vacant = _inputs(tmp_path)
    (investment / "index.html").unlink()
    output = tmp_path / "release"

    with pytest.raises(DashboardReleaseError, match="investment_map_invalid"):
        build_dashboard_release(
            dashboard_assets=assets,
            investment_map=investment,
            vacant_map=vacant,
            output_directory=output,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".release.*"))


def test_build_dashboard_release_rejects_tampered_map_bundle(
    tmp_path: Path,
) -> None:
    assets, investment, vacant = _inputs(tmp_path)
    (vacant / "index.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(DashboardReleaseError, match="vacant_map_invalid"):
        build_dashboard_release(
            dashboard_assets=assets,
            investment_map=investment,
            vacant_map=vacant,
            output_directory=tmp_path / "release",
        )


def test_build_dashboard_release_rejects_mismatched_access_snapshots(
    tmp_path: Path,
) -> None:
    assets, investment, vacant = _inputs(tmp_path)
    _write_bundle(
        vacant,
        length_field="bytes",
        access_snapshot_id="snapshot-other",
    )

    with pytest.raises(DashboardReleaseError, match="access_snapshot_mismatch"):
        build_dashboard_release(
            dashboard_assets=assets,
            investment_map=investment,
            vacant_map=vacant,
            output_directory=tmp_path / "release",
        )


def test_release_validation_detects_a_map_removed_after_build(tmp_path: Path) -> None:
    assets, investment, vacant = _inputs(tmp_path)
    output = tmp_path / "release"
    build_dashboard_release(
        dashboard_assets=assets,
        investment_map=investment,
        vacant_map=vacant,
        output_directory=output,
    )

    (output / "vacant-map/index.html").unlink()

    assert validate_dashboard_release(output) is False
