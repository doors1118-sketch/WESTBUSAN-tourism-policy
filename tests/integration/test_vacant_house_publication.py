from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from westbusan.db import Database
from westbusan.storage import RawStore
from westbusan.vacant_house.fencing import VacantHouseFenceError
from westbusan.vacant_house.importer import import_staged_bundle, prepare_import
from westbusan.vacant_house.models import VacantHouseLeaseToken
from westbusan.vacant_house.publish import (
    VACANT_MANIFEST_TABLES,
    VacantPublicationError,
    canonical_vacant_json,
    publish_vacant_run,
    vacant_manifest_is_valid,
    write_vacant_manifest,
)
from westbusan.vacant_house.stage import stage_archive

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
)


def _database(path: Path) -> Database:
    db = Database(path, Path("sql"))
    db.migrate()
    return db


def _seed_running(db: Database, *, source_count: int = 0) -> VacantHouseLeaseToken:
    run_id, owner = uuid4(), uuid4()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=10)
    db.connection.execute(
        """insert into pipeline_writer_lease (
               lease_key, owner_token, run_id, fence_epoch, heartbeat_at,
               lease_expires_at, fence_touch
           ) values ('writer', ?, ?, 1, ?, ?, 0)""",
        [owner, run_id, now, expires],
    )
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, owner_token,
               fence_epoch, lease_expires_at, source_row_count,
               accepted_record_count, exception_count, started_at
           ) values (?, ?, repeat('a', 64), repeat('b', 64), 'staging-v1',
                     'RUNNING', ?, 1, ?, ?, 0, 0, ?)""",
        [run_id, SNAPSHOT_DATE, owner, expires, source_count, now],
    )
    return VacantHouseLeaseToken(run_id, owner, 1, expires)


def _seed_prior_pointer(db: Database) -> tuple[UUID, tuple[object, ...]]:
    run_id, manifest_id = uuid4(), uuid4()
    published_at = datetime(2025, 1, 31, tzinfo=UTC)
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, fence_epoch,
               started_at, completed_at
           ) values (?, ?, repeat('c', 64), repeat('d', 64), 'prior-v1',
                     'COMPLETED', 1, ?, ?)""",
        [run_id, published_at.date(), published_at, published_at],
    )
    db.connection.execute(
        """insert into vacant_house_completion_manifest (
               manifest_id, vacant_run_id, table_name, row_count,
               row_digest_sha256, schema_version, manifest_json, created_at
           ) values (?, ?, 'vacant_house_revision', 0, repeat('e', 64),
                     'prior-v1', '{}', ?)""",
        [manifest_id, run_id, published_at],
    )
    db.connection.execute(
        """insert into vacant_house_publication_current (
               singleton_key, pointer_id, vacant_run_id, published_at, publisher,
               publication_event_id, manifest_id
           ) values (1, ?, ?, ?, 'prior-actor', ?, ?)""",
        [uuid4(), run_id, published_at, uuid4(), manifest_id],
    )
    pointer = db.query(
        "select * from vacant_house_publication_current where singleton_key = 1"
    )[0]
    return run_id, pointer


def _insert_exception(
    db: Database, run_id: UUID, exception_id: UUID, message: str
) -> None:
    db.connection.execute(
        """insert into vacant_house_exception (
               exception_id, vacant_run_id, exception_code, safe_message,
               evidence_json, resolution_status, created_at
           ) values (?, ?, 'safe_test', ?, '{}', 'OPEN', ?)""",
        [exception_id, run_id, message, datetime(2025, 2, 28, tzinfo=UTC)],
    )


def _manifest_row(db: Database, run_id: UUID, table: str) -> tuple[object, ...]:
    return db.query(
        """select manifest_id, table_name, row_count, row_digest_sha256,
                  schema_version, manifest_json
           from vacant_house_completion_manifest
           where vacant_run_id = ? and table_name = ?""",
        [run_id, table],
    )[0]


def _source_archive(path: Path) -> Path:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, district_code in enumerate(DISTRICT_CODES):
            suffix = district_code[-3:]
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "비공개"
            sheet.append(list(HEADERS))
            sheet.append(
                [
                    district_code,
                    "10100",
                    f"구-{suffix}",
                    "동-101",
                    "1",
                    suffix,
                    0,
                    f"비공개-도로-{suffix}",
                    1999,
                    0,
                    0,
                    "1등급",
                ]
            )
            output = BytesIO()
            workbook.save(output)
            workbook.close()
            archive.writestr(f"district-{index:02d}.xlsx", output.getvalue())
    return path


