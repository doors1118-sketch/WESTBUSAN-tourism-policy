"""Typer command line interface for the West Busan accommodation pipeline."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

import typer

from westbusan.orchestrator import Pipeline, RunSummary, export_current, redact_for_log

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
        paths = export_current(pipeline.db, pipeline.settings.data_dir, selected_date)
    except ValueError as error:
        _print_json({"status": "BLOCKED", "reason": str(error)})
        raise typer.Exit(1) from error
    _print_json({"status": "exported", "paths": [str(path) for path in paths]})


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
