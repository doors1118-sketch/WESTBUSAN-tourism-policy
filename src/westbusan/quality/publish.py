"""Atomic publication of one verified, persisted quality suite."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import duckdb

from westbusan.db import Database
from westbusan.quality.checks import (
    QualityReport,
    persisted_report_is_valid,
    persisted_required_failures,
)


@dataclass(frozen=True, slots=True)
class PublishResult:
    published: bool
    run_id: UUID
    current_run_id: UUID | None
    reason: str | None = None


def can_publish(report: QualityReport) -> bool:
    """Report-local helper; publication additionally verifies persisted evidence."""
    return not report.has_failed_required_check


def publish_if_valid(db: Database, run_id: UUID, report: QualityReport) -> PublishResult:
    """Advance only for this exact, complete, untampered quality suite."""
    if not can_publish(report) or not persisted_report_is_valid(db, run_id, report):
        return PublishResult(False, run_id, current_published_run(db), "invalid_quality_suite")
    if persisted_required_failures(db, run_id):
        return PublishResult(False, run_id, current_published_run(db), "required_check_failed")
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if not persisted_report_is_valid(db, run_id, report) or persisted_required_failures(db, run_id):
            db.connection.execute("rollback")
            began = False
            return PublishResult(False, run_id, current_published_run(db), "invalid_quality_suite")
        current = current_published_run(db)
        if current == run_id:
            db.connection.execute("commit")
            began = False
            return PublishResult(True, run_id, run_id)
        db.connection.execute("""insert into publication_state (publication_key, published_run_id) values ('current', ?) on conflict (publication_key) do update set published_run_id = excluded.published_run_id, published_at = now()""", [run_id])
        db.connection.execute("commit")
        began = False
    except Exception:
        _rollback_if_started(db, began)
        raise
    return PublishResult(True, run_id, run_id)


def current_published_run(db: Database) -> UUID | None:
    rows = db.query("select published_run_id from publication_state where publication_key = 'current'")
    return rows[0][0] if rows else None


def _rollback_if_started(db: Database, began: bool) -> None:
    """Preserve the original database exception if its rollback also fails."""
    if not began:
        return
    try:
        db.connection.execute("rollback")
    except duckdb.Error:
        return
