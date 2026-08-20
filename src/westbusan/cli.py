"""Typer command line interface for the West Busan accommodation pipeline."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

import typer

from westbusan.db import migrate_legacy_run
from westbusan.orchestrator import Pipeline, RunSummary, export_current, redact_for_log
from westbusan.quality.checks import approve_schema_baseline, observed_schema_contracts
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.export import export_spatial_current
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialPipeline
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
from westbusan.vacant_house.models import (
    StagedVacantBundleError,
    VacantHouseSourceError,
)
from westbusan.vacant_house.publish import (
    VacantPublicationError,
    publish_vacant_run,
    write_vacant_manifest,
)
from westbusan.vacant_house.source import profile_archive
from westbusan.vacant_house.stage import stage_archive, validate_staged_bundle

app = typer.Typer(
    no_args_is_help=True,
    help="Collect, validate, publish, and export West Busan accommodation evidence.",
)


def exit_code_for_summary(summary: RunSummary) -> int:
    """Map the run contract to the documented operator exit codes."""
    if not summary.published or summary.failed_required_checks:
        return 1
    return 2 if summary.warning_count else 0


@app.command("init-db")
def init_db(
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Create or migrate the configured DuckDB database."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    _print_json({"status": "initialized", "db_path": str(pipeline.db.path)})


@app.command("migrate-legacy")
def migrate_legacy(
    run_id: Annotated[UUID, typer.Option(help="Legacy run to approve after revision audit.")],
    operator: Annotated[
        str, typer.Option(help="Operator identity recorded in the append-only audit.")
    ],
    reason: Annotated[
        str, typer.Option(help="Reason for approving this legacy reconstruction.")
    ],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Backfill self-lineage only when immutable revision copies are complete."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    try:
        migrate_legacy_run(
            pipeline.db,
            run_id,
            operator_identity=operator,
            reason=reason,
        )
    except (RuntimeError, ValueError) as error:
        _print_json({"status": "BLOCKED", "run_id": run_id, "reason": str(error)})
        raise typer.Exit(1) from error
    _print_json({"status": "migrated", "run_id": run_id})


@app.command()
def probe(
    source_id: Annotated[
        list[str] | None,
        typer.Option("--source-id", help="Source to probe; repeat to select several."),
    ] = None,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Probe source access with a one-row request."""
    statuses = _pipeline(root).probe(source_id)
    _print_json(
        {
            "statuses": [
                {
                    "source_id": status.source_id,
                    "status": status.status,
                    "checked_at": status.checked_at,
                    "detail": status.detail,
                }
                for status in statuses
            ]
        }
    )
    if any(status.status not in {"READY", "EMPTY"} for status in statuses):
        raise typer.Exit(1)


