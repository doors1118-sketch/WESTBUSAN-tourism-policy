"""Typer command line interface for the West Busan accommodation pipeline."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import duckdb
import httpx
import typer

from westbusan.buildings.load import backfill_building_investment_profiles
from westbusan.db import migrate_legacy_run
from westbusan.orchestrator import Pipeline, RunSummary, export_current, redact_for_log
from westbusan.quality.checks import approve_schema_baseline, observed_schema_contracts
from westbusan.river_regulation.geometry import (
    collect_nakdong_parcel_geometries,
    load_current_nakdong_regulation_pnus,
    publish_nakdong_parcel_geometry_snapshot,
)
from westbusan.river_regulation.heritage import (
    collect_heritage_snapshot,
    publish_heritage_snapshot,
)
from westbusan.river_regulation.parcel import (
    collect_nakdong_parcel_records,
    publish_nakdong_parcel_snapshot,
)
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.enrich import enrich_current_facilities
from westbusan.spatial.export import export_spatial_current
from westbusan.spatial.geocode import VWorldGeocoder
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialPipeline
from westbusan.vacant_house.cadastral import VWorldCadastralClient
from westbusan.vacant_house.fencing import (
    VacantHouseFenceError,
    VacantHouseLeaseUnavailable,
)
from westbusan.vacant_house.importer import (
    VacantHouseImportError,
    fail_import,
    import_staged_bundle,
    load_completed_import,
    prepare_import,
)
from westbusan.vacant_house.models import (
    StagedVacantBundleError,
    VacantHouseSourceError,
)
from westbusan.vacant_house.parcel_context import VWorldNedParcelContextClient
from westbusan.vacant_house.parcel_context_store import (
    ParcelContextCollectionError,
    ParcelContextPublicationError,
    ParcelContextSource,
    collect_current_parcel_context,
    publish_parcel_context,
)
from westbusan.vacant_house.publish import (
    VacantPublicationError,
    load_published_vacant_run,
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
        summary = load_completed_import(pipeline.db, staged)
        if summary is None:
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
        else:
            publication = load_published_vacant_run(
                pipeline.db,
                summary.vacant_run_id,
                actor,
                reason,
            )
    except Exception as error:  # noqa: BLE001 - safe redaction boundary
        if token is not None:
            try:
                fail_import(pipeline.db, token)
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


@app.command("building-profile-backfill")
def building_profile_backfill(
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Replay immutable building payloads into versioned investment profiles."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    result = backfill_building_investment_profiles(pipeline.db)
    _print_json(
        {
            "status": "COMPLETED" if not result.invalid_payload_rows else "WARNING",
            "scanned_rows": result.scanned_rows,
            "profile_rows": result.profile_rows,
            "invalid_payload_rows": result.invalid_payload_rows,
        }
    )
    if result.invalid_payload_rows:
        raise typer.Exit(2)


@app.command("vacant-house-building-link")
def vacant_house_building_link(
    as_of: Annotated[str, typer.Option(help="Business date (YYYY-MM-DD).")],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Collect and publish building-register evidence for current A/B candidates."""
    _finish(
        _pipeline(root).enrich_vacant_candidate_buildings(
            _parse_date(as_of, "as-of")
        )
    )