def _active_import(tmp_path: Path):
    archive = _source_archive(tmp_path / "source.zip")
    bundle = stage_archive(archive, tmp_path / "staged", SNAPSHOT_DATE)
    db = _database(tmp_path / "inventory.duckdb")
    token = prepare_import(db, bundle, actor="internal-operator")
    import_staged_bundle(db, RawStore(tmp_path / "private-raw"), bundle, token)
    return db, token


def test_empty_manifest_has_exact_table_set_and_is_valid(tmp_path: Path) -> None:
    """Missing even an empty target table would make completeness unverifiable."""
    db = _database(tmp_path / "empty.duckdb")
    token = _seed_running(db)

    manifest = write_vacant_manifest(db, token.vacant_run_id, token)

    assert (
        tuple(entry.table_name for entry in manifest.entries) == VACANT_MANIFEST_TABLES
    )
    assert [entry.row_count for entry in manifest.entries] == [0, 0, 0, 0]
    assert {entry.row_digest_sha256 for entry in manifest.entries} == {
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    }
    assert vacant_manifest_is_valid(db, token.vacant_run_id)


def test_manifest_digest_is_independent_of_insertion_order(tmp_path: Path) -> None:
    """Physical insert order must not change a logical table digest."""
    db = _database(tmp_path / "order.duckdb")
    token = _seed_running(db)
    first_id, second_id = uuid4(), uuid4()
    _insert_exception(db, token.vacant_run_id, first_id, "first")
    _insert_exception(db, token.vacant_run_id, second_id, "second")
    first = write_vacant_manifest(db, token.vacant_run_id, token)
    first_digest = next(
        entry.row_digest_sha256
        for entry in first.entries
        if entry.table_name == "vacant_house_exception"
    )
    db.connection.execute(
        "delete from vacant_house_exception where vacant_run_id = ?",
        [token.vacant_run_id],
    )
    _insert_exception(db, token.vacant_run_id, second_id, "second")
    _insert_exception(db, token.vacant_run_id, first_id, "first")

    second = write_vacant_manifest(db, token.vacant_run_id, token)

    assert (
        next(
            entry.row_digest_sha256
            for entry in second.entries
            if entry.table_name == "vacant_house_exception"
        )
        == first_digest
    )


@pytest.mark.parametrize("mutation", ["delete", "update", "insert"])
def test_manifest_invalidates_after_target_table_mutation(
    tmp_path: Path, mutation: str
) -> None:
    """Any post-manifest target mutation must fail validation."""
    db = _database(tmp_path / f"target-{mutation}.duckdb")
    token = _seed_running(db)
    exception_id = uuid4()
    _insert_exception(db, token.vacant_run_id, exception_id, "before")
    write_vacant_manifest(db, token.vacant_run_id, token)
    if mutation == "delete":
        db.connection.execute(
            "delete from vacant_house_exception where exception_id = ?", [exception_id]
        )
    elif mutation == "update":
        db.connection.execute(
            "update vacant_house_exception set safe_message = 'after' where exception_id = ?",
            [exception_id],
        )
    else:
        _insert_exception(db, token.vacant_run_id, uuid4(), "after")

    assert vacant_manifest_is_valid(db, token.vacant_run_id) is False


