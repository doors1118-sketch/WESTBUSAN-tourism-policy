import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.cli import app, exit_code_for_summary
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities
from westbusan.models import SourceStatus
from westbusan.orchestrator import Pipeline, RunSummary


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
    } <= set(result.stdout.split())


@pytest.mark.parametrize(
    "command",
    (
        "spatial-boundary-inspect",
        "spatial-boundary-approve",
        "spatial-run",
        "spatial-export",
    ),
)
def test_spatial_cli_help_constructs_each_operator_command(command: str) -> None:
    """Catches an unusable spatial command signature before any DB mutation."""
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0


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
            "91e3b3226f20d3de893e6897ccb1ac57"
            "cd5f27d6bd2625c19c366b1abbbcb4e2"
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
