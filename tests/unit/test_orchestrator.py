import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

import westbusan.orchestrator as orchestrator_module
from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.entity_resolution.match import build_facilities
from westbusan.models import SourceSpec, SourceStatus
from westbusan.orchestrator import (
    Pipeline,
    export_current,
    iter_source_partitions,
    redact_for_log,
)
from westbusan.sources.registry import SourceRegistry
from westbusan.transport.load import SourceMonthEvidence, load_transport


def test_monthly_partitions_include_both_boundary_months() -> None:
    """Catches a backfill that drops a partial first or last month."""
    spec = SourceSpec("monthly", "https://example.test", cadence="monthly")

    assert iter_source_partitions(spec, date(2026, 1, 15), date(2026, 3, 2)) == (
        "2026-01",
        "2026-02",
        "2026-03",
    )


def test_current_only_backfill_uses_one_restartable_snapshot(tmp_path: Path) -> None:
    """Catches replaying one current-state source once per historical month."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))

    first = pipeline.backfill(
        date(2022, 1, 1), date(2026, 8, 16), source_ids=["lodgings"]
    )
    second = pipeline.backfill(
        date(2022, 1, 1), date(2026, 8, 16), source_ids=["lodgings"]
    )

    assert first.published is False
    assert second.published is False
    assert second.run_id != first.run_id
    assert pipeline.db.query(
        "select attempt, status from pipeline_run order by attempt"
    ) == [(1, "BLOCKED"), (2, "BLOCKED")]
    assert pipeline.db.scalar("select count(*) from raw_artifact") == 2
    assert pipeline.db.scalar("select count(distinct path) from raw_artifact") == 1
    checkpoint = pipeline.db.scalar(
        """select checkpoint_json from collection_checkpoint
           where source_id = 'lodgings' and partition_key = 'snapshot:2026-08-16'"""
    )
    assert json.loads(checkpoint)["status"] == "completed"
    assert json.loads(checkpoint)["run_id"] == str(second.run_id)


def test_second_pipeline_cannot_acquire_an_active_run_attempt_lease(
    tmp_path: Path,
) -> None:
    """Catches two processes receiving write access to the same RUNNING attempt."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    first_run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "lease-contention"
    )
    assert first_run is not None
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()

    with pytest.raises(RuntimeError, match="active lease"):
        second._prepare_run(
            "fixture", "daily", date(2026, 8, 16), "lease-contention"
        )


def test_global_writer_lease_blocks_a_different_logical_run(
    tmp_path: Path,
) -> None:
    """Catches simultaneous logical runs mutating shared entity/review tables."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "logical-a"
    )
    assert run is not None
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()

    with pytest.raises(RuntimeError, match="global writer lease"):
        second._prepare_run(
            "fixture", "daily", date(2026, 8, 17), "logical-b"
        )


def test_terminal_source_status_and_completed_checkpoint_commit_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches restart honoring completed after terminal status failed to persist."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    run, _ = pipeline._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "checkpoint-atomic"
    )
    assert run is not None

    def fail_status(_status):
        raise RuntimeError("status write failed")

    monkeypatch.setattr(pipeline.db, "record_source_status", fail_status)
    with pytest.raises(RuntimeError, match="status write failed"):
        pipeline._complete_source_partition(
            run,
            "lodgings",
            "snapshot:2026-08-16",
            2,
            "READY",
            {"row_count": 1},
        )

    assert pipeline.db.query(
        """select checkpoint_json from collection_checkpoint
           where source_id = 'lodgings'"""
    ) == []


def test_stale_owner_cannot_delete_facilities_or_pending_reviews_after_takeover(
    tmp_path: Path,
) -> None:
    """Catches a stale facility rebuild deleting current global/review state."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "facility-fence"
    )
    assert run is not None
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "L1",
            "BPLC_NM": "호텔",
            "ROAD_NM_ADDR": "부산광역시 사하구 하단동 1",
        },
        date(2026, 8, 16),
    )
    load_license_snapshot(first.db, [record], run.run_id)
    build_facilities(first.db, run.run_id)
    review_id = uuid4()
    first.db.connection.execute(
        "insert into duplicate_review (review_id, evidence_json) values (?, '{}')",
        [review_id],
    )
    before = (
        first.db.scalar("select count(*) from dim_facility"),
        first.db.scalar("select count(*) from duplicate_review"),
    )
    first.db.connection.execute(
        "update pipeline_run set lease_expires_at = now() - interval '1 second' where run_id = ?",
        [run.run_id],
    )
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()
    taken, _ = second._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "facility-fence"
    )
    assert taken is not None

    with pytest.raises(RuntimeError, match="writer fence"):
        build_facilities(
            first.db,
            run.run_id,
            fence_check=lambda: first._assert_fence(run.run_id),
        )

    assert (
        first.db.scalar("select count(*) from dim_facility"),
        first.db.scalar("select count(*) from duplicate_review"),
    ) == before


