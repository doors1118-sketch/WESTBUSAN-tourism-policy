from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook

from westbusan.db import Database
from westbusan.storage import RawStore
from westbusan.vacant_house.fencing import (
    VacantHouseFenceError,
    VacantHouseLeaseUnavailable,
)
from westbusan.vacant_house.importer import (
    VacantHouseImportError,
    import_staged_bundle,
    prepare_import,
    release_import,
)
from westbusan.vacant_house.models import StagedVacantBundleError
from westbusan.vacant_house.stage import stage_archive, validate_staged_bundle

SNAPSHOT_DATE = date(2025, 2, 28)
DISTRICT_CODES = (
    "26110",
    "26140",
    "26170",
    "26200",
    "26230",
    "26260",
    "26290",
    "26320",
    "26350",
    "26380",
    "26410",
    "26440",
    "26470",
    "26500",
    "26530",
    "26710",
)
HEADERS = (
    "시군구코드",
    "읍면동코드",
    "시군구",
    "읍면동",
    "토지구분",
    "본번",
    "부번",
    "도로명주소",
    "건축연도",
    "무허가여부",
    "철거필요여부",
    "빈집등급",
    "도로명코드",
    "건물본번",
    "건물부번",
    "건물명",
    "동명",
    "호명",
    "지번주소",
    "주택유형",
    "건물면적",
    "대지면적",
    "정비사업여부",
)


def _database(path: Path) -> Database:
    db = Database(path, Path("sql"))
    db.migrate()
    return db


def _row(
    district_code: str,
    *,
    construction_year: int = 1999,
    unlicensed: object = 0,
) -> list[object]:
    suffix = district_code[-3:]
    return [
        district_code,
        "10100",
        f"구-{suffix}",
        "동-101",
        "1",
        suffix,
        0,
        f"비공개-도로-{suffix}",
        construction_year,
        unlicensed,
        0,
        "1등급",
        f"{district_code}4202001",
        suffix,
        0,
        f"건물-{suffix}",
        "101동",
        "202호",
        f"비공개-지번-{suffix}",
        "단독주택",
        12.5,
        34.5,
        "검토중",
    ]


def _xlsx(rows: list[list[object]]) -> bytes:
    return _xlsx_sheets([rows])