@app.command("schema-approve")
def schema_approve(
    source_id: Annotated[str | None, typer.Option(help="Observed source to approve.")] = None,
    operation: Annotated[str | None, typer.Option(help="Observed operation to approve.")] = None,
    partition: Annotated[str | None, typer.Option(help="Observed partition to approve.")] = None,
    fingerprint: Annotated[
        str | None, typer.Option(help="Exact observed schema fingerprint.")
    ] = None,
    approver: Annotated[
        str | None, typer.Option(help="Non-secret operator identifier.")
    ] = None,
    rationale: Annotated[
        str | None, typer.Option(help="Brief review rationale without secrets.")
    ] = None,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Display observed contracts and approve only one exact explicit confirmation."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    observed = observed_schema_contracts(pipeline.db)
    values = (source_id, operation, partition, fingerprint, approver, rationale)
    if not any(value is not None for value in values):
        _print_json({"status": "REVIEW_REQUIRED", "observed": observed})
        raise typer.Exit(1)
    if any(value is None or not value.strip() for value in values):
        _print_json(
            {
                "status": "BLOCKED",
                "reason": "incomplete_explicit_confirmation",
                "observed": observed,
            }
        )
        raise typer.Exit(1)
    assert all(value is not None for value in values)
    confirmation = {
        "source_id": source_id,
        "operation": operation,
        "partition": partition,
        "fingerprint": fingerprint,
    }
    if confirmation not in observed:
        _print_json(
            {
                "status": "BLOCKED",
                "reason": "confirmation_does_not_match_observation",
                "observed": observed,
            }
        )
        raise typer.Exit(1)
    approve_schema_baseline(
        pipeline.db,
        source_id,
        operation,
        fingerprint,
        partition=partition,
        approval_method="operator_cli",
        approver=approver,
        rationale=rationale,
    )
    _print_json({"status": "APPROVED", "approved": confirmation, "observed": observed})


@app.command()
def backfill(
    start: Annotated[str, typer.Option(help="Inclusive start date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Inclusive end date (YYYY-MM-DD).")],
    source_id: Annotated[
        list[str] | None,
        typer.Option("--source-id", help="Source to collect; repeat to select several."),
    ] = None,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Collect an inclusive historical range and preserve restart checkpoints."""
    _finish(
        _pipeline(root).backfill(
            _parse_date(start, "start"), _parse_date(end, "end"), source_id
        )
    )


@app.command()
def daily(
    as_of: Annotated[str, typer.Option(help="Business date (YYYY-MM-DD).")],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Run the daily pipeline for an Asia/Seoul business date."""
    _finish(_pipeline(root).daily(_parse_date(as_of, "as-of")))


@app.command()
def quality(
    run_id: Annotated[UUID | None, typer.Option(help="Run to report.")] = None,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Print persisted quality evidence without rerunning or weakening gates."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    selected = run_id
    if selected is None:
        rows = pipeline.db.query(
            """select run_id from pipeline_run
               order by created_at desc, attempt desc nulls last limit 1"""
        )
        selected = rows[0][0] if rows else None
    if selected is None:
        _print_json({"status": "BLOCKED", "reason": "no_pipeline_run"})
        raise typer.Exit(1)
    checks = pipeline.db.query(
        """select check_name, status, severity, source_id, actual_json, expected_json
           from fact_data_quality where run_id = ? order by check_name, source_id""",
        [selected],
    )
    failed = sum(status == "failed" and severity == "required" for _, status, severity, *_ in checks)
    warnings = sum(status == "warning" for _, status, *_ in checks)
    _print_json(
        {
            "run_id": selected,
            "status": "BLOCKED" if failed else "COMPLETED_WITH_WARNINGS" if warnings else "PASSED",
            "failed_required_checks": failed,
            "warning_count": warnings,
            "checks": [
                {
                    "name": name,
                    "status": status,
                    "severity": severity,
                    "source_id": source,
                    "actual": json.loads(actual),
                    "expected": json.loads(expected),
                }
                for name, status, severity, source, actual, expected in checks
            ],
        }
    )
    if failed or not checks:
        raise typer.Exit(1)
    if warnings:
        raise typer.Exit(2)


@app.command()
def export(
    export_date: Annotated[
        str | None,
        typer.Option("--date", help="Export partition date (YYYY-MM-DD)."),
    ] = None,
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Replace a same-date bundle only after manifest verification fails.",
        ),
    ] = False,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Export all four current operator datasets as CSV and Parquet."""
    pipeline = _pipeline(root)
    selected_date = (
        _parse_date(export_date, "date")
        if export_date
        else datetime.now(ZoneInfo("Asia/Seoul")).date()
    )
    try:
        paths = export_current(
            pipeline.db,
            pipeline.settings.data_dir,
            selected_date,
            rebuild=rebuild,
        )
    except (RuntimeError, ValueError) as error:
        _print_json({"status": "BLOCKED", "reason": str(error)})
        raise typer.Exit(1) from error
    _print_json({"status": "exported", "paths": [str(path) for path in paths]})


@app.command("vacant-house-profile")
def vacant_house_profile(
    archive: Annotated[Path, typer.Argument(help="Private source ZIP archive.")],
) -> None:
    """Print only aggregate workbook-format and candidate-row evidence."""
    try:
        profile = profile_archive(archive)
    except Exception as error:  # noqa: BLE001 - safe redaction boundary
        _vacant_blocked(error, "vacant_house_profile_failed")
    _print_json(
        {
            "status": "PROFILED",
            "archive_sha256": profile.archive_sha256,
            "workbook_count": profile.workbook_count,
            "modern_workbook_count": profile.modern_workbook_count,
            "legacy_workbook_count": profile.legacy_workbook_count,
            "candidate_row_count": profile.candidate_row_count,
        }
    )


@app.command("vacant-house-stage")
def vacant_house_stage(
    archive: Annotated[Path, typer.Argument(help="Private source ZIP archive.")],
    snapshot_date: Annotated[str, typer.Argument(help="Source snapshot date (YYYY-MM-DD).")],
    output_root: Annotated[Path, typer.Argument(help="Protected private staging root.")],
) -> None:
    """Create one deterministic sealed bundle without opening DuckDB."""
    try:
        bundle = stage_archive(
            archive,
            output_root,
            _parse_date(snapshot_date, "snapshot-date"),
        )
    except Exception as error:  # noqa: BLE001 - safe redaction boundary
        _vacant_blocked(error, "vacant_house_stage_failed")
    _print_json(
        {
            "status": "STAGED",
            "archive_sha256": bundle.archive_sha256,
            "manifest_sha256": bundle.manifest_sha256,
            "source_row_count": bundle.source_row_count,
            "normalized_row_count": bundle.normalized_row_count,
            "exception_count": bundle.exception_count,
        }
    )


@app.command("vacant-house-import")
def vacant_house_import(
    bundle: Annotated[Path, typer.Argument(help="Validated private staging bundle.")],
    actor: Annotated[str, typer.Argument(help="Internal operator identity.")],
    reason: Annotated[str, typer.Argument(help="Approved publication reason.")],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Import, manifest, and atomically publish one complete private snapshot."""
    token = None
    try:
        staged = validate_staged_bundle(bundle)
        pipeline = _pipeline(root)
        pipeline.db.migrate()
        token = prepare_import(pipeline.db, staged, actor)
        summary = import_staged_bundle(
            pipeline.db,
            pipeline.raw_store,
            staged,
            token,
        )
        write_vacant_manifest(pipeline.db, summary.vacant_run_id, token)
        publication = publish_vacant_run(
            pipeline.db,
            summary.vacant_run_id,
            token,
            actor,
            reason,
        )
    except Exception as error:  # noqa: BLE001 - safe redaction boundary
        if token is not None:
            try:
                release_import(pipeline.db, token)
            except Exception:  # noqa: BLE001, S110 - preserve safe primary failure
                pass
        _vacant_blocked(error, "vacant_house_import_failed")
    _print_json(
        {
            "status": "COMPLETED",
            "vacant_run_id": publication.vacant_run_id,
            "source_row_count": summary.source_row_count,
            "accepted_record_count": summary.current_count,
            "exception_count": summary.exception_count,
        }
    )


@app.command("spatial-boundary-inspect")
def spatial_boundary_inspect(
    boundary_file: Annotated[
        Path,
        typer.Argument(help="Official Busan administrative-boundary GeoJSON."),
    ],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Validate exact boundary bytes and print a redacted review summary."""
    try:
        pipeline = _pipeline(root)
        inspection = inspect_boundary(boundary_file, pipeline.settings.regions)
    except Exception as error:
        _print_json({"status": "BLOCKED", "reason": "boundary_inspection_failed"})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "REVIEW_REQUIRED",
            "content_hash": inspection.content_hash,
            "feature_count": inspection.feature_count,
            "district_count": inspection.district_count,
            "dong_count": inspection.dong_count,
            "crs": inspection.crs,
            "bounds": inspection.bounds,
            "geometry_valid": inspection.geometry_valid,
        }
    )
    raise typer.Exit(1)


@app.command("spatial-boundary-approve")
def spatial_boundary_approve(
    boundary_file: Annotated[
        Path,
        typer.Argument(help="Exact inspected Busan boundary GeoJSON."),
    ],
    sha256: Annotated[str, typer.Option(help="Exact SHA-256 printed by inspection.")],
    approver: Annotated[str, typer.Option(help="Non-secret reviewer identity.")],
    rationale: Annotated[
        str, typer.Option(help="Brief review rationale without secrets.")
    ],
    source_org: Annotated[str, typer.Option(help="Official source organization.")],
    source_url: Annotated[str, typer.Option(help="Official HTTPS source URL.")],
    source_date: Annotated[str, typer.Option(help="Official source date (YYYY-MM-DD).")],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Approve one exact boundary hash and materialize its deterministic grid."""
    try:
        pipeline = _pipeline(root)
        pipeline.db.migrate()
        parsed_source_date = _parse_date(source_date, "source-date")
        inspection = inspect_boundary(boundary_file, pipeline.settings.regions)
        boundary_version_id = approve_boundary(
            pipeline.db,
            pipeline.raw_store,
            boundary_file,
            inspection,
            sha256,
            approver,
            rationale,
            BoundaryMetadata(
                source_org,
                source_url,
                parsed_source_date,
                parsed_source_date.isoformat(),
            ),
        )
        grid = build_grid(
            pipeline.db, boundary_version_id, pipeline.settings.spatial
        )
    except typer.BadParameter:
        raise
    except Exception as error:
        _print_json({"status": "BLOCKED", "reason": "boundary_approval_failed"})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "APPROVED",
            "boundary_version_id": boundary_version_id,
            "content_hash": inspection.content_hash,
            "grid_cell_count": grid.cell_count,
            "grid_row_digest": grid.row_digest,
        }
    )


@app.command("spatial-run")
def spatial_run(
    base_run_id: Annotated[UUID, typer.Option(help="Current published core run.")],
    boundary_version_id: Annotated[
        UUID, typer.Option(help="Reviewed boundary version.")
    ],
    business_date: Annotated[
        str, typer.Option(help="Spatial business date (YYYY-MM-DD).")
    ],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Build and atomically publish all spatial marts for exact inputs."""
    try:
        pipeline = _pipeline(root)
        pipeline.db.migrate()
        summary = SpatialPipeline(pipeline.db, pipeline.settings).run(
            base_run_id,
            boundary_version_id,
            _parse_date(business_date, "business-date"),
        )
    except typer.BadParameter:
        raise
    except Exception as error:
        _print_json({"status": "BLOCKED", "reason": "spatial_run_blocked"})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": summary.status,
            "published": summary.published,
            "spatial_run_id": summary.spatial_run_id,
            "base_run_id": summary.base_published_run_id,
            "boundary_version_id": summary.boundary_version_id,
            "business_date": summary.business_date,
        }
    )


@app.command("spatial-export")
def spatial_export(
    export_date: Annotated[
        str, typer.Option("--date", help="Export partition date (YYYY-MM-DD).")
    ],
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Replace a same-date bundle only after verification fails.",
        ),
    ] = False,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Export the verified current spatial publication as an offline bundle."""
    try:
        pipeline = _pipeline(root)
        pipeline.db.migrate()
        bundle = export_spatial_current(
            pipeline.db,
            pipeline.settings.data_dir,
            _parse_date(export_date, "date"),
            rebuild=rebuild,
        )
        base_run_id = pipeline.db.scalar(
            """select base_published_run_id from spatial_run
               where spatial_run_id = ?""",
            [bundle.spatial_run_id],
        )
    except typer.BadParameter:
        raise
    except Exception as error:
        _print_json({"status": "BLOCKED", "reason": "spatial_export_blocked"})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "exported",
            "spatial_run_id": bundle.spatial_run_id,
            "base_run_id": base_run_id,
            "files": [path.name for path in bundle.paths],
        }
    )