@app.command("vacant-house-parcel-context")
def vacant_house_parcel_context(
    actor: Annotated[str, typer.Argument(help="Internal operator identity.")],
    reason: Annotated[str, typer.Argument(help="Approved publication reason.")],
    domain: Annotated[
        str,
        typer.Option(help="Domain registered to the VWorld API key."),
    ] = "tourism.busanproduct.co.kr",
    minimum_coverage: Annotated[
        float,
        typer.Option(help="Minimum matched share required per source."),
    ] = 0.8,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Collect and publish PNU-bound VWorld planning and land characteristics."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    key = os.environ.get("VWORLD_API_KEY", "")
    if not key:
        _print_json({"status": "BLOCKED", "reason": "vworld_api_key_required"})
        raise typer.Exit(1)
    current = pipeline.db.query(
        "select vacant_run_id from vacant_house_publication_current where singleton_key=1"
    )
    if len(current) != 1:
        _print_json({"status": "BLOCKED", "reason": "vacant_pointer_missing"})
        raise typer.Exit(1)
    inventory_run_id = current[0][0]
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            result = collect_current_parcel_context(
                pipeline.db,
                inventory_run_id=inventory_run_id,
                sources=(
                    ParcelContextSource(
                        "land_use",
                        VWorldNedParcelContextClient(
                            api_key=key,
                            domain=domain,
                            client=client,
                            kind="land_use",
                            source_id="vworld_land_use",
                        ),
                    ),
                    ParcelContextSource(
                        "land_characteristics",
                        VWorldNedParcelContextClient(
                            api_key=key,
                            domain=domain,
                            client=client,
                            kind="land_characteristics",
                            source_id="vworld_land_characteristics",
                        ),
                    ),
                ),
            )
        if result.status != "COMPLETED":
            raise ParcelContextCollectionError("provider_quality_blocked")
        publish_parcel_context(
            pipeline.db,
            context_run_id=result.context_run_id,
            publisher=actor,
            reason=reason,
            minimum_matched_coverage=minimum_coverage,
        )
    except (ParcelContextCollectionError, ParcelContextPublicationError) as error:
        _print_json({"status": "BLOCKED", "reason": str(error)})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "COMPLETED",
            "context_run_id": result.context_run_id,
            "inventory_run_id": result.inventory_run_id,
            "observation_count": result.observation_count,
            "matched_count": result.matched_count,
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


@app.command("spatial-geocode")
def spatial_geocode(
    limit: Annotated[
        int | None,
        typer.Option(help="Maximum current facilities to checkpoint in this call."),
    ] = None,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Resolve current accommodation addresses through the reviewed VWorld cache."""
    api_key = os.environ.get("VWORLD_API_KEY", "")
    if not api_key:
        _print_json({"status": "BLOCKED", "reason": "vworld_key_unavailable"})
        raise typer.Exit(1)
    try:
        pipeline = _pipeline(root)
        pipeline.db.migrate()
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
        ) as client:
            summary = enrich_current_facilities(
                pipeline.db,
                VWorldGeocoder(api_key, client),
                limit=limit,
            )
    except Exception as error:
        _print_json({"status": "BLOCKED", "reason": "spatial_geocode_blocked"})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "CHECKPOINTED",
            "total": summary.total,
            "matched": summary.matched,
            "cache_hits": summary.cache_hits,
            "district_mismatch": summary.district_mismatch,
            "not_found": summary.not_found,
            "provider_error": summary.provider_error,
            "invalid_response": summary.invalid_response,
            "missing_address": summary.missing_address,
        }
    )


@app.command("heritage-criteria-sync")
def heritage_criteria_sync(
    west: Annotated[float, typer.Option(help="WGS84 minimum longitude.")] = 128.75,
    south: Annotated[float, typer.Option(help="WGS84 minimum latitude.")] = 35.0,
    east: Annotated[float, typer.Option(help="WGS84 maximum longitude.")] = 129.12,
    north: Annotated[float, typer.Option(help="WGS84 maximum latitude.")] = 35.32,
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Cache one complete official HGIS criteria snapshot in DuckDB."""
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    run_id = uuid4()
    checked_at = datetime.now(ZoneInfo("Asia/Seoul"))
    bounds = (west, south, east, north)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "westbusan-policy-screen/1.0"},
        ) as client:
            collected = collect_heritage_snapshot(client, bounds=bounds)
        publish_heritage_snapshot(
            pipeline.db,
            run_id=run_id,
            checked_at=checked_at,
            bounds=bounds,
            designations=collected.designations,
            criteria_zones=collected.criteria_zones,
        )
    except Exception as error:
        _print_json(
            {
                "status": "BLOCKED",
                "reason": (
                    str(error)
                    if isinstance(error, ValueError)
                    else "heritage_criteria_sync_failed"
                ),
            }
        )
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "PUBLISHED",
            "run_id": run_id,
            "checked_at": checked_at,
            "designation_count": len(collected.designations),
            "criteria_zone_count": len(collected.criteria_zones),
            "bounds": bounds,
        }
    )


