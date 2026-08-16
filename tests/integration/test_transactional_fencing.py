from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import duckdb
import pytest

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.analytics.build import build_marts
from westbusan.config import PolicyConfig
from westbusan.entity_resolution.match import build_facilities
from westbusan.orchestrator import Pipeline
from westbusan.quality.checks import run_quality_suite
from westbusan.quality.publish import publish_if_valid


def test_paused_facility_transaction_conflicts_with_two_connection_takeover(
    tmp_path: Path,
) -> None:
    first, second, run_id = _active_pipelines(tmp_path, "facility")
    load_license_snapshot(
        first.db,
        [
            normalize_license(
                "lodgings",
                {
                    "MNG_NO": "L1",
                    "BPLC_NM": "호텔",
                    "ROAD_NM_ADDR": "부산광역시 사하구 길 1",
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )

    _run_paused_against_takeover(
        first,
        second,
        run_id,
        lambda fence: build_facilities(first.db, run_id, fence_check=fence),
        pause_on_check=2,
    )


def test_paused_quality_transaction_conflicts_with_two_connection_takeover(
    tmp_path: Path,
) -> None:
    first, second, run_id = _active_pipelines(tmp_path, "quality")

    _run_paused_against_takeover(
        first,
        second,
        run_id,
        lambda fence: run_quality_suite(first.db, run_id, fence_check=fence),
        pause_on_check=1,
    )


@pytest.mark.parametrize(
    ("stage", "pause_on_check"),
    (("facility", 3), ("region", 5), ("comparison", 7), ("signal", 9), ("manifest", 11)),
)
def test_each_paused_mart_stage_conflicts_with_two_connection_takeover(
    tmp_path: Path, stage: str, pause_on_check: int
) -> None:
    first, second, run_id = _active_pipelines(tmp_path, f"mart-{stage}")
    load_license_snapshot(
        first.db,
        [
            normalize_license(
                "lodgings",
                {
                    "MNG_NO": "L1",
                    "BPLC_NM": "호텔",
                    "ROAD_NM_ADDR": "부산광역시 사하구 길 1",
                },
                date(2026, 8, 16),
            )
        ],
        run_id,
    )
    build_facilities(first.db, run_id)

    _run_paused_against_takeover(
        first,
        second,
        run_id,
        lambda fence: build_marts(
            first.db,
            run_id,
            PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
            fence_check=fence,
        ),
        pause_on_check=pause_on_check,
    )


def test_paused_publication_conflicts_with_two_connection_takeover(
    tmp_path: Path,
) -> None:
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    summary = first.daily(date(2026, 8, 16))
    assert summary.published
    report = run_quality_suite(first.db, summary.run_id)
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()
    _reactivate_writer(first, summary.run_id)

    _run_paused_against_takeover(
        first,
        second,
        summary.run_id,
        lambda fence: publish_if_valid(
            first.db, summary.run_id, report, fence_check=fence
        ),
        pause_on_check=1,
    )


def _active_pipelines(
    tmp_path: Path, identity: str
) -> tuple[Pipeline, Pipeline, UUID]:
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), identity
    )
    assert run is not None
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()
    return first, second, run.run_id


def _run_paused_against_takeover(
    first: Pipeline,
    second: Pipeline,
    run_id: UUID,
    action,
    *,
    pause_on_check: int,
) -> None:
    paused, release = Event(), Event()
    checks = 0

    def fence() -> None:
        nonlocal checks
        first._assert_fence(run_id)
        checks += 1
        if checks == pause_on_check:
            paused.set()
            if not release.wait(10):
                raise TimeoutError("test did not release paused transaction")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(action, fence)
        assert paused.wait(10)
        with pytest.raises(duckdb.TransactionException, match="Conflict"):
            second.db.query(
                """update pipeline_writer_lease
                   set owner_token = ?, fence_epoch = fence_epoch + 1
                   where lease_key = 'writer' returning fence_epoch""",
                [uuid4()],
            )
        release.set()
        future.result(timeout=10)


def _reactivate_writer(pipeline: Pipeline, run_id: UUID) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=5)
    epoch = int(
        pipeline.db.scalar(
            "select writer_fence_epoch from pipeline_run where run_id = ?", [run_id]
        )
    )
    pipeline.db.connection.execute(
        """update pipeline_run
           set status = 'RUNNING', lease_owner_token = ?, lease_expires_at = ?
           where run_id = ?""",
        [pipeline._lease_owner_token, expires, run_id],
    )
    pipeline.db.connection.execute(
        """update pipeline_writer_lease
           set owner_token = ?, run_id = ?, fence_epoch = ?, lease_expires_at = ?
           where lease_key = 'writer'""",
        [pipeline._lease_owner_token, run_id, epoch, expires],
    )