def _finish(summary: RunSummary) -> None:
    _print_json(summary.as_dict())
    code = exit_code_for_summary(summary)
    if code:
        raise typer.Exit(code)


def _pipeline(root: Path | None) -> Pipeline:
    return Pipeline.from_root(root or Path.cwd())


def _parse_date(value: str, option_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter(
            "expected YYYY-MM-DD", param_hint=f"--{option_name}"
        ) from error


def _vacant_blocked(error: Exception, fallback: str) -> NoReturn:
    if isinstance(
        error,
        (VacantHouseSourceError, StagedVacantBundleError, VacantHouseImportError),
    ):
        reason = error.code
    elif isinstance(error, VacantHouseLeaseUnavailable):
        reason = "global_writer_lease_active"
    elif isinstance(error, VacantHouseFenceError):
        reason = "vacant_house_writer_fence_lost"
    elif isinstance(error, VacantPublicationError):
        candidate = str(error)
        reason = (
            candidate
            if candidate
            and candidate.isascii()
            and all(character.isalnum() or character == "_" for character in candidate)
            else fallback
        )
    else:
        reason = fallback
    _print_json({"status": "BLOCKED", "reason": reason})
    raise typer.Exit(1) from error


def _print_json(value: object) -> None:
    typer.echo(
        json.dumps(
            redact_for_log(value),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    app()