@app.command("nakdong-parcel-regulation-sync")
def nakdong_parcel_regulation_sync(
    pnu_file: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 text file containing one 19-digit review PNU per line.",
        ),
    ],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Publish a complete PNU-bound planning snapshot for the Nakdong tab only."""
    api_key = os.getenv("VWORLD_API_KEY", "").strip()
    if not api_key:
        _print_json({"status": "BLOCKED", "reason": "vworld_api_key_missing"})
        raise typer.Exit(1)
    domain = os.getenv("VWORLD_API_DOMAIN", "busanproduct.co.kr").strip()
    pnus = [
        line.strip()
        for line in pnu_file.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    run_id = uuid4()
    checked_at = datetime.now(ZoneInfo("Asia/Seoul"))
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "westbusan-policy-screen/1.0"},
        ) as client:
            records = collect_nakdong_parcel_records(
                pnus,
                land_use_client=VWorldNedParcelContextClient(
                    api_key=api_key,
                    domain=domain,
                    client=client,
                    kind="land_use",
                    source_id="nakdong_vworld_land_use",
                ),
                land_characteristics_client=VWorldNedParcelContextClient(
                    api_key=api_key,
                    domain=domain,
                    client=client,
                    kind="land_characteristics",
                    source_id="nakdong_vworld_land_characteristics",
                ),
            )
        publish_nakdong_parcel_snapshot(
            pipeline.db,
            run_id=run_id,
            checked_at=checked_at,
            parcels=records,
        )
    except Exception as error:
        safe_reason = (
            str(error)
            if isinstance(error, (TypeError, ValueError))
            else "nakdong_parcel_sync_failed"
        )
        _print_json({"status": "BLOCKED", "reason": safe_reason})
        raise typer.Exit(1) from error
    _print_json(
        {
            "status": "PUBLISHED",
            "run_id": run_id,
            "checked_at": checked_at,
            "parcel_count": len(records),
            "land_use_coverage": 1.0,
        }
    )


@app.command("nakdong-parcel-geometry-sync")
def nakdong_parcel_geometry_sync(
    pnu_file: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 text file containing one 19-digit review PNU per line.",
        ),
    ],
    root: Annotated[Path | None, typer.Option(help="Repository root.")] = None,
) -> None:
    """Publish cadastral geometries for click-to-PNU resolution on the river map."""
    api_key = os.getenv("VWORLD_API_KEY", "").strip()
    if not api_key:
        _print_json({"status": "BLOCKED", "reason": "vworld_api_key_missing"})
        raise typer.Exit(1)
    domain = os.getenv("VWORLD_API_DOMAIN", "busanproduct.co.kr").strip()
    pnus = [
        line.strip()
        for line in pnu_file.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    try:
        approved_pnus = load_current_nakdong_regulation_pnus(
            pipeline.db.connection
        )
    except (duckdb.Error, ValueError) as error:
        _print_json({"status": "BLOCKED", "reason": str(error)})
        raise typer.Exit(1) from error
    if len(pnus) != len(set(pnus)):
        _print_json({"status": "BLOCKED", "reason": "duplicate_pnu_in_input"})
        raise typer.Exit(1)
    if set(pnus) != set(approved_pnus):
        _print_json(
            {
                "status": "BLOCKED",
                "reason": "approved_pnu_membership_mismatch",
                "input_count": len(pnus),
                "approved_count": len(approved_pnus),
                "missing_count": len(set(approved_pnus) - set(pnus)),
                "unexpected_count": len(set(pnus) - set(approved_pnus)),
            }
        )
        raise typer.Exit(1)
    run_id = uuid4()
    checked_at = datetime.now(ZoneInfo("Asia/Seoul"))
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "westbusan-policy-screen/1.0"},
        ) as client:
            records = collect_nakdong_parcel_geometries(
                pnus,
                client=VWorldCadastralClient(
                    api_key=api_key,
                    domain=domain,
                    client=client,
                ),
            )
        publish_nakdong_parcel_geometry_snapshot(
            pipeline.db,
            run_id=run_id,
            checked_at=checked_at,
            records=records,
        )
    except Exception as error:
        safe_reason = (
            str(error)
            if isinstance(error, (TypeError, ValueError))
            else "nakdong_parcel_geometry_sync_failed"
        )
        _print_json({"status": "BLOCKED", "reason": safe_reason})
        raise typer.Exit(1) from error
    matched_count = sum(record["status"] == "matched" for record in records)
    _print_json(
        {
            "status": "PUBLISHED",
            "run_id": run_id,
            "checked_at": checked_at,
            "target_count": len(records),
            "matched_count": matched_count,
            "coverage": matched_count / len(records),
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
