import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from typer.testing import CliRunner

import westbusan.cli as cli_module
from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.cli import app, exit_code_for_summary
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities
from westbusan.models import SourceStatus
from westbusan.orchestrator import Pipeline, RunSummary
from westbusan.vacant_house.stage import stage_archive

VACANT_DISTRICT_CODES = (
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
VACANT_HEADERS = (
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


def _vacant_archive(path: Path, districts: tuple[str, ...] = VACANT_DISTRICT_CODES) -> Path:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, district_code in enumerate(districts):
            suffix = district_code[-3:]
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "비공개 원본"
            sheet.append(list(VACANT_HEADERS))
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


def _summary(*, published: bool, warnings: int, failed: int) -> RunSummary:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return RunSummary(
        uuid4(),
        "daily",
        "PUBLISHED" if published else "BLOCKED",
        published,
        1,
        1,
        warnings,
        failed,
        now,
        now,
    )


def test_summary_exit_codes_distinguish_publish_warning_and_block() -> None:
    """Catches warning-only completion being reported as either failure or success."""
    assert exit_code_for_summary(_summary(published=True, warnings=0, failed=0)) == 0
    assert exit_code_for_summary(_summary(published=True, warnings=1, failed=0)) == 2
    assert exit_code_for_summary(_summary(published=False, warnings=0, failed=1)) == 1


def test_cli_help_lists_all_operational_commands() -> None:
    """Catches unsupported annotations preventing the CLI from constructing."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert {
        "init-db",
        "probe",
        "schema-approve",
        "backfill",
        "daily",
        "quality",
        "export",
        "spatial-boundary-inspect",
        "spatial-boundary-approve",
        "spatial-run",
        "spatial-export",
        "spatial-geocode",
        "vacant-house-profile",
        "vacant-house-stage",
        "vacant-house-import",
        "building-profile-backfill",
        "vacant-house-building-link",
        "vacant-house-parcel-context",
    } <= set(result.stdout.split())


@pytest.mark.parametrize(
    "command",
    (
        "vacant-house-profile",
        "vacant-house-stage",
        "vacant-house-import",
        "vacant-house-parcel-context",
    ),
)
def test_vacant_house_cli_help_constructs_safe_operator_commands(command: str) -> None:
    """Catches an unusable vacant-house command before private input is opened."""
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0


def test_vacant_house_profile_outputs_aggregate_evidence_only(tmp_path: Path) -> None:
    """Profile output cannot expose private workbook labels or row values."""
    archive = _vacant_archive(tmp_path / "private-source.zip", ("26380",))

    result = CliRunner().invoke(app, ["vacant-house-profile", str(archive)])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["status"] == "PROFILED"
    assert output["workbook_count"] == 1
    assert output["modern_workbook_count"] == 1
    assert output["legacy_workbook_count"] == 0
    assert output["candidate_row_count"] == 1
    assert len(output["archive_sha256"]) == 64
    for private_value in (
        str(archive),
        "private-source.zip",
        "district-00.xlsx",
        "비공개 원본",
        "비공개-도로-380",
    ):
        assert private_value not in result.stdout


def test_vacant_house_profile_failure_is_redacted_json(tmp_path: Path) -> None:
    """A deliberate source error cannot echo path, token, or a traceback."""
    private_path = tmp_path / "PRIVATE-TOKEN-CREDENTIAL-LOT-7788.zip"

    result = CliRunner().invoke(app, ["vacant-house-profile", str(private_path)])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "invalid_archive",
        "status": "BLOCKED",
    }
    for private_value in (str(private_path), "PRIVATE-TOKEN-CREDENTIAL", "Traceback"):
        assert private_value not in result.output


def test_vacant_house_stage_outputs_counts_and_hashes_without_bundle_path(
    tmp_path: Path,
) -> None:
    """Staging output is sufficient to reconcile rows without disclosing paths."""
    archive = _vacant_archive(tmp_path / "private-stage-source.zip", ("26380",))
    output_root = tmp_path / "PRIVATE-STAGING-ROOT-7788"

    result = CliRunner().invoke(
        app,
        [
            "vacant-house-stage",
            str(archive),
            "2025-02-28",
            str(output_root),
        ],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["status"] == "STAGED"
    assert output["source_row_count"] == 1
    assert output["normalized_row_count"] == 1
    assert output["exception_count"] == 0
    assert len(output["archive_sha256"]) == 64
    assert len(output["manifest_sha256"]) == 64
    assert str(archive) not in result.stdout
    assert str(output_root) not in result.stdout
    assert "PRIVATE-STAGING-ROOT-7788" not in result.stdout


def test_vacant_house_import_publishes_safe_aggregate_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """The operator command must join import, manifest, and publication once."""
    archive = _vacant_archive(tmp_path / "private-import-source.zip")
    bundle = stage_archive(archive, tmp_path / "private-staged", date(2025, 2, 28))
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    arguments = [
        "vacant-house-import",
        str(bundle.path),
        "internal-operator",
        "approved snapshot",
        "--root",
        str(Path.cwd()),
    ]
    result = CliRunner().invoke(app, arguments)
    repeated = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    output = json.loads(result.stdout)
    assert json.loads(repeated.stdout) == output
    assert output["status"] == "COMPLETED"
    assert output["source_row_count"] == 16
    assert output["accepted_record_count"] == 16
    assert output["exception_count"] == 0
    assert len(output["vacant_run_id"]) == 36
    assert str(bundle.path) not in result.stdout
    assert "internal-operator" not in result.stdout
    assert "approved snapshot" not in result.stdout
    assert pipeline.db.scalar("select count(*) from vacant_house_publication_current") == 1
    assert pipeline.db.scalar("select count(*) from vacant_house_publication_audit") == 1
    assert pipeline.db.scalar("select count(*) from raw_artifact") == 1


def test_vacant_house_import_rejects_tamper_without_private_input_echo(
    tmp_path: Path,
) -> None:
    """Bundle validation must happen before DB setup and return only a safe code."""
    archive = _vacant_archive(tmp_path / "private-tamper-source.zip", ("26380",))
    bundle = stage_archive(archive, tmp_path / "PRIVATE-STAGED-TOKEN-7788", date(2025, 2, 28))
    with (bundle.path / "records.parquet").open("ab") as handle:
        handle.write(b"tampered")

    result = CliRunner().invoke(
        app,
        [
            "vacant-house-import",
            str(bundle.path),
            "internal-operator",
            "approved snapshot",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "invalid_staged_bundle",
        "status": "BLOCKED",
    }
    for private_value in (str(bundle.path), "PRIVATE-STAGED-TOKEN-7788", "Traceback"):
        assert private_value not in result.output


def test_vacant_house_import_failure_is_retryable_without_partial_publication(
    tmp_path: Path, monkeypatch
) -> None:
    """A prepublication failure must release safely and permit the exact retry."""
    archive = _vacant_archive(tmp_path / "private-retry-source.zip")
    bundle = stage_archive(archive, tmp_path / "private-retry-stage", date(2025, 2, 28))
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))
    original_write = cli_module.write_vacant_manifest

    def fail_before_publication(*_args, **_kwargs):
        raise RuntimeError("PRIVATE-FAILURE-EVIDENCE-MUST-NOT-PRINT")

    monkeypatch.setattr(cli_module, "write_vacant_manifest", fail_before_publication)
    arguments = [
        "vacant-house-import",
        str(bundle.path),
        "internal-operator",
        "approved retry",
        "--root",
        str(Path.cwd()),
    ]
    failed = CliRunner().invoke(app, arguments)

    assert failed.exit_code == 1
    assert json.loads(failed.stdout) == {
        "reason": "vacant_house_import_failed",
        "status": "BLOCKED",
    }
    assert "PRIVATE-FAILURE-EVIDENCE" not in failed.output
    assert pipeline.db.scalar("select count(*) from vacant_house_publication_current") == 0
    assert pipeline.db.query(
        "select status, owner_token from vacant_house_import_run"
    ) == [("FAILED", None)]
    assert (
        pipeline.db.scalar(
            "select run_id from pipeline_writer_lease where lease_key = 'writer'"
        )
        is None
    )

    monkeypatch.setattr(cli_module, "write_vacant_manifest", original_write)
    retried = CliRunner().invoke(app, arguments)

    assert retried.exit_code == 0
    assert json.loads(retried.stdout)["status"] == "COMPLETED"
    assert pipeline.db.scalar("select count(*) from vacant_house_publication_current") == 1
    assert pipeline.db.scalar("select count(*) from vacant_house_publication_audit") == 1


@pytest.mark.parametrize(
    "command",
    (
        "spatial-boundary-inspect",
        "spatial-boundary-approve",
        "spatial-run",
        "spatial-export",
        "spatial-geocode",
    ),
)
def test_spatial_cli_help_constructs_each_operator_command(command: str) -> None:
    """Catches an unusable spatial command signature before any DB mutation."""
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0


def test_spatial_geocode_blocks_safely_when_server_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a missing credential becoming a traceback or partial DB mutation."""
    monkeypatch.delenv("VWORLD_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        ["spatial-geocode", "--root", str(Path.cwd()), "--limit", "1"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "status": "BLOCKED",
        "reason": "vworld_key_unavailable",
    }


def test_spatial_boundary_inspection_prints_only_review_summary(
    monkeypatch,
) -> None:
    """Catches raw boundary bytes or configured credentials leaking to stdout."""
    secret = "CLI-PRIVATE-CREDENTIAL-MUST-NOT-PRINT"
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", secret)

    result = CliRunner().invoke(
        app,
        [
            "spatial-boundary-inspect",
            "tests/fixtures/spatial/busan_dongs.geojson",
            "--root",
            str(Path.cwd()),
        ],
    )
    output = json.loads(result.stdout)

    assert result.exit_code == 1
    assert output == {
        "bounds": [128.9, 35.05, 128.916, 35.066],
        "content_hash": (
            "5c0456147f201117ae45bb710436a976"
            "7534f097a33eb8c1483fd5e4885e8d16"
        ),
        "crs": "EPSG:4326",
        "district_count": 16,
        "dong_count": 17,
        "feature_count": 17,
        "geometry_valid": True,
        "status": "REVIEW_REQUIRED",
    }
    assert "FeatureCollection" not in result.stdout
    assert secret not in result.stdout


def test_spatial_boundary_missing_file_does_not_echo_internal_path(
    tmp_path: Path,
) -> None:
    """Catches framework validation disclosing a private operator inbox path."""
    private_path = tmp_path / "PRIVATE-INBOX-PATH-MUST-NOT-PRINT.geojson"

    result = CliRunner().invoke(
        app,
        [
            "spatial-boundary-inspect",
            str(private_path),
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "boundary_inspection_failed",
        "status": "BLOCKED",
    }
    assert "PRIVATE-INBOX-PATH-MUST-NOT-PRINT" not in result.output


def test_spatial_boundary_approval_rejects_hash_mismatch_without_leaking_input(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches CLI approval bypassing the reviewed hash or echoing sensitive input."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))
    secret = "CLI-REVIEW-NOTE-MUST-NOT-PRINT"

    result = CliRunner().invoke(
        app,
        [
            "spatial-boundary-approve",
            "tests/fixtures/spatial/busan_dongs.geojson",
            "--sha256",
            "0" * 64,
            "--approver",
            "operator-1",
            "--rationale",
            secret,
            "--source-org",
            "부산광역시",
            "--source-url",
            "https://data.busan.go.kr/boundary",
            "--source-date",
            "2026-08-01",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "boundary_approval_failed",
        "status": "BLOCKED",
    }
    assert secret not in result.stdout
    assert "FeatureCollection" not in result.stdout
    assert pipeline.db.scalar("select count(*) from spatial_boundary_version") == 0


def test_spatial_run_cli_rejects_a_nonpublished_base(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches the CLI deriving map evidence from an invisible core attempt."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    base_run_id = uuid4()
    pipeline.db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'test', now(), 'RUNNING', '2026-08-16', true)""",
        [base_run_id],
    )
    pipeline.db.connection.execute(
        """insert into publication_state (publication_key, published_run_id)
           values ('current', ?)""",
        [base_run_id],
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "spatial-run",
            "--base-run-id",
            str(base_run_id),
            "--boundary-version-id",
            str(uuid4()),
            "--business-date",
            "2026-08-17",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "spatial_run_blocked",
        "status": "BLOCKED",
    }
    assert pipeline.db.scalar("select count(*) from spatial_run") == 0


def test_spatial_export_cli_fails_closed_without_current_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches export manufacturing a bundle without a current spatial publication."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "spatial-export",
            "--date",
            "2026-08-17",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "spatial_export_blocked",
        "status": "BLOCKED",
    }
    assert not (pipeline.settings.data_dir / "spatial_exports").exists()


def test_quality_defaults_to_latest_attempt_not_prior_publication(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a newer blocked attempt being hidden behind last-known-good."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    published = pipeline.daily(date(2026, 8, 16))
    blocked = pipeline.backfill(
        date(2026, 8, 17), date(2026, 8, 17), source_ids=["lodgings"]
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(app, ["quality", "--root", str(Path.cwd())])
    output = json.loads(result.stdout)

    assert published.published is True
    assert blocked.published is False
    assert result.exit_code == 1
    assert output["run_id"] == str(blocked.run_id)
    assert output["status"] == "BLOCKED"


def _seed_schema_observation(tmp_path: Path, monkeypatch) -> tuple[Database, str]:
    data_dir = tmp_path / "data"
    db_path = data_dir / "westbusan.duckdb"
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(tmp_path / "logs"))
    db = Database(db_path, Path("sql"))
    db.migrate()
    body = b'{"data":[{"MNG_NO":"L1","OPN_ATMY_GRP_CD":"6260000"}],"totalCount":1,"pageNo":1,"numOfRows":1}'
    raw_path = tmp_path / "observed.json"
    raw_path.write_bytes(body)
    empty_path = tmp_path / "observed-empty.json"
    empty_body = b'{"data":[],"totalCount":0,"pageNo":1,"numOfRows":0}'
    empty_path.write_bytes(empty_body)
    expected_fingerprint = hashlib.sha256(
        b'["MNG_NO","OPN_ATMY_GRP_CD"]'
    ).hexdigest()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    db.connection.execute(
        """insert into raw_artifact (
               artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
               content_hash, path, created_at, source_date
           ) values (?, ?, 'lodgings', '2026-08-16', ?, 'request', 'content', ?, ?, '2026-08-16')""",
        [
            uuid4(),
            uuid4(),
            json.dumps({"operation": "info", "partition": "2026-08-16"}),
            str(raw_path),
            now,
        ],
    )
    db.connection.execute(
        """insert into raw_artifact (
               artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
               content_hash, path, created_at, source_date
           ) values (?, ?, 'lodgings', '2026-08-16', ?, 'empty-request', 'empty-content', ?, ?, '2026-08-16')""",
        [
            uuid4(),
            uuid4(),
            json.dumps({"operation": "info", "partition": "2026-08-16"}),
            str(empty_path),
            now,
        ],
    )
    return db, expected_fingerprint


def test_schema_approval_first_run_only_displays_observations(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches observed schemas being approved automatically during discovery."""
    db, fingerprint = _seed_schema_observation(tmp_path, monkeypatch)

    result = CliRunner().invoke(app, ["schema-approve", "--root", str(Path.cwd())])
    output = json.loads(result.stdout)

    assert result.exit_code == 1
    assert output["status"] == "REVIEW_REQUIRED"
    assert output["observed"] == [
        {
            "source_id": "lodgings",
            "operation": "info",
            "partition": "2026-08-16",
            "fingerprint": fingerprint,
        }
    ]
    assert db.scalar("select count(*) from quality_schema_baseline") == 0


def test_schema_approval_rejects_a_fingerprint_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a typo or stale review approving a different observed contract."""
    db, _ = _seed_schema_observation(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "schema-approve",
            "--root",
            str(Path.cwd()),
            "--source-id",
            "lodgings",
            "--operation",
            "info",
            "--partition",
            "2026-08-16",
            "--fingerprint",
            "0" * 64,
            "--approver",
            "operator-1",
            "--rationale",
            "reviewed official fields",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "confirmation_does_not_match_observation"
    assert db.scalar("select count(*) from quality_schema_baseline") == 0


def test_schema_approval_requires_all_confirmation_and_audit_fields(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a partial confirmation being interpreted as operator approval."""
    db, fingerprint = _seed_schema_observation(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "schema-approve",
            "--root",
            str(Path.cwd()),
            "--source-id",
            "lodgings",
            "--operation",
            "info",
            "--partition",
            "2026-08-16",
            "--fingerprint",
            fingerprint,
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "incomplete_explicit_confirmation"
    assert db.scalar("select count(*) from quality_schema_baseline") == 0


def test_schema_approval_accepts_exact_observation_with_operator_audit(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches exact reviewed approval failing to persist its human audit trail."""
    db, fingerprint = _seed_schema_observation(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "schema-approve",
            "--root",
            str(Path.cwd()),
            "--source-id",
            "lodgings",
            "--operation",
            "info",
            "--partition",
            "2026-08-16",
            "--fingerprint",
            fingerprint,
            "--approver",
            "operator-1",
            "--rationale",
            "reviewed official fields",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "APPROVED"
    assert db.query(
        """select source_id, operation, partition_key, approved_schema_fingerprint,
                  approval_method, approver, rationale
           from quality_schema_baseline"""
    ) == [
        (
            "lodgings",
            "info",
            "2026-08-16",
            fingerprint,
            "operator_cli",
            "operator-1",
            "reviewed official fields",
        )
    ]
    assert db.query(
        """select source_id, operation, partition_key, approved_schema_fingerprint,
                  approval_method, approver, rationale
           from quality_schema_approval_event"""
    ) == [
        (
            "lodgings",
            "info",
            "2026-08-16",
            fingerprint,
            "operator_cli",
            "operator-1",
            "reviewed official fields",
        )
    ]


def test_migrate_legacy_command_approves_only_backfilled_self_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    """The operator command makes a safely versioned legacy run rebuildable."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    run_id = uuid4()
    pipeline.db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'legacy', now(), 'BLOCKED', '2026-08-16', false)""",
        [run_id],
    )
    load_license_snapshot(
        pipeline.db,
        [
            normalize_license(
                "lodgings",
                {
                    "MNG_NO": "legacy",
                    "BPLC_NM": "레거시호텔",
                    "ROAD_NM_ADDR": "부산광역시 사하구 길 1",
                    "SALS_STTS_CD": "01",
                    "SALS_STTS_NM": "영업",
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )
    pipeline.db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 16, tzinfo=UTC),
            "READY",
            {},
            run_id,
        )
    )
    facility_id = uuid4()
    building_id = uuid4()
    pipeline.db.connection.execute(
        "insert into run_facility values (?, ?, '레거시호텔', '사하구', 'west')",
        [run_id, facility_id],
    )
    pipeline.db.connection.execute(
        """insert into run_facility_license (
               run_id, facility_id, source_id, source_record_id, evidence_json
           ) values (?, ?, 'lodgings', 'legacy', '{}')""",
        [run_id, facility_id],
    )
    pipeline.db.connection.execute(
        """insert into dim_building (building_id, building_key)
           values (?, 'legacy-building')""",
        [building_id],
    )
    pipeline.db.connection.execute(
        """insert into staging_building_snapshot_version (
               version_run_id, building_id, observed_on, parcel_hash,
               is_closed, source_payload_json
           ) values (?, 'legacy-building', '2026-08-16',
                     'legacy-parcel', false, '{}')""",
        [run_id],
    )
    pipeline.db.connection.execute(
        "insert into run_facility_building values (?, ?, ?)",
        [run_id, facility_id, building_id],
    )
    for family, table in (
        ("tourism", "fact_tourism_demand"),
        ("transport", "fact_transport_flow"),
    ):
        pipeline.db.connection.execute(
            f"""insert into {table} (
                   source_id, metric_code, period, district, region_group,
                   dimension_json, dimension_json_hash, source_revision,
                   metric_value, unit, source_payload_json, artifact_id,
                   loaded_run_id, observation_key
               ) values (?, 'visitor', '2026-08', '사하구', 'west',
                         '{{}}', ?, 'revision', 1, 'count', '{{}}', ?, ?, ?)""",
            [
                f"legacy_{family}",
                f"dimension-{family}",
                uuid4(),
                run_id,
                f"legacy-{family}-observation",
            ],
        )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "migrate-legacy",
            "--run-id",
            str(run_id),
            "--operator",
            "tester",
            "--reason",
            "verified fixture migration",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 0
    assert build_facilities(pipeline.db, run_id).facility_count == 0
    assert pipeline.db.query(
        "select status, rebuildable from pipeline_run where run_id = ?", [run_id]
    ) == [("BLOCKED", True)]
    assert pipeline.db.query(
        """select operator_identity, reason, decision
           from legacy_migration_audit where run_id = ?""",
        [run_id],
    ) == [("tester", "verified fixture migration", "approved")]
    assert pipeline.db.query(
        """select family, observation_key from run_fact_observation
           where run_id = ? order by family""",
        [run_id],
    ) == [
        ("tourism", "legacy-tourism-observation"),
        ("transport", "legacy-transport-observation"),
    ]
    assert pipeline.db.scalar(
        "select count(*) from run_license_building_observation where run_id = ?",
        [run_id],
    ) == 1
    assert pipeline.db.scalar(
        "select count(*) from staging_building_revision where version_run_id = ?",
        [run_id],
    ) == 1
    audit_evidence = json.loads(
        pipeline.db.scalar(
            "select evidence_json from legacy_migration_audit where run_id = ?",
            [run_id],
        )
    )
    assert all(
        count == 0
        for name, count in audit_evidence.items()
        if name.startswith("missing_")
    )


def test_migrate_legacy_rejects_null_tourism_key_without_membership(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy demand rows without immutable identity cannot be reconstructed safely."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    run_id = uuid4()
    pipeline.db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'legacy', now(), 'BLOCKED', '2026-08-16', false)""",
        [run_id],
    )
    pipeline.db.connection.execute(
        """insert into fact_tourism_demand (
               source_id, metric_code, period, district, region_group,
               dimension_json, dimension_json_hash, source_revision, metric_value,
               unit, source_payload_json, artifact_id, loaded_run_id, observation_key
           ) values (
               'tourism_data_lab', 'visitor', '2026-08', '사하구', 'west',
               '{}', 'dimension', 'revision', 1, 'count', '{}', ?, ?, null
           )""",
        [uuid4(), run_id],
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "migrate-legacy",
            "--run-id",
            str(run_id),
            "--operator",
            "tester",
            "--reason",
            "legacy audit",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    assert pipeline.db.scalar(
        "select rebuildable from pipeline_run where run_id = ?", [run_id]
    ) is False
    assert pipeline.db.query(
        """select operator_identity, reason, decision
           from legacy_migration_audit where run_id = ?""",
        [run_id],
    ) == [("tester", "legacy audit", "rejected")]


def test_migrate_legacy_rejects_unrecoverable_base_only_license_history(
    tmp_path: Path, monkeypatch
) -> None:
    """A mutable base row cannot stand in for a missing immutable run revision."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    run_id = uuid4()
    pipeline.db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'legacy', now(), 'BLOCKED', '2026-08-16', false)""",
        [run_id],
    )
    load_license_snapshot(
        pipeline.db,
        [
            normalize_license(
                "lodgings",
                {
                    "MNG_NO": "lost-history",
                    "BPLC_NM": "이력유실호텔",
                    "ROAD_NM_ADDR": "부산광역시 사하구 길 2",
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )
    pipeline.db.connection.execute(
        "delete from staging_license_snapshot_version where version_run_id = ?",
        [run_id],
    )
    pipeline.db.connection.execute(
        "delete from staging_license_revision where version_run_id = ?", [run_id]
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "migrate-legacy",
            "--run-id",
            str(run_id),
            "--operator",
            "tester",
            "--reason",
            "base history audit",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    evidence = json.loads(
        pipeline.db.scalar(
            "select evidence_json from legacy_migration_audit where run_id = ?",
            [run_id],
        )
    )
    assert evidence["missing_license_base_revisions"] == 1


@pytest.mark.parametrize("has_dim_building", (True, False))
def test_migrate_legacy_rejects_linked_building_without_visible_revision(
    tmp_path: Path, monkeypatch, has_dim_building: bool
) -> None:
    """A missing dimension or revision makes a run-scoped building link unsafe."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    run_id, facility_id, building_id = uuid4(), uuid4(), uuid4()
    pipeline.db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'legacy', now(), 'BLOCKED', '2026-08-16', false)""",
        [run_id],
    )
    load_license_snapshot(
        pipeline.db,
        [
            normalize_license(
                "lodgings",
                {
                    "MNG_NO": "linked-no-revision",
                    "BPLC_NM": "건축물이력유실호텔",
                    "ROAD_NM_ADDR": "부산광역시 사하구 길 3",
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )
    pipeline.db.connection.execute(
        "insert into run_facility values (?, ?, '호텔', '사하구', 'west')",
        [run_id, facility_id],
    )
    pipeline.db.connection.execute(
        """insert into run_facility_license (
               run_id, facility_id, source_id, source_record_id, evidence_json
           ) values (?, ?, 'lodgings', 'linked-no-revision', '{}')""",
        [run_id, facility_id],
    )
    if has_dim_building:
        pipeline.db.connection.execute(
            """insert into dim_building (building_id, building_key)
               values (?, 'unversioned-building')""",
            [building_id],
        )
    pipeline.db.connection.execute(
        "insert into run_facility_building values (?, ?, ?)",
        [run_id, facility_id, building_id],
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        [
            "migrate-legacy",
            "--run-id",
            str(run_id),
            "--operator",
            "tester",
            "--reason",
            "linked building audit",
            "--root",
            str(Path.cwd()),
        ],
    )

    assert result.exit_code == 1
    evidence = json.loads(
        pipeline.db.scalar(
            "select evidence_json from legacy_migration_audit where run_id = ?",
            [run_id],
        )
    )
    assert evidence["missing_linked_building_revisions"] == 1


def test_export_requires_explicit_rebuild_for_mismatched_same_date_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLI never silently replaces a corrupted same-date export directory."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.daily(date(2026, 8, 16))
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))
    runner = CliRunner()
    arguments = [
        "export",
        "--date",
        "2026-08-16",
        "--root",
        str(Path.cwd()),
    ]
    assert runner.invoke(app, arguments).exit_code == 0
    exported = pipeline.settings.data_dir / "exports" / "export_date=2026-08-16"
    (exported / "facility_current.csv").write_bytes(b"tampered")

    rejected = runner.invoke(app, arguments)
    rebuilt = runner.invoke(app, [*arguments, "--rebuild"])

    assert rejected.exit_code == 1
    assert rebuilt.exit_code == 0
    assert (exported / "facility_current.csv").read_bytes() != b"tampered"