@pytest.mark.parametrize(
    "mutation",
    ["row_count", "digest", "schema", "json", "missing", "extra"],
)
def test_manifest_rejects_tampered_or_wrong_table_entries(
    tmp_path: Path, mutation: str
) -> None:
    """Stored manifest metadata cannot substitute for rehashed evidence."""
    db = _database(tmp_path / f"manifest-{mutation}.duckdb")
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)
    table = VACANT_MANIFEST_TABLES[0]
    if mutation == "row_count":
        db.connection.execute(
            """update vacant_house_completion_manifest set row_count = 1
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, table],
        )
    elif mutation == "digest":
        db.connection.execute(
            """update vacant_house_completion_manifest
               set row_digest_sha256 = repeat('f', 64)
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, table],
        )
    elif mutation == "schema":
        db.connection.execute(
            """update vacant_house_completion_manifest set schema_version = 'tampered'
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, table],
        )
    elif mutation == "json":
        db.connection.execute(
            """update vacant_house_completion_manifest set manifest_json = '{}'
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, table],
        )
    elif mutation == "missing":
        db.connection.execute(
            """delete from vacant_house_completion_manifest
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, table],
        )
    else:
        db.connection.execute(
            """insert into vacant_house_completion_manifest (
                   manifest_id, vacant_run_id, table_name, row_count,
                   row_digest_sha256, schema_version, manifest_json, created_at
               ) values (?, ?, 'unsupported_table', 0, repeat('f', 64),
                         'schema', '{}', ?)""",
            [uuid4(), token.vacant_run_id, datetime.now(UTC)],
        )

    assert vacant_manifest_is_valid(db, token.vacant_run_id) is False


def test_canonical_manifest_values_reject_nonfinite_and_unsupported() -> None:
    """Platform-dependent scalar encodings cannot enter a digest."""
    with pytest.raises(ValueError, match="nonfinite"):
        canonical_vacant_json((float("nan"),))
    with pytest.raises(TypeError, match="unsupported"):
        canonical_vacant_json((object(),))


def test_import_manifest_and_publication_complete_and_release_exact_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Task 5 result must become one manifest-bound current publication."""
    db, token = _active_import(tmp_path)
    manifest = write_vacant_manifest(db, token.vacant_run_id, token)

    publication = publish_vacant_run(
        db,
        token.vacant_run_id,
        token,
        actor="internal-operator",
        reason="approved snapshot",
    )

    assert publication.vacant_run_id == token.vacant_run_id
    assert publication.manifest_id == manifest.anchor_manifest_id
    assert publication.previous_vacant_run_id is None
    assert db.query(
        """select status, owner_token, lease_expires_at
           from vacant_house_import_run where vacant_run_id = ?""",
        [token.vacant_run_id],
    ) == [("COMPLETED", None, None)]
    assert db.query(
        "select run_id, owner_token from pipeline_writer_lease where lease_key = 'writer'"
    ) == [(None, None)]
    assert db.scalar("select count(*) from vacant_house_publication_audit") == 1
    assert vacant_manifest_is_valid(db, token.vacant_run_id)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_finalizer_rejects_a_lease_that_expires_after_manifest_verification(
    tmp_path: Path,
) -> None:
    """A suspended finalizer cannot resume after both owned leases expire."""
    db = _database(tmp_path / "expired-finalizer.duckdb")
    prior_run_id, prior_pointer = _seed_prior_pointer(db)
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)

    def expire_after_manifest(stage: str, run_id: UUID) -> None:
        if stage != "after_manifest_verification":
            return
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        db.connection.execute(
            """update pipeline_writer_lease set lease_expires_at = ?
               where lease_key = 'writer' and run_id = ?""",
            [expired_at, run_id],
        )
        db.connection.execute(
            """update vacant_house_import_run set lease_expires_at = ?
               where vacant_run_id = ?""",
            [expired_at, run_id],
        )

    with pytest.raises(VacantHouseFenceError, match="fence_lost"):
        publish_vacant_run(
            db,
            token.vacant_run_id,
            token,
            actor="internal-operator",
            reason="approved snapshot",
            stage_hook=expire_after_manifest,
        )

    assert db.query(
        "select * from vacant_house_publication_current where singleton_key = 1"
    ) == [prior_pointer]
    assert db.query(
        "select status, owner_token from vacant_house_import_run where vacant_run_id = ?",
        [token.vacant_run_id],
    ) == [("RUNNING", token.owner_token)]
    assert (
        db.scalar(
            "select count(*) from vacant_house_publication_audit where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 0
    )
    assert prior_run_id is not None


def test_finalizer_rejects_writer_takeover_after_manifest_verification(
    tmp_path: Path,
) -> None:
    """A new shared-writer epoch after hashing must leave publication unchanged."""
    db = _database(tmp_path / "takeover-finalizer.duckdb")
    _prior_run_id, prior_pointer = _seed_prior_pointer(db)
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)

    def replace_shared_epoch(stage: str, _run_id: UUID) -> None:
        if stage != "after_manifest_verification":
            return
        db.connection.execute(
            """update pipeline_writer_lease
               set owner_token = ?, run_id = ?, fence_epoch = fence_epoch + 1,
                   lease_expires_at = ?
               where lease_key = 'writer'""",
            [uuid4(), uuid4(), datetime.now(UTC) + timedelta(minutes=5)],
        )

    with pytest.raises(VacantHouseFenceError, match="fence_lost"):
        publish_vacant_run(
            db,
            token.vacant_run_id,
            token,
            actor="internal-operator",
            reason="approved snapshot",
            stage_hook=replace_shared_epoch,
        )

    assert db.query(
        "select * from vacant_house_publication_current where singleton_key = 1"
    ) == [prior_pointer]
    assert (
        db.scalar(
            "select count(*) from vacant_house_publication_audit where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 0
    )


@pytest.mark.parametrize(
    "stage",
    [
        "after_manifest_verification",
        "after_pointer_update",
        "after_audit_insertion",
        "after_terminal_run_update",
        "after_lease_release",
    ],
)
def test_finalizer_failure_rolls_back_and_retry_publishes_once(
    tmp_path: Path, stage: str
) -> None:
    """Every injected finalizer crash must preserve the exact prior pointer."""
    db = _database(tmp_path / f"crash-{stage}.duckdb")
    prior_run_id, prior_pointer = _seed_prior_pointer(db)
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)

    def fail_at(observed: str, _run_id: UUID) -> None:
        if observed == stage:
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected publication failure"):
        publish_vacant_run(
            db,
            token.vacant_run_id,
            token,
            actor="internal-operator",
            reason="approved snapshot",
            stage_hook=fail_at,
        )

    assert db.query(
        "select * from vacant_house_publication_current where singleton_key = 1"
    ) == [prior_pointer]
    assert db.query(
        "select status, owner_token from vacant_house_import_run where vacant_run_id = ?",
        [token.vacant_run_id],
    ) == [("RUNNING", token.owner_token)]
    assert db.query(
        "select run_id, owner_token from pipeline_writer_lease where lease_key = 'writer'"
    ) == [(token.vacant_run_id, token.owner_token)]
    assert (
        db.scalar(
            "select count(*) from vacant_house_publication_audit where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 0
    )

    published = publish_vacant_run(
        db,
        token.vacant_run_id,
        token,
        actor="internal-operator",
        reason="approved snapshot",
    )

    assert published.previous_vacant_run_id == prior_run_id
    assert (
        db.scalar(
            "select count(*) from vacant_house_publication_audit where vacant_run_id = ?",
            [token.vacant_run_id],
        )
        == 1
    )


def test_same_run_retry_returns_identical_persisted_publication(tmp_path: Path) -> None:
    """A retry cannot duplicate audit or rewrite publication identity."""
    db = _database(tmp_path / "idempotent.duckdb")
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)
    first = publish_vacant_run(
        db,
        token.vacant_run_id,
        token,
        actor="internal-operator",
        reason="approved snapshot",
    )

    second = publish_vacant_run(
        db,
        token.vacant_run_id,
        token,
        actor="internal-operator",
        reason="approved snapshot",
    )

    assert second == first
    assert db.scalar("select count(*) from vacant_house_publication_audit") == 1