def test_prepare_run_separates_utc_execution_time_from_business_date(
    tmp_path: Path,
) -> None:
    """Catches a Seoul business midnight being stored as the actual start timestamp."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.db.migrate()
    pipeline.db.connection.execute("set TimeZone='UTC'")
    before = datetime.now(UTC)

    run, _ = pipeline._prepare_run(
        "fixture", "daily", date(2020, 1, 2), "utc-business-date"
    )

    assert run is not None
    started_text, business_date = pipeline.db.query(
        "select started_at::varchar, business_date from pipeline_run where run_id = ?",
        [run.run_id],
    )[0]
    started_at = datetime.fromisoformat(str(started_text))
    assert started_at >= before
    assert business_date == date(2020, 1, 2)
    assert run.business_date == date(2020, 1, 2)


def test_expired_lease_takeover_revokes_the_previous_owner(
    tmp_path: Path,
) -> None:
    """Catches a stale owner continuing checkpoints or finalization after takeover."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    first_run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "stale-takeover"
    )
    assert first_run is not None
    first._checkpoint("lodgings", "2026-08", "completed", 2, first_run.run_id)
    first_owner = first.db.scalar(
        "select lease_owner_token from pipeline_run where run_id = ?",
        [first_run.run_id],
    )
    first.db.connection.execute(
        """update pipeline_run set lease_expires_at = now() - interval '1 second'
           where run_id = ?""",
        [first_run.run_id],
    )
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()

    second_run, _ = second._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "stale-takeover"
    )

    assert second_run is not None
    assert second_run.run_id == first_run.run_id
    second_owner, heartbeat, lease_expires = second.db.query(
        """select lease_owner_token, heartbeat_at::varchar, lease_expires_at::varchar
           from pipeline_run where run_id = ?""",
        [first_run.run_id],
    )[0]
    assert second_owner != first_owner
    assert heartbeat is not None
    assert datetime.fromisoformat(str(lease_expires)) > datetime.fromisoformat(
        str(heartbeat)
    )
    with pytest.raises(RuntimeError, match="lease ownership"):
        first._checkpoint("lodgings", "2026-08", "completed", 3, first_run.run_id)
    with pytest.raises(RuntimeError, match="lease ownership"):
        first._commit_terminal_summary(
            orchestrator_module.RunSummary(
                first_run.run_id,
                "daily",
                "BLOCKED",
                False,
                0,
                0,
                0,
                1,
                first_run.started_at,
                datetime.now(UTC),
            )
        )
    second._checkpoint("lodgings", "2026-08", "completed", 4, second_run.run_id)
    assert json.loads(
        second.db.scalar(
            """select checkpoint_json from collection_checkpoint
               where source_id = 'lodgings' and partition_key = '2026-08'"""
        )
    )["next_page"] == 4