def _xlsx_sheets(sheets: list[list[list[object]]]) -> bytes:
    workbook = Workbook()
    for index, rows in enumerate(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = f"비공개 원본 {index + 1}"
        if not rows:
            continue
        sheet.append(list(HEADERS))
        for row in rows:
            sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _bundle(
    tmp_path: Path,
    *,
    districts: tuple[str, ...] = DISTRICT_CODES,
    exact_duplicate: bool = False,
    ambiguous_duplicate: bool = False,
    invalid_identity: bool = False,
    extra_empty_sheet: bool = False,
    combine_last_district: bool = False,
    merge_first_district_into_second: bool = False,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_path / "source.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        workbook_districts = districts[:-1] if combine_last_district else districts
        for index, district_code in enumerate(workbook_districts):
            rows = [_row(district_code)]
            if index == 0 and exact_duplicate:
                rows.append(_row(district_code))
            if index == 0 and ambiguous_duplicate:
                rows.append(_row(district_code, construction_year=2001))
            if index == 0 and invalid_identity:
                invalid = _row(district_code)
                invalid[5] = None
                invalid[12] = None
                invalid[13] = None
                rows.append(invalid)
            sheets = [rows]
            if merge_first_district_into_second and index == 0:
                sheets = [[]]
            if merge_first_district_into_second and index == 1:
                sheets.append([_row(districts[0])])
            if index == 0 and combine_last_district:
                sheets.append([_row(districts[-1])])
            if index == 0 and extra_empty_sheet:
                sheets.append([])
            archive.writestr(
                f"district-{index:02d}.xlsx",
                _xlsx_sheets(sheets),
            )
    return stage_archive(archive_path, tmp_path / "staged", SNAPSHOT_DATE)


def _seed_prior_publication(db: Database) -> UUID:
    prior_run_id = uuid4()
    manifest_id = uuid4()
    now = datetime(2025, 1, 31, tzinfo=UTC)
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, fence_epoch,
               source_row_count, accepted_record_count, exception_count,
               started_at, completed_at
           ) values (?, ?, ?, ?, ?, 'COMPLETED', 1, 0, 0, 0, ?, ?)""",
        [prior_run_id, now.date(), "a" * 64, "b" * 64, "prior-v1", now, now],
    )
    db.connection.execute(
        """insert into vacant_house_completion_manifest (
               manifest_id, vacant_run_id, table_name, row_count,
               row_digest_sha256, schema_version, manifest_json, created_at
           ) values (?, ?, 'vacant_house_revision', 0, ?, 'prior-v1', '{}', ?)""",
        [manifest_id, prior_run_id, "c" * 64, now],
    )
    db.connection.execute(
        """insert into vacant_house_publication_current (
               singleton_key, pointer_id, vacant_run_id, published_at, publisher,
               publication_event_id, manifest_id
           ) values (1, ?, ?, ?, 'prior-actor', ?, ?)""",
        [uuid4(), prior_run_id, now, uuid4(), manifest_id],
    )
    return prior_run_id


def _with_fatal_source_exception(bundle, code: str):
    exception_path = bundle.path / "exceptions.parquet"
    table = pq.read_table(exception_path)
    rows = table.to_pylist()
    assert len(rows) == 1
    rows[0]["safe_code"] = code
    rows[0]["safe_field"] = "workbook"
    rows[0]["safe_message"] = "source workbook failed validation"
    rows[0]["evidence_json"] = json.dumps(
        {"code": code, "field": "workbook"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), exception_path)
    raw = exception_path.read_bytes()
    manifest_path = bundle.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["exceptions.parquet"].update(
        {"sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}
    )
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return validate_staged_bundle(bundle.path)


def test_imports_complete_bundle_with_explicit_exact_duplicate_and_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dropping source rows, duplicate evidence, or one district breaks custody."""
    bundle = _bundle(tmp_path, exact_duplicate=True, invalid_identity=True)
    db = _database(tmp_path / "inventory.duckdb")
    prior_run_id = _seed_prior_publication(db)

    token = prepare_import(db, bundle, actor="internal-operator")
    with pytest.raises(FrozenInstanceError):
        token.fence_epoch = -1  # type: ignore[misc]
    summary = import_staged_bundle(
        db, RawStore(tmp_path / "private-raw"), bundle, token
    )
    release_import(db, token)

    assert summary.vacant_run_id == token.vacant_run_id
    assert summary.source_row_count == 18
    assert summary.source_artifact_count == 16
    assert summary.revision_count == 17
    assert summary.current_count == 16
    assert summary.exact_duplicate_count == 2
    assert summary.ambiguous_duplicate_count == 0
    assert summary.exception_count == 1
    assert db.scalar("select count(*) from vacant_house_import_run") == 2
    assert (
        db.scalar(
            "select count(*) from vacant_house_source_artifact where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 16
    )
    assert (
        db.scalar(
            "select count(*) from vacant_house_revision where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 17
    )
    assert (
        db.scalar(
            "select count(*) from vacant_house_current where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 16
    )
    assert (
        db.scalar(
            """select count(*) from vacant_house_revision
           where vacant_run_id = ? and duplicate_group_id is not null""",
            [token.vacant_run_id],
        )
        == 2
    )
    selected_duplicate = db.query(
        """select current.selected_source_row_id, min(revision.source_row_id)
           from vacant_house_current as current
           join vacant_house_revision as revision
             on revision.vacant_run_id = current.vacant_run_id
            and revision.record_id = current.record_id
           where current.vacant_run_id = ?
             and revision.duplicate_group_id is not null
           group by current.selected_source_row_id""",
        [token.vacant_run_id],
    )
    assert selected_duplicate == [(selected_duplicate[0][1], selected_duplicate[0][1])]
    assert (
        db.scalar(
            "select count(*) from vacant_house_exception where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 1
    )
    assert (
        db.scalar(
            "select count(*) from raw_artifact where run_id = ?",
            [token.vacant_run_id],
        )
        == 1
    )
    assert db.query(
        """select status, source_row_count, accepted_record_count, exception_count
           from vacant_house_import_run where vacant_run_id = ?""",
        [token.vacant_run_id],
    ) == [("RUNNING", 18, 16, 1)]
    assert (
        db.scalar(
            "select vacant_run_id from vacant_house_publication_current where singleton_key = 1"
        )
        == prior_run_id
    )
    assert (
        db.scalar(
            "select count(*) from vacant_house_revision where vacant_run_id <> ?",
            [token.vacant_run_id],
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_import_preserves_blank_sheet_as_zero_row_source_artifact(
    tmp_path: Path,
) -> None:
    """Every readable workbook sheet remains independently hash-bound."""
    bundle = _bundle(tmp_path, extra_empty_sheet=True)
    db = _database(tmp_path / "empty-sheet-artifact.duckdb")

    token = prepare_import(db, bundle, actor="internal-operator")
    summary = import_staged_bundle(
        db,
        RawStore(tmp_path / "private-raw"),
        bundle,
        token,
    )

    assert bundle.workbook_count == 16
    assert bundle.sheet_count == 17
    assert summary.source_artifact_count == 17
    assert (
        db.scalar(
            """select count(*) from vacant_house_source_artifact
               where vacant_run_id = ? and source_row_count = 0""",
            [token.vacant_run_id],
        )
        == 1
    )


def test_complete_district_set_in_fifteen_workbooks_fails_delivery_contract(
    tmp_path: Path,
) -> None:
    """District codes alone cannot prove the expected workbook inventory arrived."""
    bundle = _bundle(tmp_path, combine_last_district=True)
    db = _database(tmp_path / "missing-workbook.duckdb")

    with pytest.raises(VacantHouseImportError) as caught:
        prepare_import(db, bundle, actor="internal-operator")

    assert bundle.workbook_count == 15
    assert set(bundle.district_codes) == set(DISTRICT_CODES)
    assert caught.value.code == "incomplete_workbook_coverage"
    assert db.scalar("select count(*) from vacant_house_revision") == 0


def test_each_expected_workbook_must_map_to_exactly_one_district(
    tmp_path: Path,
) -> None:
    """A blank workbook plus a two-district workbook is not a 16-part delivery."""
    bundle = _bundle(tmp_path, merge_first_district_into_second=True)
    db = _database(tmp_path / "misassigned-workbooks.duckdb")

    with pytest.raises(VacantHouseImportError) as caught:
        prepare_import(db, bundle, actor="internal-operator")

    assert bundle.workbook_count == 16
    assert set(bundle.district_codes) == set(DISTRICT_CODES)
    assert caught.value.code == "incomplete_workbook_coverage"
    assert db.scalar("select count(*) from vacant_house_revision") == 0


def test_ambiguous_canonical_duplicates_create_exception_without_current(
    tmp_path: Path,
) -> None:
    """Selecting a conflicting duplicate would silently choose disputed evidence."""
    bundle = _bundle(tmp_path, ambiguous_duplicate=True)
    db = _database(tmp_path / "ambiguous.duckdb")
    token = prepare_import(db, bundle, actor="internal-operator")

    summary = import_staged_bundle(
        db, RawStore(tmp_path / "private-raw"), bundle, token
    )

    assert summary.revision_count == 17
    assert summary.current_count == 15
    assert summary.exact_duplicate_count == 0
    assert summary.ambiguous_duplicate_count == 2
    assert summary.exception_count == 1
    assert (
        db.scalar(
            """select count(*) from vacant_house_revision
           where vacant_run_id = ? and review_status = 'duplicate_ambiguous'""",
            [token.vacant_run_id],
        )
        == 2
    )
    assert (
        db.scalar(
            """select count(*) from vacant_house_exception
           where vacant_run_id = ? and exception_code = 'duplicate_ambiguous'""",
            [token.vacant_run_id],
        )
        == 1
    )


def test_incomplete_coverage_records_failed_run_and_preserves_publication(
    tmp_path: Path,
) -> None:
    """A partial city snapshot must fail closed without changing last-known-good."""
    bundle = _bundle(tmp_path, districts=DISTRICT_CODES[:-1])
    db = _database(tmp_path / "incomplete.duckdb")
    prior_run_id = _seed_prior_publication(db)

    with pytest.raises(VacantHouseImportError) as caught:
        prepare_import(db, bundle, actor="internal-operator")

    assert caught.value.code == "incomplete_district_coverage"
    failed = db.query(
        """select status, source_row_count, accepted_record_count,
                  exception_count, failure_evidence_json
           from vacant_house_import_run where status = 'FAILED'"""
    )
    assert len(failed) == 1
    assert failed[0][:4] == ("FAILED", 15, 0, 0)
    assert json.loads(failed[0][4]) == {
        "district_count": 15,
        "failure_code": "incomplete_district_coverage",
        "workbook_count": 15,
    }
    assert (
        db.scalar(
            "select vacant_run_id from vacant_house_publication_current where singleton_key = 1"
        )
        == prior_run_id
    )
    assert db.scalar("select count(*) from vacant_house_revision") == 0
    assert (
        db.scalar("select run_id from pipeline_writer_lease where lease_key = 'writer'")
        is None
    )


@pytest.mark.parametrize("code", ["unreadable_workbook", "mixed_district_sheet"])
def test_fatal_source_exception_records_failed_run(tmp_path: Path, code: str) -> None:
    """A sealed fatal workbook exception must not become an accepted snapshot."""
    staged = _bundle(tmp_path, invalid_identity=True)
    bundle = _with_fatal_source_exception(staged, code)
    db = _database(tmp_path / "fatal-source.duckdb")

    with pytest.raises(VacantHouseImportError) as caught:
        prepare_import(db, bundle, actor="internal-operator")

    assert caught.value.code == "fatal_source_exception"
    assert (
        db.query(
            """select status, failure_evidence_json
           from vacant_house_import_run"""
        )[0][0]
        == "FAILED"
    )
    assert (
        json.loads(
            db.query("select failure_evidence_json from vacant_house_import_run")[0][0]
        )["failure_code"]
        == "fatal_source_exception"
    )
    assert db.scalar("select count(*) from vacant_house_revision") == 0


def test_tampered_bundle_is_rejected_before_database_lease(tmp_path: Path) -> None:
    """A stale in-memory descriptor must not bypass bundle revalidation."""
    bundle = _bundle(tmp_path)
    db = _database(tmp_path / "tampered.duckdb")
    with (bundle.path / "records.parquet").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(StagedVacantBundleError):
        prepare_import(db, bundle, actor="internal-operator")

    assert db.scalar("select count(*) from vacant_house_import_run") == 0
    assert db.scalar("select count(*) from pipeline_writer_lease") == 0


def test_active_core_writer_blocks_prepare_on_second_connection(tmp_path: Path) -> None:
    """Vacant and core writers must never own the shared lease concurrently."""
    bundle = _bundle(tmp_path)
    first = _database(tmp_path / "shared.duckdb")
    second = _database(tmp_path / "shared.duckdb")
    now = datetime.now(UTC)
    first.connection.execute(
        """insert into pipeline_writer_lease (
               lease_key, owner_token, run_id, fence_epoch, heartbeat_at,
               lease_expires_at, fence_touch
           ) values ('writer', ?, ?, 7, ?, ?, 0)""",
        [uuid4(), uuid4(), now, now + timedelta(minutes=5)],
    )

    with pytest.raises(VacantHouseLeaseUnavailable):
        prepare_import(second, bundle, actor="internal-operator")

    assert second.scalar("select count(*) from vacant_house_import_run") == 0


def test_expired_owner_token_cannot_insert_any_target_rows(tmp_path: Path) -> None:
    """A takeover must fence a stale token before its target transaction writes."""
    first_bundle = _bundle(tmp_path / "first")
    second_bundle = _bundle(tmp_path / "second", exact_duplicate=True)
    first = _database(tmp_path / "shared.duckdb")
    second = _database(tmp_path / "shared.duckdb")
    stale = prepare_import(first, first_bundle, actor="owner-a")
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    first.connection.execute(
        "update pipeline_writer_lease set lease_expires_at = ? where lease_key = 'writer'",
        [expired_at],
    )
    first.connection.execute(
        "update vacant_house_import_run set lease_expires_at = ? where vacant_run_id = ?",
        [expired_at, stale.vacant_run_id],
    )
    current = prepare_import(second, second_bundle, actor="owner-b")

    with pytest.raises(VacantHouseFenceError):
        import_staged_bundle(
            first,
            RawStore(tmp_path / "private-raw"),
            first_bundle,
            stale,
        )

    assert (
        second.scalar(
            "select count(*) from vacant_house_source_artifact where vacant_run_id = ?",
            [stale.vacant_run_id],
        )
        == 0
    )
    assert (
        second.scalar(
            "select count(*) from vacant_house_revision where vacant_run_id = ?",
            [stale.vacant_run_id],
        )
        == 0
    )
    assert (
        second.scalar(
            "select count(*) from vacant_house_exception where vacant_run_id = ?",
            [stale.vacant_run_id],
        )
        == 0
    )
    assert (
        second.scalar(
            "select count(*) from vacant_house_completion_manifest where vacant_run_id = ?",
            [stale.vacant_run_id],
        )
        == 0
    )
    assert (
        second.scalar(
            "select count(*) from vacant_house_publication_audit where vacant_run_id = ?",
            [stale.vacant_run_id],
        )
        == 0
    )
    assert current.fence_epoch > stale.fence_epoch


def test_expired_same_bundle_run_is_reclaimed_and_strictly_reused(
    tmp_path: Path,
) -> None:
    """A crashed prepublication run resumes only from exact persisted targets."""
    bundle = _bundle(tmp_path)
    db = _database(tmp_path / "resume.duckdb")
    stale = prepare_import(db, bundle, actor="owner-a")
    import_staged_bundle(db, RawStore(tmp_path / "private-raw"), bundle, stale)
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    db.connection.execute(
        "update pipeline_writer_lease set lease_expires_at = ? where lease_key = 'writer'",
        [expired_at],
    )
    db.connection.execute(
        """update vacant_house_import_run set lease_expires_at = ?
           where vacant_run_id = ?""",
        [expired_at, stale.vacant_run_id],
    )

    resumed = prepare_import(db, bundle, actor="owner-b")
    summary = import_staged_bundle(
        db,
        RawStore(tmp_path / "private-raw"),
        bundle,
        resumed,
    )

    assert resumed.vacant_run_id == stale.vacant_run_id
    assert resumed.owner_token != stale.owner_token
    assert resumed.fence_epoch > stale.fence_epoch
    assert summary.source_artifact_count == 16
    assert summary.revision_count == 16
    assert summary.current_count == 16
    assert (
        db.scalar(
            "select count(*) from raw_artifact where run_id = ?",
            [stale.vacant_run_id],
        )
        == 1
    )
    assert db.query(
        """select status, owner_token, fence_epoch, accepted_record_count,
                  failure_evidence_json
           from vacant_house_import_run where vacant_run_id = ?""",
        [stale.vacant_run_id],
    ) == [("RUNNING", resumed.owner_token, resumed.fence_epoch, 16, None)]


def test_resumed_same_bundle_rejects_any_changed_persisted_target(
    tmp_path: Path,
) -> None:
    """Aggregate equality cannot authorise reuse of changed private row content."""
    bundle = _bundle(tmp_path)
    db = _database(tmp_path / "invalid-resume.duckdb")
    stale = prepare_import(db, bundle, actor="owner-a")
    import_staged_bundle(db, RawStore(tmp_path / "private-raw"), bundle, stale)
    db.connection.execute(
        """update vacant_house_revision set exact_address = 'changed-private-value'
           where vacant_run_id = ? and source_row_id = (
               select min(source_row_id) from vacant_house_revision
               where vacant_run_id = ?
           )""",
        [stale.vacant_run_id, stale.vacant_run_id],
    )
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    db.connection.execute(
        "update pipeline_writer_lease set lease_expires_at = ? where lease_key = 'writer'",
        [expired_at],
    )
    db.connection.execute(
        """update vacant_house_import_run set lease_expires_at = ?
           where vacant_run_id = ?""",
        [expired_at, stale.vacant_run_id],
    )
    resumed = prepare_import(db, bundle, actor="owner-b")

    with pytest.raises(VacantHouseImportError) as caught:
        import_staged_bundle(
            db,
            RawStore(tmp_path / "private-raw"),
            bundle,
            resumed,
        )

    assert caught.value.code == "interrupted_import_state_invalid"
    assert db.scalar("select count(*) from vacant_house_publication_current") == 0


def test_release_requires_exact_current_token(tmp_path: Path) -> None:
    """A stale release must not clear the new owner's shared writer lease."""
    first_bundle = _bundle(tmp_path / "first")
    second_bundle = _bundle(tmp_path / "second", exact_duplicate=True)
    first = _database(tmp_path / "release.duckdb")
    second = _database(tmp_path / "release.duckdb")
    stale = prepare_import(first, first_bundle, actor="owner-a")
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    first.connection.execute(
        "update pipeline_writer_lease set lease_expires_at = ? where lease_key = 'writer'",
        [expired_at],
    )
    first.connection.execute(
        "update vacant_house_import_run set lease_expires_at = ? where vacant_run_id = ?",
        [expired_at, stale.vacant_run_id],
    )
    current = prepare_import(second, second_bundle, actor="owner-b")

    with pytest.raises(VacantHouseFenceError):
        release_import(first, stale)

    assert second.query(
        """select run_id, owner_token, fence_epoch from pipeline_writer_lease
           where lease_key = 'writer'"""
    ) == [(current.vacant_run_id, current.owner_token, current.fence_epoch)]