@pytest.mark.parametrize(
    "mutation", ["pointer", "audit", "audit_action", "run", "manifest"]
)
def test_same_run_retry_revalidates_every_persisted_identity(
    tmp_path: Path, mutation: str
) -> None:
    """A current pointer is insufficient when its supporting evidence changed."""
    db = _database(tmp_path / f"idempotent-{mutation}.duckdb")
    token = _seed_running(db)
    write_vacant_manifest(db, token.vacant_run_id, token)
    publish_vacant_run(
        db,
        token.vacant_run_id,
        token,
        actor="internal-operator",
        reason="approved snapshot",
    )
    mutations: dict[str, Callable[[], None]] = {
        "pointer": lambda: db.connection.execute(
            "update vacant_house_publication_current set publisher = 'changed'"
        ),
        "audit": lambda: db.connection.execute(
            "update vacant_house_publication_audit set reason = 'changed'"
        ),
        "audit_action": lambda: db.connection.execute(
            "update vacant_house_publication_audit set action = 'changed'"
        ),
        "run": lambda: db.connection.execute(
            """update vacant_house_import_run
               set completed_at = completed_at + interval '1 second'
               where vacant_run_id = ?""",
            [token.vacant_run_id],
        ),
        "manifest": lambda: db.connection.execute(
            """update vacant_house_completion_manifest set row_count = row_count + 1
               where vacant_run_id = ? and table_name = ?""",
            [token.vacant_run_id, VACANT_MANIFEST_TABLES[0]],
        ),
    }
    mutations[mutation]()

    with pytest.raises(VacantPublicationError):
        publish_vacant_run(
            db,
            token.vacant_run_id,
            token,
            actor="internal-operator",
            reason="approved snapshot",
        )
