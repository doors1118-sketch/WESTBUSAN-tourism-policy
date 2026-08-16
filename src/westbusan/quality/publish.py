"""Atomic last-known-good publication pointer management."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from westbusan.db import Database
from westbusan.quality.checks import QualityReport


@dataclass(frozen=True, slots=True)
class PublishResult:
    """The outcome of attempting to publish one validated run."""

    published: bool
    run_id: UUID
    current_run_id: UUID | None
    reason: str | None = None


def can_publish(report: QualityReport) -> bool:
    """Allow warnings and informational skips, but never failed required gates."""
    return not report.has_failed_required_check


def publish_if_valid(db: Database, run_id: UUID, report: QualityReport) -> PublishResult:
    """Atomically point current analytics at *run_id* after required gates pass."""
    if not can_publish(report):
        return PublishResult(False, run_id, current_published_run(db), "required_check_failed")

    try:
        db.connection.execute("begin transaction")
        db.connection.execute(
            """
            insert into publication_state (publication_key, published_run_id)
            values ('current', ?)
            on conflict (publication_key) do update set
                published_run_id = excluded.published_run_id,
                published_at = now()
            """,
            [run_id],
        )
        db.connection.execute("commit")
    except Exception:
        db.connection.execute("rollback")
        raise
    return PublishResult(True, run_id, run_id)


def current_published_run(db: Database) -> UUID | None:
    """Return the single current analytical version, if one has passed quality gates."""
    row = db.query(
        "select published_run_id from publication_state where publication_key = 'current'"
    )
    return row[0][0] if row else None
