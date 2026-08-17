from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pytest

from westbusan.config import RegionConfig
from westbusan.db import Database
from westbusan.models import RawArtifact
from westbusan.spatial import boundary as boundary_module
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.models import (
    BoundaryApprovalError,
    BoundaryContractError,
    BoundaryMetadata,
)
from westbusan.storage import RawStore

FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")


def _changed_fixture(tmp_path: Path, change: object) -> Path:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    change(document)
    path = tmp_path / "changed.geojson"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_inspection_reports_complete_deterministic_boundary_evidence() -> None:
    """Catches omitted official-universe, hash, count, CRS, or bounds evidence."""
    first = inspect_boundary(FIXTURE, RegionConfig.default())
    second = inspect_boundary(FIXTURE, RegionConfig.default())

    assert first == second
    assert first.content_hash == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert first.feature_count == 17
    assert first.district_count == 16
    assert first.dong_count == 17
    assert first.crs == "EPSG:4326"
    assert first.bounds == (128.9, 35.05, 128.916, 35.066)
    assert first.geometry_valid is True
    evidence = json.loads(first.evidence_json)
    assert evidence["district_membership_counts"] == {"east": 3, "other": 9, "west": 4}
    assert evidence["districts"] == sorted(
        RegionConfig.default().west
        + RegionConfig.default().east
        + RegionConfig.default().other
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda doc: doc["features"][0]["properties"].pop("district"),
            "district",
        ),
        (
            lambda doc: doc["features"][0]["properties"].update(
                {"district": "서울특별시"}
            ),
            "16 Busan districts",
        ),
        (
            lambda doc: doc["features"][0]["geometry"].update(
                {
                    "coordinates": [
                        [
                            [133.0, 35.0],
                            [133.1, 35.0],
                            [133.1, 35.1],
                            [133.0, 35.1],
                            [133.0, 35.0],
                        ]
                    ]
                }
            ),
            "South Korea",
        ),
        (
            lambda doc: doc["features"][0]["geometry"].update(
                {
                    "coordinates": [
                        [
                            [128.900, 35.050],
                            [128.904, 35.054],
                            [128.904, 35.050],
                            [128.900, 35.054],
                            [128.900, 35.050],
                        ]
                    ]
                }
            ),
            "invalid geometry",
        ),
        (lambda doc: doc.pop("crs"), "CRS"),
        (
            lambda doc: doc["crs"]["properties"].update({"name": "EPSG:5174"}),
            "EPSG:4326",
        ),
        (
            lambda doc: doc["features"][1]["properties"].update(
                {"dong_code": "26440101", "dong_name": "다른동"}
            ),
            "conflicting dong",
        ),
    ],
)
def test_inspection_rejects_invalid_official_boundary_contract(
    tmp_path: Path, mutation: object, message: str
) -> None:
    """Catches accepting a malformed or non-official boundary as reviewed input."""
    path = _changed_fixture(tmp_path, mutation)

    with pytest.raises(BoundaryContractError, match=message):
        inspect_boundary(path, RegionConfig.default())


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_inspection_rejects_nonstandard_nonfinite_json_constants(
    tmp_path: Path, constant: str
) -> None:
    """Catches Python's permissive JSON parser accepting NaN or Infinity tokens."""
    body = FIXTURE.read_text(encoding="utf-8")
    path = tmp_path / "nonfinite.geojson"
    path.write_text(
        body.replace('"features": [', f'"non_standard": {constant}, "features": ['),
        encoding="utf-8",
    )

    with pytest.raises(BoundaryContractError, match="NaN or Infinity"):
        inspect_boundary(path, RegionConfig.default())


def _metadata(version: str = "2026-08-reviewed") -> BoundaryMetadata:
    return BoundaryMetadata(
        source_organization="부산광역시",
        source_url="https://data.busan.go.kr/boundary",
        source_date=date(2026, 8, 1),
        source_version=version,
    )


def _database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "spatial.duckdb", Path("sql"))
    db.migrate()
    return db