def test_expired_lease_takeover_fences_old_collectors_and_failure_status(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches stale collectors writing raw, staging, or status evidence after takeover."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    first_run, _ = first._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "collector-takeover"
    )
    assert first_run is not None
    logger = orchestrator_module._JsonlLogger(tmp_path / "logs", date(2026, 8, 16))
    first.db.connection.execute(
        """update pipeline_run set lease_expires_at = now() - interval '1 second'
           where run_id = ?""",
        [first_run.run_id],
    )
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()
    second_run, _ = second._prepare_run(
        "fixture", "daily", date(2026, 8, 16), "collector-takeover"
    )
    assert second_run is not None
    assert second_run.run_id == first_run.run_id
    first.settings.service_key = SecretStr("test-service-key")
    network_calls: list[str] = []

    class UnexpectedClient:
        def get(self, *_args, **_kwargs):
            network_calls.append("called")
            raise AssertionError("a fenced collector must not contact the provider")

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", UnexpectedClient)

    def counts() -> tuple[int, int, int, int, int]:
        return tuple(
            int(first.db.scalar(f"select count(*) from {table}"))
            for table in (
                "raw_artifact",
                "staging_license_snapshot",
                "source_status",
                "collection_checkpoint",
                "fact_transport_flow",
            )
        )

    before = counts()
    with pytest.raises(RuntimeError, match="lease ownership"):
        first._collect_fixture_source(
            first_run, "lodgings", date(2026, 8, 16), logger
        )
    assert counts() == before

    with pytest.raises(RuntimeError, match="lease ownership"):
        first._collect_accommodation(
            first_run, "lodgings", date(2026, 8, 16), logger
        )
    assert network_calls == []
    assert counts() == before

    with pytest.raises(RuntimeError, match="lease ownership"):
        load_transport(
            first.db,
            SourceRegistry((first.registry.get("srt_station_boarding_file"),)),
            date(2026, 7, 1),
            date(2026, 7, 31),
            first_run,
            progress=lambda: first._refresh_lease(first_run.run_id),
        )
    assert counts() == before

    with pytest.raises(RuntimeError, match="lease ownership"):
        first._record_failure(first_run, "lodgings", ValueError("late"), logger)
    assert counts() == before


def test_transport_long_file_loop_heartbeats_keep_attempt_owned(
    tmp_path: Path,
) -> None:
    """Catches a long file normalization loop that lets its run lease expire."""
    first = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first.db.migrate()
    first_run, _ = first._prepare_run(
        "production", "backfill", date(2026, 3, 31), "transport-heartbeat"
    )
    assert first_run is not None
    inbox = first.db.path.parent / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "SRT_역_heartbeat.csv").write_text(
        "승차역,2026년1월,2026년2월,2026년3월\n부산역,10,20,30\n",
        encoding="utf-8",
    )
    registry = SourceRegistry(
        (first.registry.get("srt_station_boarding_file"),)
    )
    second = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    second.db.migrate()
    heartbeats = 0
    takeover_checked = False

    def heartbeat() -> None:
        nonlocal heartbeats, takeover_checked
        heartbeats += 1
        if heartbeats in {4, 8}:
            first.db.connection.execute(
                """update pipeline_run
                   set lease_expires_at = now() - interval '1 second'
                   where run_id = ?""",
                [first_run.run_id],
            )
        first._refresh_lease(first_run.run_id)
        if heartbeats == 8:
            with pytest.raises(RuntimeError, match="active lease"):
                second._prepare_run(
                    "production",
                    "backfill",
                    date(2026, 3, 31),
                    "transport-heartbeat",
                )
            takeover_checked = True

    result = load_transport(
        first.db,
        registry,
        date(2026, 1, 1),
        date(2026, 3, 31),
        first_run,
        progress=heartbeat,
    )

    assert result.records_loaded == 3
    assert heartbeats >= 8
    assert takeover_checked is True
    assert first.db.scalar("select count(*) from fact_transport_flow") == 3


