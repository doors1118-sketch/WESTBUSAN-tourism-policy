from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.quality.checks import CheckResult, QualityReport
from westbusan.quality.publish import current_published_run, publish_if_valid


def test_rejected_run_never_replaces_last_known_good_publication(tmp_path: Path) -> None:
    """Catches a failed run advancing the analytical publication pointer."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    run_a, run_b = uuid4(), uuid4()
    valid = QualityReport([CheckResult("rows", "passed", actual=10, expected=">0")])
    invalid = QualityReport([CheckResult("rows", "failed", actual=0, expected=">0")])

    first = publish_if_valid(db, run_a, valid)
    rejected = publish_if_valid(db, run_b, invalid)

    assert first.published is True
    assert rejected.published is False
    assert current_published_run(db) == run_a


def test_publication_is_idempotent_for_a_valid_run(tmp_path: Path) -> None:
    """Catches repeated publication changing the version or adding another pointer."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    report = QualityReport([CheckResult("rows", "passed", actual=10, expected=">0")])

    first = publish_if_valid(db, run_id, report)
    second = publish_if_valid(db, run_id, report)

    assert first.published is True
    assert second.published is True
    assert current_published_run(db) == run_id
    assert db.query("select count(*) from publication_state") == [(1,)]