def test_hash_mismatch_records_rejection_without_approving_or_copying(
    tmp_path: Path,
) -> None:
    """Catches hash bypasses and rejection paths that leave no audit evidence."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)

    with pytest.raises(BoundaryApprovalError, match="hash"):
        approve_boundary(
            db,
            RawStore(tmp_path / "raw"),
            FIXTURE,
            inspection,
            "0" * 64,
            "reviewer@example.org",
            "Reviewed against the official district release.",
            _metadata(),
        )

    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]
    assert db.query("select count(*) from raw_artifact") == [(0,)]
    events = db.query(
        """select observed_content_hash, boundary_version_id, action, actor,
                  rationale, evidence_json
           from spatial_boundary_approval_event"""
    )
    assert len(events) == 1
    assert events[0][:5] == (
        inspection.content_hash,
        None,
        "rejected",
        "reviewer@example.org",
        "Reviewed against the official district release.",
    )
    assert json.loads(events[0][5]) == {"reason": "supplied_hash_mismatch"}
    assert FIXTURE.read_text(encoding="utf-8") not in events[0][5]


class _InboxMutatingRawStore(RawStore):
    def __init__(self, data_dir: Path, inbox: Path) -> None:
        super().__init__(data_dir)
        self.inbox = inbox

    def write(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        artifact = super().write(*args, **kwargs)
        self.inbox.write_text("mutable inbox was replaced", encoding="utf-8")
        return artifact


class _FailingRawStore(RawStore):
    def write(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        raise OSError("DO_NOT_AUDIT_FILE_CONTENTS")


class _ArtifactRecordFailingDatabase(Database):
    def record_artifact(self, artifact: RawArtifact) -> None:
        raise duckdb.ConstraintException("DO_NOT_AUDIT_DATABASE_DETAIL")


def test_raw_store_failure_appends_redacted_rejection_event(tmp_path: Path) -> None:
    """Catches immutable-copy failures disappearing after the inbox hash is observed."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)

    with pytest.raises(BoundaryApprovalError, match="storage failed"):
        approve_boundary(
            db,
            _FailingRawStore(tmp_path / "data"),
            FIXTURE,
            inspection,
            inspection.content_hash,
            "reviewer@example.org",
            "Reviewed against the official district release.",
            _metadata(),
        )

    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]
    assert db.query("select count(*) from raw_artifact") == [(0,)]
    event = db.query(
        """select observed_content_hash, boundary_version_id, action,
                  source_metadata_json, evidence_json
           from spatial_boundary_approval_event"""
    )[0]
    assert event[:3] == (inspection.content_hash, None, "rejected")
    assert json.loads(event[3]) == {
        "source_date": "2026-08-01",
        "source_organization": "부산광역시",
        "source_url": "https://data.busan.go.kr/boundary",
        "source_version": "2026-08-reviewed",
    }
    assert json.loads(event[4]) == {
        "failure_type": "OSError",
        "reason": "raw_store_write_failed",
    }
    assert "DO_NOT_AUDIT_FILE_CONTENTS" not in event[4]


def test_artifact_record_failure_appends_redacted_rejection_event(
    tmp_path: Path,
) -> None:
    """Catches artifact-record failures losing the mandatory rejection audit."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _ArtifactRecordFailingDatabase(tmp_path / "spatial.duckdb", Path("sql"))
    db.migrate()

    with pytest.raises(BoundaryApprovalError, match="recording failed"):
        approve_boundary(
            db,
            RawStore(tmp_path / "data"),
            FIXTURE,
            inspection,
            inspection.content_hash,
            "reviewer@example.org",
            "Reviewed against the official district release.",
            _metadata(),
        )

    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]
    assert db.query("select count(*) from raw_artifact") == [(0,)]
    event = db.query(
        """select observed_content_hash, boundary_version_id, action,
                  source_metadata_json, evidence_json
           from spatial_boundary_approval_event"""
    )[0]
    assert event[:3] == (inspection.content_hash, None, "rejected")
    assert json.loads(event[3])["source_url"] == "https://data.busan.go.kr/boundary"
    assert json.loads(event[4]) == {
        "failure_type": "ConstraintException",
        "reason": "raw_artifact_record_failed",
    }
    assert "DO_NOT_AUDIT_DATABASE_DETAIL" not in event[4]
    stored_files = list((tmp_path / "data" / "raw").rglob("*.geojson"))
    assert len(stored_files) == 1
    assert hashlib.sha256(stored_files[0].read_bytes()).hexdigest() == (
        inspection.content_hash
    )


def test_approval_parses_immutable_copy_after_inbox_is_changed(tmp_path: Path) -> None:
    """Catches reparsing the mutable reviewed inbox after immutable storage."""
    inbox = tmp_path / "inbox.geojson"
    inbox.write_bytes(FIXTURE.read_bytes())
    inspection = inspect_boundary(inbox, RegionConfig.default())
    db = _database(tmp_path)
    store = _InboxMutatingRawStore(tmp_path / "data", inbox)

    boundary_id = approve_boundary(
        db,
        store,
        inbox,
        inspection,
        inspection.content_hash,
        "reviewer@example.org",
        "Reviewed against the official district release.",
        _metadata(),
    )

    row = db.query(
        """select boundary.raw_artifact_id, boundary.content_hash, artifact.path,
                  artifact.content_hash
           from spatial_boundary_version as boundary
           join raw_artifact as artifact
             on artifact.artifact_id = boundary.raw_artifact_id
           where boundary.boundary_version_id = ?""",
        [boundary_id],
    )[0]
    assert row[1] == row[3] == inspection.content_hash
    assert hashlib.sha256(Path(row[2]).read_bytes()).hexdigest() == inspection.content_hash
    assert inbox.read_text(encoding="utf-8") == "mutable inbox was replaced"
    assert db.query(
        "select action, boundary_version_id from spatial_boundary_approval_event"
    ) == [("approved", boundary_id)]


def test_approval_rehash_rejects_artifact_changed_after_immutable_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches artifact mutation between immutable inspection and atomic approval."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)
    original_inspect = boundary_module.inspect_boundary
    inspected_artifact: list[Path] = []

    def inspect_then_mutate(path: Path, regions: RegionConfig):
        result = original_inspect(path, regions)
        artifact_path = Path(path)
        inspected_artifact.append(artifact_path)
        artifact_path.write_bytes(b"tampered after immutable inspection")
        return result

    monkeypatch.setattr(boundary_module, "inspect_boundary", inspect_then_mutate)

    with pytest.raises(BoundaryApprovalError, match="changed after inspection"):
        approve_boundary(
            db,
            RawStore(tmp_path / "data"),
            FIXTURE,
            inspection,
            inspection.content_hash,
            "reviewer@example.org",
            "Reviewed against the official district release.",
            _metadata(),
        )

    assert len(inspected_artifact) == 1
    assert hashlib.sha256(inspected_artifact[0].read_bytes()).hexdigest() != (
        inspection.content_hash
    )
    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]
    assert db.query(
        """select observed_content_hash, boundary_version_id, action, evidence_json
           from spatial_boundary_approval_event"""
    ) == [
        (
            inspection.content_hash,
            None,
            "rejected",
            '{"reason":"immutable_artifact_hash_changed"}',
        )
    ]


def test_repeated_matching_approval_is_idempotent_and_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    """Catches duplicate versions or silent metadata rewrites for the same bytes."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)
    store = RawStore(tmp_path / "data")
    arguments = (
        db,
        store,
        FIXTURE,
        inspection,
        inspection.content_hash,
        "reviewer@example.org",
        "Reviewed against the official district release.",
    )

    first = approve_boundary(*arguments, _metadata())
    second = approve_boundary(*arguments, _metadata())

    assert second == first
    assert db.query("select count(*) from spatial_boundary_version") == [(1,)]
    assert db.query(
        "select action, boundary_version_id from spatial_boundary_approval_event order by event_at"
    ) == [("approved", first), ("approved", first)]

    with pytest.raises(BoundaryApprovalError, match="conflicting metadata"):
        approve_boundary(*arguments, _metadata("different-version"))

    assert db.query("select count(*) from spatial_boundary_version") == [(1,)]
    assert db.query(
        "select action, boundary_version_id from spatial_boundary_approval_event order by event_at"
    )[-1] == ("rejected", None)