def test_production_passes_lease_progress_to_every_loader_family(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches an orchestration branch invoking a family loader without fencing writes."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    specs = (
        SourceSpec(
            "building_register_title",
            "https://example.test/building",
            group="building",
        ),
        SourceSpec(
            "tourism_data_lab",
            "https://example.test/tourism",
            group="tourism",
            cadence="monthly",
        ),
        SourceSpec(
            "srt_station_boarding_file",
            "https://example.test/transport",
            source_type="file",
            group="transport",
            cadence="monthly",
        ),
    )
    pipeline.registry = SourceRegistry(specs)
    calls: list[str] = []

    def ready_probe(spec, client, db):
        status = SourceStatus(spec.source_id, datetime.now(UTC), "READY", {})
        db.record_source_status(status)
        return status

    def building_loader(db, registry, run, *, raw_store, progress):
        progress()
        calls.append("building")
        return SimpleNamespace(building_rows=0)

    def tourism_loader(db, registry, start, end, run, *, progress):
        progress()
        calls.append("tourism")
        return SimpleNamespace(records_loaded=0)

    def transport_loader(db, registry, start, end, run, *, progress):
        progress()
        calls.append("transport")
        source_id = registry.ids()[0]
        return SimpleNamespace(
            records_loaded=0,
            source_months=(
                SourceMonthEvidence(source_id, start.strftime("%Y-%m"), 0, True),
            ),
        )

    monkeypatch.setattr(orchestrator_module, "probe_source", ready_probe)
    monkeypatch.setattr(
        orchestrator_module, "collect_buildings_for_licenses", building_loader
    )
    monkeypatch.setattr(orchestrator_module, "load_tourism_demand", tourism_loader)
    monkeypatch.setattr(orchestrator_module, "load_transport", transport_loader)
    monkeypatch.setattr(
        pipeline, "_finish_run", lambda run, total_rows, logger: "finished"
    )

    result = pipeline._execute_production(
        "daily", date(2026, 8, 16), date(2026, 8, 16), list(pipeline.registry.ids())
    )

    assert result == "finished"
    assert calls == ["building", "tourism", "transport"]


def test_fixture_backfill_defaults_to_the_complete_required_fixture_set(
    tmp_path: Path,
) -> None:
    """Catches an offline run accidentally selecting unfixtureable live sources."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))

    summary = pipeline.backfill(date(2022, 1, 1), date(2026, 8, 16))

    assert summary.published is True
    assert summary.raw_artifacts == 6


def test_later_source_failure_preserves_an_earlier_success(tmp_path: Path) -> None:
    """Catches one malformed source aborting or rolling back prior durable rows."""
    fixtures = tmp_path / "fixtures" / "accommodation"
    fixtures.mkdir(parents=True)
    fixtures.joinpath("lodgings.json").write_text(
        '[{"MNG_NO":"kept","BPLC_NM":"보존호텔",'
        '"ROAD_NM_ADDR":"부산광역시 사하구 낙동대로 1"}]',
        encoding="utf-8",
    )
    fixtures.joinpath("tourist_accommodations.json").write_text(
        '{"not":"a row list"}', encoding="utf-8"
    )
    pipeline = Pipeline.for_fixtures(tmp_path / "runtime", fixtures.parent)

    summary = pipeline.backfill(
        date(2026, 8, 16),
        date(2026, 8, 16),
        source_ids=["lodgings", "tourist_accommodations"],
    )

    assert summary.published is False
    assert pipeline.db.scalar(
        "select count(*) from staging_license_snapshot where source_record_id = 'kept'"
    ) == 1
    assert pipeline.db.scalar(
        """select status from source_status
           where source_id = 'tourist_accommodations' order by checked_at desc limit 1"""
    ) == "SCHEMA_CHANGED"


def test_family_ready_result_does_not_fabricate_monthly_checkpoints(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches READY being expanded into unsupported source-month completion."""
    cases = (
        (
            SourceSpec(
                "tourism_data_lab",
                "https://example.test/tourism",
                operation="reviewed",
                group="tourism",
                cadence="monthly",
            ),
            "load_tourism_demand",
        ),
        (
            SourceSpec(
                "srt_station_boarding_file",
                "file://example/srt",
                group="transport",
                cadence="monthly",
                source_type="file",
            ),
            "load_transport",
        ),
    )
    for index, (spec, loader_name) in enumerate(cases):
        pipeline = Pipeline.for_fixtures(tmp_path / str(index), Path("tests/fixtures"))
        pipeline.fixture_dir = None
        pipeline.registry = SourceRegistry((spec,))
        outcome = SimpleNamespace(
            records_loaded=0, sources_ready=(spec.source_id,), source_months=()
        )
        monkeypatch.setattr(
            orchestrator_module,
            loader_name,
            lambda *args, _outcome=outcome, **kwargs: _outcome,
        )

        summary = pipeline.backfill(date(2026, 1, 1), date(2026, 3, 31))

        assert summary.published is False
        assert pipeline.db.scalar(
            "select count(*) from collection_checkpoint where source_id = ?",
            [spec.source_id],
        ) == 0


