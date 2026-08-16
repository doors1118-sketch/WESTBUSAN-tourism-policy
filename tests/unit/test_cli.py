import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.cli import app, exit_code_for_summary
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities
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
    } <= set(result.stdout.split())


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
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(
        app,
        ["migrate-legacy", "--run-id", str(run_id), "--root", str(Path.cwd())],
    )

    assert result.exit_code == 0
    assert build_facilities(pipeline.db, run_id).facility_count == 1


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
