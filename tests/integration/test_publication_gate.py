import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.models import SourceStatus
from westbusan.quality.checks import QualityReport, run_quality_suite
from westbusan.quality.publish import current_published_run, publish_if_valid
from westbusan.sources.datagokr import parse_data_page


def test_rejected_run_never_replaces_last_known_good_publication(tmp_path: Path) -> None:
    """Catches a failed run advancing the analytical publication pointer."""
    db = _db(tmp_path)
    valid_run, invalid_run = uuid4(), uuid4()
    valid = _valid_report(db, tmp_path, valid_run)
    invalid = run_quality_suite(db, invalid_run)

    first = publish_if_valid(db, valid_run, valid)
    rejected = publish_if_valid(db, invalid_run, invalid)

    assert first.published is True
    assert rejected.published is False
    assert current_published_run(db) == valid_run


def test_publication_is_idempotent_for_a_verified_valid_run(tmp_path: Path) -> None:
    """Catches repeated publication changing a valid run's pointer row."""
    db = _db(tmp_path)
    run_id = uuid4()
    report = _valid_report(db, tmp_path, run_id)

    first = publish_if_valid(db, run_id, report)
    second = publish_if_valid(db, run_id, report)

    assert first.published is True
    assert second.published is True
    assert current_published_run(db) == run_id
    assert db.query("select count(*) from publication_state") == [(1,)]


def _valid_report(db: Database, tmp_path: Path, run_id) -> QualityReport:
    body = json.dumps(
        {"data": [{"MNG_NO": "L1"}], "totalCount": 1, "pageNo": 1, "numOfRows": 1}
    ).encode()
    page = parse_data_page(body, "application/json")
    path = tmp_path / f"{run_id}.json"
    path.write_bytes(body)
    db.connection.execute(
        """
        insert into raw_artifact (
            artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
            content_hash, path, created_at, source_date
        ) values (?, ?, 'lodgings', ?, '{}', 'request', ?, ?, ?, ?)
        """,
        [
            uuid4(),
            run_id,
            date(2026, 8, 16),
            hashlib.sha256(body).hexdigest(),
            str(path),
            datetime(2026, 8, 16, tzinfo=UTC),
            date(2026, 8, 16),
        ],
    )
    db.connection.execute(
        """
        insert into staging_license_snapshot (
            source_id, source_record_id, observed_on, first_loaded_run_id, last_loaded_run_id,
            district, region_group, region_quality, room_count, room_count_quality,
            source_payload_json, record_hash
        ) values ('lodgings', 'L1', ?, ?, ?, '사하구', 'west', 'resolved', 1, 'reported', '{}', 'hash')
        """,
        [date(2026, 8, 16), run_id, run_id],
    )
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 16, tzinfo=UTC),
            "READY",
            {"required": True, "schema_fingerprint": page.schema_fingerprint},
            run_id,
        )
    )
    return run_quality_suite(db, run_id)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    return db