def test_daily_tourism_requests_the_previous_complete_month(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches daily passing as_of as a start later than the capped month end."""
    spec = SourceSpec(
        "tourism_data_lab",
        "https://example.test/tourism",
        operation="reviewed",
        group="tourism",
        cadence="monthly",
    )
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.registry = SourceRegistry((spec,))
    observed: dict[str, date] = {}

    def fake_load(db, registry, start, end, run, *, progress):
        progress()
        observed.update(start=start, end=end)
        return SimpleNamespace(records_loaded=0, sources_ready=())

    monkeypatch.setattr(orchestrator_module, "load_tourism_demand", fake_load)

    summary = pipeline.daily(date(2026, 8, 16))

    assert summary.published is False
    assert observed == {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}


def test_daily_transport_requests_and_checkpoints_previous_complete_month(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches daily transport using as_of or a snapshot checkpoint instead of July."""
    spec = SourceSpec(
        "srt_station_boarding_file",
        "file://example/srt",
        group="transport",
        cadence="monthly",
        source_type="file",
    )
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.registry = SourceRegistry((spec,))
    observed: dict[str, date] = {}

    def fake_load(db, registry, start, end, run, *, progress):
        progress()
        observed.update(start=start, end=end)
        return SimpleNamespace(
            records_loaded=1,
            sources_ready=(spec.source_id,),
            source_months=(SourceMonthEvidence(spec.source_id, "2026-07", 1),),
        )

    monkeypatch.setattr(orchestrator_module, "load_transport", fake_load)

    summary = pipeline.daily(date(2026, 8, 16))

    assert summary.published is False
    assert observed == {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}
    checkpoint = json.loads(
        pipeline.db.scalar(
            """select checkpoint_json from collection_checkpoint
               where source_id = ? and partition_key = '2026-07'""",
            [spec.source_id],
        )
    )
    assert checkpoint["status"] == "completed"
    assert checkpoint["evidence"]["record_count"] == 1


def test_transport_restart_skips_only_evidence_backed_months(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches requested months being blanket-completed or recollected on restart."""
    spec = SourceSpec(
        "srt_station_boarding_file",
        "file://example/srt",
        group="transport",
        cadence="monthly",
        source_type="file",
    )
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.registry = SourceRegistry((spec,))
    calls: list[tuple[date, date]] = []

    def fake_load(db, registry, start, end, run, *, progress):
        progress()
        calls.append((start, end))
        evidence = (
            (SourceMonthEvidence(spec.source_id, "2026-01", 1),)
            if len(calls) == 1
            else (
                SourceMonthEvidence(spec.source_id, "2026-02", 1),
                SourceMonthEvidence(spec.source_id, "2026-03", 1),
            )
        )
        return SimpleNamespace(
            records_loaded=len(evidence),
            sources_ready=(spec.source_id,),
            source_months=evidence,
        )

    persist_summary = pipeline._persist_summary
    monkeypatch.setattr(orchestrator_module, "load_transport", fake_load)
    monkeypatch.setattr(
        pipeline,
        "_persist_summary",
        lambda summary: (_ for _ in ()).throw(RuntimeError("restart injection")),
    )

    with pytest.raises(RuntimeError, match="restart injection"):
        pipeline.backfill(date(2026, 1, 15), date(2026, 3, 2))

    assert pipeline.db.query(
        """select partition_key from collection_checkpoint
           where source_id = ? order by partition_key""",
        [spec.source_id],
    ) == [("2026-01",)]
    monkeypatch.setattr(pipeline, "_persist_summary", persist_summary)

    retried = pipeline.backfill(date(2026, 1, 15), date(2026, 3, 2))

    assert retried.status == "BLOCKED"
    assert calls == [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 2, 1), date(2026, 3, 31)),
    ]
    assert pipeline.db.query(
        """select partition_key from collection_checkpoint
           where source_id = ? order by partition_key""",
        [spec.source_id],
    ) == [("2026-01",), ("2026-02",), ("2026-03",)]


def test_duplicate_export_is_frozen_to_current_publication(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a blocked successor leaking mutable duplicate review into export."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    published = pipeline.daily(date(2026, 8, 16))
    bogus_review = uuid4()
    real_builder = orchestrator_module.build_facilities

    def build_then_mutate(db, run_id):
        result = real_builder(db, run_id)
        db.connection.execute(
            "insert into duplicate_review (review_id, evidence_json) values (?, ?)",
            [bogus_review, '{"source":"blocked-successor"}'],
        )
        return result

    monkeypatch.setattr(orchestrator_module, "build_facilities", build_then_mutate)
    blocked = pipeline.backfill(
        date(2026, 8, 17), date(2026, 8, 17), source_ids=["lodgings"]
    )

    paths = export_current(pipeline.db, pipeline.settings.data_dir, date(2026, 8, 17))
    duplicate_csv = next(path for path in paths if path.name == "duplicate_review.csv")

    assert published.published is True
    assert blocked.published is False
    assert str(bogus_review) not in duplicate_csv.read_text(encoding="utf-8")


def test_republishing_current_run_cannot_append_to_its_duplicate_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches same-run publication idempotency extending a frozen snapshot."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    real_publish = orchestrator_module.publish_if_valid
    captured_reports = []

    def capture_report(db, run_id, report, **kwargs):
        captured_reports.append(report)
        return real_publish(db, run_id, report, **kwargs)

    monkeypatch.setattr(orchestrator_module, "publish_if_valid", capture_report)
    published = pipeline.daily(date(2026, 8, 16))
    bogus_review = uuid4()
    pipeline.db.connection.execute(
        "insert into duplicate_review (review_id, evidence_json) values (?, ?)",
        [bogus_review, '{"source":"post-publication"}'],
    )

    result = real_publish(pipeline.db, published.run_id, captured_reports[0])

    assert result.published is True
    assert pipeline.db.scalar(
        """select count(*) from publication_duplicate_review_snapshot
           where run_id = ? and review_id = ?""",
        [published.run_id, bogus_review],
    ) == 0


def test_publication_and_terminal_summary_roll_back_together_on_summary_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a committed pointer whose pipeline status or summary is incomplete."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    persist_summary = pipeline._persist_summary

    def fail_summary(summary):
        raise RuntimeError("injected summary failure")

    monkeypatch.setattr(pipeline, "_persist_summary", fail_summary)

    with pytest.raises(RuntimeError, match="injected summary failure"):
        pipeline.daily(date(2026, 8, 16))

    run_id = pipeline.db.scalar("select run_id from pipeline_run")
    assert pipeline.db.scalar(
        "select count(*) from publication_state where is_current"
    ) == 0
    assert pipeline.db.scalar(
        "select count(*) from publication_duplicate_review_snapshot"
    ) == 0
    assert pipeline.db.scalar(
        "select status from pipeline_run where run_id = ?", [run_id]
    ) == "RUNNING"
    assert pipeline.db.scalar(
        "select count(*) from pipeline_run_summary where run_id = ?", [run_id]
    ) == 0

    monkeypatch.setattr(pipeline, "_persist_summary", persist_summary)
    retried = pipeline.daily(date(2026, 8, 16))

    assert retried.run_id == run_id
    assert retried.published is True
    assert pipeline.db.scalar(
        "select published_run_id from publication_state where is_current"
    ) == run_id


def test_blocked_status_and_summary_roll_back_together_on_summary_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a blocked terminal status committed without its immutable summary."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    persist_summary = pipeline._persist_summary
    monkeypatch.setattr(
        pipeline,
        "_persist_summary",
        lambda summary: (_ for _ in ()).throw(RuntimeError("blocked summary failure")),
    )

    with pytest.raises(RuntimeError, match="blocked summary failure"):
        pipeline.backfill(
            date(2026, 8, 16),
            date(2026, 8, 16),
            source_ids=["lodgings"],
        )

    run_id = pipeline.db.scalar("select run_id from pipeline_run")
    assert pipeline.db.scalar(
        "select status from pipeline_run where run_id = ?", [run_id]
    ) == "RUNNING"
    assert pipeline.db.scalar(
        "select count(*) from pipeline_run_summary where run_id = ?", [run_id]
    ) == 0

    monkeypatch.setattr(pipeline, "_persist_summary", persist_summary)
    retried = pipeline.backfill(
        date(2026, 8, 16), date(2026, 8, 16), source_ids=["lodgings"]
    )

    assert retried.run_id == run_id
    assert retried.status == "BLOCKED"
    assert pipeline.db.scalar(
        "select count(*) from pipeline_run_summary where run_id = ?", [run_id]
    ) == 1


def test_current_pointer_with_running_status_is_recovered_without_resuming(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a legacy published RUNNING row being reopened and recollected."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    published = pipeline.daily(date(2026, 8, 16))
    pipeline.db.connection.execute(
        "delete from pipeline_run_summary where run_id = ?", [published.run_id]
    )
    pipeline.db.connection.execute(
        "update pipeline_run set status = 'RUNNING', finished_at = null where run_id = ?",
        [published.run_id],
    )

    def fail_if_collected(*args, **kwargs):
        raise AssertionError("a published RUNNING row was resumed")

    monkeypatch.setattr(pipeline, "_collect_fixture_source", fail_if_collected)

    recovered = pipeline.daily(date(2026, 8, 16))

    assert recovered.run_id == published.run_id
    assert recovered.published is True
    assert pipeline.db.scalar(
        "select status from pipeline_run where run_id = ?", [published.run_id]
    ) in {"PUBLISHED", "PUBLISHED_WITH_WARNINGS"}
    assert pipeline.db.scalar(
        "select count(*) from pipeline_run_summary where run_id = ?", [published.run_id]
    ) == 1


def test_family_loader_failures_finalize_a_run_scoped_blocked_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches family exceptions escaping with a permanently RUNNING attempt."""
    cases = (
        (
            SourceSpec(
                "building_register_title",
                "https://example.test/building",
                operation="title",
                group="building",
                cadence="monthly",
            ),
            "collect_buildings_for_licenses",
        ),
        (
            SourceSpec(
                "tourism_data_lab",
                "https://example.test/tourism",
                operation="reviewed",
                group="tourism",
                cadence="monthly",
            ),
            "load_tourism_demand",
        ),
        (
            SourceSpec(
                "srt_station_boarding_file",
                "file://example/srt",
                group="transport",
                cadence="monthly",
                source_type="file",
            ),
            "load_transport",
        ),
    )

    def fail_loader(*args, **kwargs):
        raise RuntimeError("family collector crashed")

    for index, (spec, loader_name) in enumerate(cases):
        pipeline = Pipeline.for_fixtures(tmp_path / str(index), Path("tests/fixtures"))
        pipeline.fixture_dir = None
        pipeline.registry = SourceRegistry((spec,))
        monkeypatch.setattr(orchestrator_module, loader_name, fail_loader)

        summary = pipeline.daily(date(2026, 8, 16))

        assert summary.status == "BLOCKED"
        assert summary.published is False
        assert pipeline.db.scalar(
            "select status from pipeline_run where run_id = ?", [summary.run_id]
        ) == "BLOCKED"
        assert pipeline.db.scalar(
            """select status from source_status
               where run_id = ? and source_id = ?
               order by checked_at desc limit 1""",
            [summary.run_id, spec.source_id],
        ) == "HTTP_FAILED"


def test_optional_family_crash_is_a_required_failure_and_preserves_lkg(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches an optional tourism contract allowing an orchestration crash to publish."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    prior = pipeline.daily(date(2026, 8, 15))
    fixture_dir = pipeline.fixture_dir
    pipeline.fixture_dir = None
    accommodation = tuple(
        pipeline.registry.get(source_id)
        for source_id in pipeline.registry.ids(group="accommodation")
    )
    tourism = SourceSpec(
        "tourism_data_lab",
        "https://example.test/tourism",
        operation="reviewed",
        group="tourism",
        cadence="monthly",
    )
    pipeline.registry = SourceRegistry((*accommodation, tourism))

    def fixture_collect(run, source_id, as_of, logger):
        pipeline.fixture_dir = fixture_dir
        try:
            return pipeline._collect_fixture_source(run, source_id, as_of, logger)
        finally:
            pipeline.fixture_dir = None

    def ready_probe(spec, client, db):
        status = SourceStatus(
            spec.source_id,
            datetime.now(UTC),
            "READY",
            {"operation": spec.operation or "reviewed"},
        )
        db.record_source_status(status)
        return status

    monkeypatch.setattr(pipeline, "_collect_accommodation", fixture_collect)
    monkeypatch.setattr(orchestrator_module, "probe_source", ready_probe)
    monkeypatch.setattr(
        orchestrator_module,
        "load_tourism_demand",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tourism crashed")),
    )

    failed = pipeline.daily(date(2026, 8, 16))

    assert failed.status == "BLOCKED"
    assert failed.published is False
    assert pipeline.db.scalar(
        "select published_run_id from publication_state where is_current"
    ) == prior.run_id
    assert pipeline.db.scalar(
        """select count(*) from fact_data_quality
           where run_id = ? and source_id = 'orchestration:tourism'
             and status = 'failed' and severity = 'required'""",
        [failed.run_id],
    ) >= 1
    assert pipeline.db.scalar(
        """select status from source_status
           where run_id = ? and source_id = 'tourism_data_lab'
           order by checked_at desc limit 1""",
        [failed.run_id],
    ) == "HTTP_FAILED"


def test_log_redaction_covers_nested_credential_names() -> None:
    """Catches nested aliases leaking secrets into JSON summaries or logs."""
    payload = {
        "service_key": "one",
        "nested": {
            "ODCLOUD_API_KEY": "two",
            "Authorization": "three",
            "password": "four",
        },
        "safe": "visible",
    }

    assert redact_for_log(payload) == {
        "service_key": "[REDACTED]",
        "nested": {
            "ODCLOUD_API_KEY": "[REDACTED]",
            "Authorization": "[REDACTED]",
            "password": "[REDACTED]",
        },
        "safe": "visible",
    }


def test_export_writes_four_current_datasets_as_csv_and_parquet(tmp_path: Path) -> None:
    """Catches missing review/quality exports or exporting non-current mart runs."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    summary = pipeline.daily(date(2026, 8, 16))

    paths = export_current(pipeline.db, pipeline.settings.data_dir, date(2026, 8, 16))

    assert summary.published is True
    assert {path.name for path in paths} == {
        "facility_current.csv",
        "facility_current.parquet",
        "region_month.csv",
        "region_month.parquet",
        "data_quality.csv",
        "data_quality.parquet",
        "duplicate_review.csv",
        "duplicate_review.parquet",
    }
    assert all(path.exists() for path in paths)