def test_new_boundary_and_approval_event_commit_atomically(tmp_path: Path) -> None:
    """Catches an approved version surviving when its mandatory audit insert fails."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)
    db.connection.execute(
        """insert into spatial_boundary_approval_event (
               event_id, observed_content_hash, boundary_version_id, action,
               actor, rationale, source_metadata_json, evidence_json
           ) values (?, ?, null, 'rejected', 'prior', 'prior', '{}', '{}')""",
        [uuid4(), inspection.content_hash],
    )
    db.connection.execute(
        """create unique index one_event_per_hash_for_failure_test
           on spatial_boundary_approval_event (observed_content_hash)"""
    )

    with pytest.raises(duckdb.ConstraintException, match="Duplicate key"):
        approve_boundary(
            db,
            RawStore(tmp_path / "data"),
            FIXTURE,
            inspection,
            inspection.content_hash,
            "reviewer@example.org",
            "Reviewed against the official district release.",
            _metadata(),
        )

    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]


@pytest.mark.parametrize(
    ("approver", "rationale", "metadata", "message"),
    [
        ("", "reason", _metadata(), "approver"),
        ("reviewer", " ", _metadata(), "rationale"),
        (
            "reviewer",
            "reason",
            BoundaryMetadata("", "https://data.busan.go.kr", date(2026, 8, 1), "v1"),
            "organization",
        ),
        (
            "reviewer",
            "reason",
            BoundaryMetadata("Busan", "http://data.busan.go.kr", date(2026, 8, 1), "v1"),
            "HTTPS",
        ),
        (
            "reviewer",
            "reason",
            BoundaryMetadata("Busan", "https://data.busan.go.kr", date(2026, 8, 1), ""),
            "version",
        ),
    ],
)
def test_approval_rejects_incomplete_review_provenance(
    tmp_path: Path,
    approver: str,
    rationale: str,
    metadata: BoundaryMetadata,
    message: str,
) -> None:
    """Catches approval without attributable reviewer and official provenance."""
    inspection = inspect_boundary(FIXTURE, RegionConfig.default())
    db = _database(tmp_path)

    with pytest.raises(BoundaryApprovalError, match=message):
        approve_boundary(
            db,
            RawStore(tmp_path / "data"),
            FIXTURE,
            inspection,
            inspection.content_hash,
            approver,
            rationale,
            metadata,
        )

    assert db.query("select count(*) from spatial_boundary_version") == [(0,)]
    assert db.query("select action from spatial_boundary_approval_event") == [
        ("rejected",)
    ]
