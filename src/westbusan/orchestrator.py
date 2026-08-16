"""Runnable orchestration for collection, validation, marts, and publication."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import duckdb
from pyarrow import csv as arrow_csv
from pyarrow import parquet

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.analytics.build import build_marts
from westbusan.buildings.load import collect_buildings_for_licenses
from westbusan.config import PolicyConfig, RegionConfig, Settings
from westbusan.db import Database
from westbusan.demand.load import load_tourism_demand
from westbusan.entity_resolution.match import build_facilities
from westbusan.http import (
    AuthenticationError,
    HttpStatusError,
    QuotaError,
    SafeHttpClient,
    SchemaError,
)
from westbusan.models import RunContext, SourceSpec, SourceStatus
from westbusan.quality.checks import approve_schema_baseline, run_quality_suite
from westbusan.quality.publish import current_published_run, publish_if_valid
from westbusan.sources.datagokr import parse_data_page
from westbusan.sources.registry import SourceRegistry, probe_source
from westbusan.storage import RawStore
from westbusan.transport.load import load_transport

_LEASE_DURATION = timedelta(minutes=15)
_FIXTURE_SOURCES = (
    "lodgings",
    "tourist_accommodations",
    "foreigner_city_homestays",
    "rural_homestays",
    "hanok_experience",
    "tourist_pensions",
)
_OPERATIONAL_ERRORS = (
    AuthenticationError,
    HttpStatusError,
    KeyError,
    OSError,
    QuotaError,
    SchemaError,
    TypeError,
    ValueError,
)
_ACCOMMODATION_JURISDICTION_PARAMETER = "cond[OPN_ATMY_GRP_CD::EQ]"
_BUSAN_JURISDICTION_CODE = "6260000"


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Credential-free result returned by every state-changing pipeline run."""

    run_id: UUID
    mode: str
    status: str
    published: bool
    raw_artifacts: int
    row_count: int
    warning_count: int
    failed_required_checks: int
    started_at: datetime
    finished_at: datetime

    def as_dict(self) -> dict[str, object]:
        return _redact(asdict(self))  # type: ignore[return-value]


class Pipeline:
    """Coordinate durable source evidence through fail-closed publication."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        *,
        fixture_dir: Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.settings = settings
        self.fixture_dir = Path(fixture_dir).resolve() if fixture_dir else None
        self.db = Database(settings.db_path, self.root / "sql")
        self.registry = SourceRegistry.load(self.root / "config" / "sources.yaml")
        self.raw_store = RawStore(settings.data_dir)
        self._lease_owner_token = uuid4()

    @classmethod
    def for_fixtures(cls, data_root: Path, fixture_dir: Path) -> Pipeline:
        """Construct an offline pipeline over immutable repository fixtures."""
        root = Path.cwd().resolve()
        data_dir = Path(data_root).resolve() / "data"
        settings = Settings(
            service_key="",
            data_dir=data_dir,
            db_path=data_dir / "westbusan.duckdb",
            log_dir=Path(data_root).resolve() / "logs",
            regions=RegionConfig(
                west=["강서구", "북구", "사상구", "사하구"],
                east=["해운대구", "수영구", "기장군"],
                other=[
                    "중구",
                    "서구",
                    "동구",
                    "영도구",
                    "부산진구",
                    "동래구",
                    "남구",
                    "금정구",
                    "연제구",
                ],
            ),
            policy=PolicyConfig(
                small_room_threshold=20,
                old_building_years=[20, 30],
            ),
        )
        return cls(root, settings, fixture_dir=fixture_dir)

    @classmethod
    def from_root(cls, root: Path) -> Pipeline:
        """Build a production pipeline using environment-backed local settings."""
        resolved = Path(root).resolve()
        return cls(resolved, Settings.load(resolved))

    def probe(self, source_ids: list[str] | None = None) -> list[SourceStatus]:
        """Probe selected registered sources without exposing configured credentials."""
        self.db.migrate()
        selected = self._selected_ids(source_ids)
        client = SafeHttpClient()
        statuses: list[SourceStatus] = []
        for source_id in selected:
            status = probe_source(self.registry.get(source_id), client, self.db)
            statuses.append(status)
        return statuses

    def daily(self, as_of: date) -> RunSummary:
        """Run the ordered daily workflow for one Asia/Seoul business date."""
        if self.fixture_dir is None:
            return self._execute_production("daily", as_of, as_of, None)
        return self._execute_fixtures("daily", as_of, as_of, None)

    def backfill(
        self,
        start: date,
        end: date,
        source_ids: list[str] | None = None,
    ) -> RunSummary:
        """Collect an inclusive date range while retaining completed partitions."""
        if start > end:
            raise ValueError("backfill start must be on or before end")
        if self.fixture_dir is not None:
            return self._execute_fixtures("backfill", start, end, source_ids)
        selected = self._selected_ids(source_ids)
        return self._execute_production("backfill", start, end, list(selected))

    def _execute_fixtures(
        self,
        mode: Literal["daily", "backfill"],
        start: date,
        end: date,
        source_ids: list[str] | None,
    ) -> RunSummary:
        selected = tuple(source_ids) if source_ids is not None else _FIXTURE_SOURCES
        unknown = sorted(set(selected) - set(_FIXTURE_SOURCES))
        if unknown:
            raise ValueError(f"no fixture collector for: {', '.join(unknown)}")
        identity = f"{mode}:{start.isoformat()}:{end.isoformat()}:{','.join(selected)}"
        self.db.migrate()
        run, persisted = self._prepare_run("fixture", mode, end, identity)
        if persisted is not None:
            return persisted
        assert run is not None
        logger = _JsonlLogger(self.settings.log_dir, end)
        total_rows = 0
        for source_id in selected:
            partition = f"snapshot:{end.isoformat()}"
            try:
                source_rows = self._collect_fixture_source(
                    run, source_id, end, logger
                )
                total_rows += source_rows
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
                self._record_failure(run, source_id, error, logger)
                self._checkpoint(source_id, partition, "failed", 1, run.run_id)
                continue
            self._complete_source_partition(
                run,
                source_id,
                partition,
                2,
                "READY" if source_rows else "EMPTY",
                {"row_count": source_rows, "fixture": True},
            )
        return self._finish_run(run, total_rows, logger)

    def _execute_production(
        self,
        mode: Literal["daily", "backfill"],
        start: date,
        end: date,
        source_ids: list[str] | None,
    ) -> RunSummary:
        selected = self._selected_ids(source_ids)
        identity = f"{mode}:{start.isoformat()}:{end.isoformat()}:{','.join(selected)}"
        self.db.migrate()
        run, persisted = self._prepare_run("production", mode, end, identity)
        if persisted is not None:
            return persisted
        assert run is not None
        logger = _JsonlLogger(self.settings.log_dir, end)
        total_rows = 0
        client = SafeHttpClient()

        for source_id in selected:
            self._refresh_lease(run.run_id)
            spec = self.registry.get(source_id)
            if spec.source_type != "api":
                continue
            try:
                status = probe_source(spec, client, self.db)
                self._refresh_lease(run.run_id)
                self.db.connection.execute(
                    "update source_status set run_id = ? where source_id = ? and checked_at = ?",
                    [run.run_id, source_id, status.checked_at],
                )
            except _OPERATIONAL_ERRORS as error:
                self._record_failure(run, source_id, error, logger)

        for source_id in selected:
            spec = self.registry.get(source_id)
            if spec.group != "accommodation":
                continue
            try:
                total_rows += self._collect_accommodation(run, spec.source_id, end, logger)
            except _OPERATIONAL_ERRORS as error:
                self._record_failure(run, source_id, error, logger)

        selected_registry = SourceRegistry(
            tuple(self.registry.get(source_id) for source_id in selected)
        )
        if any(self.registry.get(item).group == "building" for item in selected):
            building_registry = SourceRegistry(
                tuple(
                    self.registry.get(source_id)
                    for source_id in self.registry.ids(group="building")
                )
            )
            try:
                result = collect_buildings_for_licenses(
                    self.db,
                    building_registry,
                    run,
                    raw_store=self.raw_store,
                    progress=lambda: self._refresh_lease(run.run_id),
                )
            except Exception as error:  # noqa: BLE001 - terminal family boundary
                for source_id in selected:
                    if self.registry.get(source_id).group == "building":
                        self._record_failure(run, source_id, error, logger)
                self._record_orchestration_failure(run, "building", error, logger)
            else:
                total_rows += result.building_rows
        if any(self.registry.get(item).group == "tourism" for item in selected):
            tourism_start, tourism_end = start, end
            if mode == "daily":
                tourism_end = date(end.year, end.month, 1) - timedelta(days=1)
                tourism_start = date(tourism_end.year, tourism_end.month, 1)
            try:
                result = load_tourism_demand(
                    self.db,
                    selected_registry,
                    tourism_start,
                    tourism_end,
                    run,
                    progress=lambda: self._refresh_lease(run.run_id),
                    **self._supported_storage_kwargs(load_tourism_demand),
                )
            except Exception as error:  # noqa: BLE001 - terminal family boundary
                for source_id in selected:
                    if self.registry.get(source_id).group == "tourism":
                        self._record_failure(run, source_id, error, logger)
                self._record_orchestration_failure(run, "tourism", error, logger)
            else:
                total_rows += result.records_loaded
        if any(self.registry.get(item).group == "transport" for item in selected):
            try:
                transport_start, transport_end = start, end
                if mode == "daily":
                    transport_end = date(end.year, end.month, 1) - timedelta(days=1)
                    transport_start = date(
                        transport_end.year, transport_end.month, 1
                    )
                for source_id in selected:
                    spec = self.registry.get(source_id)
                    if spec.group != "transport":
                        continue
                    partitions = iter_source_partitions(
                        spec, transport_start, transport_end
                    )
                    pending = tuple(
                        partition
                        for partition in partitions
                        if not self._transport_checkpoint_complete(
                            source_id, partition, run.run_id
                        )
                    )
                    source_registry = SourceRegistry((spec,))
                    for range_start, range_end in _month_partition_ranges(pending):
                        self._refresh_lease(run.run_id)
                        result = load_transport(
                            self.db,
                            source_registry,
                            range_start,
                            range_end,
                            run,
                            progress=lambda: self._refresh_lease(run.run_id),
                            **self._supported_storage_kwargs(load_transport),
                        )
                        total_rows += result.records_loaded
                        for evidence in result.source_months:
                            if (
                                evidence.source_id != source_id
                                or evidence.month not in pending
                                or not (
                                    evidence.record_count > 0
                                    or evidence.explicit_empty
                                )
                            ):
                                continue
                            self._checkpoint(
                                source_id,
                                evidence.month,
                                "completed",
                                2,
                                run.run_id,
                                evidence={
                                    "record_count": evidence.record_count,
                                    "explicit_empty": evidence.explicit_empty,
                                },
                            )
            except Exception as error:  # noqa: BLE001 - terminal family boundary
                for source_id in selected:
                    if self.registry.get(source_id).group == "transport":
                        self._record_failure(run, source_id, error, logger)
                self._record_orchestration_failure(run, "transport", error, logger)
        return self._finish_run(run, total_rows, logger)

    def _finish_run(
        self, run: RunContext, total_rows: int, logger: _JsonlLogger
    ) -> RunSummary:
        self._refresh_lease(run.run_id)
        build_facilities(
            self.db,
            run.run_id,
            **self._supported_phase_kwargs(build_facilities, run.run_id),
        )
        report = run_quality_suite(
            self.db,
            run.run_id,
            **self._supported_phase_kwargs(run_quality_suite, run.run_id),
        )
        failed = sum(
            check.status == "failed" and check.severity == "required"
            for check in report.checks
        )
        warnings = sum(check.status == "warning" for check in report.checks)
        if not failed:
            build_marts(
                self.db,
                run.run_id,
                self.settings.policy,
                **self._supported_phase_kwargs(build_marts, run.run_id),
            )
        finished = datetime.now(UTC)
        raw_artifacts = int(
            self.db.scalar(
                "select count(*) from raw_artifact where run_id = ?", [run.run_id]
            )
        )
        published_summary = RunSummary(
            run.run_id,
            run.mode,
            "PUBLISHED_WITH_WARNINGS" if warnings else "PUBLISHED",
            True,
            raw_artifacts,
            total_rows,
            warnings,
            failed,
            run.started_at,
            finished,
        )
        publication = publish_if_valid(
            self.db,
            run.run_id,
            report,
            finalize=lambda: self._write_terminal_summary(published_summary),
            **self._supported_phase_kwargs(publish_if_valid, run.run_id),
        )
        if publication.published:
            summary = published_summary
        else:
            summary = RunSummary(
                run.run_id,
                run.mode,
                "BLOCKED",
                False,
                raw_artifacts,
                total_rows,
                warnings,
                failed,
                run.started_at,
                finished,
            )
            self._commit_terminal_summary(summary)
        logger.write("run_complete", **summary.as_dict())
        return summary

    def _selected_ids(self, source_ids: list[str] | None) -> tuple[str, ...]:
        selected = tuple(source_ids) if source_ids is not None else self.registry.ids()
        for source_id in selected:
            self.registry.get(source_id)
        return selected

    def _supported_storage_kwargs(self, loader: object) -> dict[str, object]:
        """Inject configured storage without constraining narrow test adapters."""
        parameters = inspect.signature(loader).parameters
        values: dict[str, object] = {
            "raw_store": self.raw_store,
            "inbox_dir": self.settings.data_dir / "inbox",
        }
        return {name: value for name, value in values.items() if name in parameters}

    def _supported_phase_kwargs(
        self, phase: object, run_id: UUID
    ) -> dict[str, object]:
        parameters = inspect.signature(phase).parameters
        values: dict[str, object] = {
            "progress": lambda: self._refresh_lease(run_id),
            "fence_check": lambda: self._assert_fence(run_id),
        }
        return {name: value for name, value in values.items() if name in parameters}

    def _prepare_run(
        self,
        scope: str,
        mode: Literal["daily", "backfill"],
        as_of: date,
        identity: str,
    ) -> tuple[RunContext | None, RunSummary | None]:
        logical_key = str(
            uuid5(
                NAMESPACE_URL,
                f"westbusan:{scope}:{self.db.path.resolve()}:{identity}",
            )
        )
        for transaction_attempt in range(3):
            began = False
            try:
                now = datetime.now(UTC)
                lease_expires = now + _LEASE_DURATION
                self.db.connection.execute("begin transaction")
                began = True
                rows = self.db.query(
                    """select run_id, status, attempt, started_at::varchar, business_date
                       from pipeline_run where logical_run_key = ?
                       order by attempt desc limit 1""",
                    [logical_key],
                )
                if rows:
                    (
                        prior_run_id,
                        prior_status,
                        prior_attempt,
                        prior_started_at,
                        prior_business_date,
                    ) = rows[0]
                    started_at = datetime.fromisoformat(str(prior_started_at))
                    if current_published_run(self.db) == prior_run_id:
                        self.db.connection.execute("commit")
                        began = False
                        return None, self._recover_current_publication(
                            prior_run_id, mode, started_at
                        )
                    if str(prior_status) in {
                        "PUBLISHED",
                        "PUBLISHED_WITH_WARNINGS",
                    }:
                        self.db.connection.execute("commit")
                        began = False
                        return None, self._load_summary(prior_run_id)
                    if str(prior_status) == "RUNNING":
                        acquired = self.db.query(
                            """update pipeline_run
                               set lease_owner_token = ?, heartbeat_at = ?,
                                   lease_expires_at = ?
                               where run_id = ? and status = 'RUNNING'
                                 and (
                                   lease_owner_token is null
                                   or lease_owner_token = ?
                                   or lease_expires_at is null
                                   or lease_expires_at <= ?
                                 )
                               returning run_id""",
                            [
                                self._lease_owner_token,
                                now,
                                lease_expires,
                                prior_run_id,
                                self._lease_owner_token,
                                now,
                            ],
                        )
                        if not acquired:
                            self.db.connection.execute("rollback")
                            began = False
                            raise RuntimeError(
                                f"pipeline run {prior_run_id} has an active lease"
                            )
                        writer_epoch = self._acquire_global_writer(
                            prior_run_id, now, lease_expires
                        )
                        self.db.connection.execute(
                            "update pipeline_run set writer_fence_epoch = ? where run_id = ?",
                            [writer_epoch, prior_run_id],
                        )
                        self.db.connection.execute("commit")
                        began = False
                        return (
                            RunContext(
                                prior_run_id,
                                mode,
                                started_at,
                                "RUNNING",
                                prior_business_date,
                            ),
                            None,
                        )
                    attempt = int(prior_attempt) + 1
                else:
                    attempt = 1
                started = now
                run = RunContext(
                    uuid5(NAMESPACE_URL, f"{logical_key}:attempt:{attempt}"),
                    mode,
                    started,
                    "RUNNING",
                    as_of,
                )
                inherited_run_id = current_published_run(self.db)
                writer_epoch = self._acquire_global_writer(
                    run.run_id, now, lease_expires
                )
                inserted = self.db.query(
                    """insert into pipeline_run (
                           run_id, mode, started_at, status, logical_run_key, attempt,
                           lease_owner_token, lease_expires_at, heartbeat_at, business_date,
                           writer_fence_epoch
                       ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       on conflict (run_id) do nothing
                       returning run_id""",
                    [
                        run.run_id,
                        run.mode,
                        run.started_at,
                        run.status,
                        logical_key,
                        attempt,
                        self._lease_owner_token,
                        lease_expires,
                        now,
                        as_of,
                        writer_epoch,
                    ],
                )
                if not inserted:
                    raise duckdb.TransactionException("run attempt insertion conflicted")
                self.db.connection.execute(
                    """insert into pipeline_run_input (run_id, input_run_id)
                       values (?, ?) on conflict do nothing""",
                    [run.run_id, run.run_id],
                )
                if inherited_run_id is not None:
                    self.db.connection.execute(
                        """insert into pipeline_run_input (run_id, input_run_id)
                           select ?, input_run_id from pipeline_run_input where run_id = ?
                           on conflict do nothing""",
                        [run.run_id, inherited_run_id],
                    )
                    self.db.connection.execute(
                        """insert into pipeline_run_input (run_id, input_run_id)
                           values (?, ?) on conflict do nothing""",
                        [run.run_id, inherited_run_id],
                    )
                self.db.connection.execute("commit")
                began = False
                return run, None
            except duckdb.TransactionException:
                if began:
                    self.db.connection.execute("rollback")
                if transaction_attempt == 2:
                    raise
            except Exception:
                if began:
                    self.db.connection.execute("rollback")
                raise
        raise AssertionError("run lease transaction retries exhausted")

    def _persist_summary(self, summary: RunSummary) -> None:
        self.db.connection.execute(
            """
            insert into pipeline_run_summary (
                run_id, mode, status, published, raw_artifacts, row_count,
                warning_count, failed_required_checks, started_at, finished_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (run_id) do nothing
            """,
            [
                summary.run_id,
                summary.mode,
                summary.status,
                summary.published,
                summary.raw_artifacts,
                summary.row_count,
                summary.warning_count,
                summary.failed_required_checks,
                summary.started_at,
                summary.finished_at,
            ],
        )

    def _write_terminal_summary(
        self, summary: RunSummary, *, recovery: bool = False
    ) -> None:
        rows = self.db.query(
            "select status from pipeline_run where run_id = ?", [summary.run_id]
        )
        if len(rows) != 1:
            raise RuntimeError(f"pipeline run {summary.run_id} is missing")
        current_status = str(rows[0][0])
        if current_status == "RUNNING":
            if not recovery:
                self._assert_fence(summary.run_id)
            updated = self.db.query(
                """update pipeline_run
                   set status = ?, finished_at = ?, lease_owner_token = null,
                       lease_expires_at = null
                   where run_id = ? and status = 'RUNNING'
                     and (? or lease_owner_token = ?)
                   returning run_id""",
                [
                    summary.status,
                    summary.finished_at,
                    summary.run_id,
                    recovery,
                    self._lease_owner_token,
                ],
            )
            if not updated:
                raise RuntimeError(
                    f"pipeline run {summary.run_id} lease ownership was lost"
                )
            self.db.connection.execute(
                """update pipeline_writer_lease
                   set owner_token = null, run_id = null,
                       heartbeat_at = ?, lease_expires_at = ?
                   where lease_key = 'writer' and run_id = ? and owner_token = ?
                     and fence_epoch = (
                       select writer_fence_epoch from pipeline_run where run_id = ?
                     )""",
                [
                    summary.finished_at,
                    summary.finished_at,
                    summary.run_id,
                    self._lease_owner_token,
                    summary.run_id,
                ],
            )
        elif not recovery or current_status != summary.status:
            raise RuntimeError(
                f"pipeline run {summary.run_id} cannot finalize from {current_status}"
            )
        self._persist_summary(summary)

    def _commit_terminal_summary(
        self, summary: RunSummary, *, recovery: bool = False
    ) -> None:
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            if recovery and current_published_run(self.db) != summary.run_id:
                raise RuntimeError(
                    f"pipeline run {summary.run_id} is no longer current"
                )
            self._write_terminal_summary(summary, recovery=recovery)
            self.db.connection.execute("commit")
            began = False
        except Exception:
            if began:
                self.db.connection.execute("rollback")
            raise

    def _recover_current_publication(
        self, run_id: UUID, mode: str, started_at: datetime
    ) -> RunSummary:
        rows = self.db.query(
            """select mode, status, published, raw_artifacts, row_count,
                      warning_count, failed_required_checks,
                      started_at::varchar, finished_at::varchar
               from pipeline_run_summary where run_id = ?""",
            [run_id],
        )
        if rows:
            values = [run_id, *rows[0]]
            values[-2] = datetime.fromisoformat(str(values[-2]))
            values[-1] = datetime.fromisoformat(str(values[-1]))
            summary = RunSummary(*values)
        else:
            warnings = int(
                self.db.scalar(
                    """select count(*) from fact_data_quality
                       where run_id = ? and status = 'warning'""",
                    [run_id],
                )
            )
            failed = int(
                self.db.scalar(
                    """select count(*) from fact_data_quality
                       where run_id = ? and status = 'failed'
                         and severity = 'required'""",
                    [run_id],
                )
            )
            published_at = self.db.scalar(
                """select published_at::varchar from publication_state
                   where publication_key = 'current' and published_run_id = ?""",
                [run_id],
            )
            summary = RunSummary(
                run_id,
                mode,
                "PUBLISHED_WITH_WARNINGS" if warnings else "PUBLISHED",
                True,
                int(
                    self.db.scalar(
                        "select count(*) from raw_artifact where run_id = ?", [run_id]
                    )
                ),
                0,
                warnings,
                failed,
                started_at,
                datetime.fromisoformat(str(published_at)),
            )
        self._commit_terminal_summary(summary, recovery=True)
        return summary

    def _refresh_lease(self, run_id: UUID) -> None:
        now = datetime.now(UTC)
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            epoch_rows = self.db.query(
                """select writer_fence_epoch from pipeline_run
                   where run_id = ? and status = 'RUNNING'
                     and lease_owner_token = ?""",
                [run_id, self._lease_owner_token],
            )
            if len(epoch_rows) != 1 or epoch_rows[0][0] is None:
                raise RuntimeError(f"pipeline run {run_id} lease ownership was lost")
            epoch = int(epoch_rows[0][0])
            refreshed_writer = self.db.query(
                """update pipeline_writer_lease
                   set heartbeat_at = ?, lease_expires_at = ?
                   where lease_key = 'writer' and run_id = ? and owner_token = ?
                     and fence_epoch = ?
                   returning fence_epoch""",
                [
                    now,
                    now + _LEASE_DURATION,
                    run_id,
                    self._lease_owner_token,
                    epoch,
                ],
            )
            refreshed_run = self.db.query(
                """update pipeline_run
                   set heartbeat_at = ?, lease_expires_at = ?
                   where run_id = ? and status = 'RUNNING'
                     and lease_owner_token = ? and writer_fence_epoch = ?
                   returning run_id""",
                [
                    now,
                    now + _LEASE_DURATION,
                    run_id,
                    self._lease_owner_token,
                    epoch,
                ],
            )
            if not refreshed_writer or not refreshed_run:
                raise RuntimeError(f"pipeline run {run_id} lease ownership was lost")
            self.db.connection.execute("commit")
            began = False
        except Exception:
            if began:
                self.db.connection.execute("rollback")
            raise

    def _acquire_global_writer(
        self,
        run_id: UUID,
        now: datetime,
        lease_expires: datetime,
    ) -> int:
        rows = self.db.query(
            """select run_id, fence_epoch, lease_expires_at::varchar
               from pipeline_writer_lease where lease_key = 'writer'"""
        )
        if not rows:
            epoch = 1
            self.db.connection.execute(
                """insert into pipeline_writer_lease (
                       lease_key, owner_token, run_id, fence_epoch,
                       heartbeat_at, lease_expires_at
                   ) values ('writer', ?, ?, ?, ?, ?)""",
                [self._lease_owner_token, run_id, epoch, now, lease_expires],
            )
            return epoch
        active_run_id, prior_epoch, expires_text = rows[0]
        expires = datetime.fromisoformat(str(expires_text))
        if active_run_id is not None and active_run_id != run_id and expires > now:
            raise RuntimeError("database global writer lease is active")
        epoch = int(prior_epoch) if active_run_id == run_id else int(prior_epoch) + 1
        self.db.connection.execute(
            """update pipeline_writer_lease
               set owner_token = ?, run_id = ?, fence_epoch = ?,
                   heartbeat_at = ?, lease_expires_at = ?
               where lease_key = 'writer'""",
            [self._lease_owner_token, run_id, epoch, now, lease_expires],
        )
        return epoch

    def _assert_fence(self, run_id: UUID) -> None:
        now = datetime.now(UTC)
        rows = self.db.query(
            """update pipeline_writer_lease as writer
               set fence_touch = coalesce(fence_touch, 0) + 1
               where writer.lease_key = 'writer' and writer.run_id = ?
                 and writer.owner_token = ? and writer.lease_expires_at > ?
                 and exists (
                     select 1 from pipeline_run as run
                     where run.run_id = writer.run_id and run.status = 'RUNNING'
                       and run.lease_owner_token = ?
                       and run.writer_fence_epoch = writer.fence_epoch
                       and run.lease_expires_at > ?
                 )
               returning writer.fence_epoch""",
            [run_id, self._lease_owner_token, now, self._lease_owner_token, now],
        )
        if len(rows) != 1:
            raise RuntimeError(
                f"pipeline run {run_id} lease ownership or writer fence was lost"
            )

    def _complete_source_partition(
        self,
        run: RunContext,
        source_id: str,
        partition: str,
        next_page: int,
        status: str,
        detail: dict[str, object],
    ) -> None:
        self._refresh_lease(run.run_id)
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            self._assert_fence(run.run_id)
            self.db.record_source_status(
                SourceStatus(
                    source_id,
                    datetime.now(UTC),
                    status,
                    detail,
                    run.run_id,
                )
            )
            payload = json.dumps(
                {
                    "status": "completed",
                    "next_page": next_page,
                    "run_id": str(run.run_id),
                    "evidence": {
                        "terminal_source_status": status,
                        **detail,
                    },
                },
                sort_keys=True,
            )
            self.db.connection.execute(
                """insert into collection_checkpoint (
                       source_id, partition_key, checkpoint_json, updated_at
                   ) values (?, ?, ?, ?)
                   on conflict (source_id, partition_key) do update set
                       checkpoint_json = excluded.checkpoint_json,
                       updated_at = excluded.updated_at""",
                [source_id, partition, payload, datetime.now(UTC)],
            )
            self._assert_fence(run.run_id)
            self.db.connection.execute("commit")
            began = False
        except Exception:
            if began:
                self.db.connection.execute("rollback")
            raise

    def _load_summary(self, run_id: UUID) -> RunSummary:
        rows = self.db.query(
            """select run_id, mode, status, published, raw_artifacts, row_count,
                      warning_count, failed_required_checks,
                      started_at::varchar, finished_at::varchar
               from pipeline_run_summary where run_id = ?""",
            [run_id],
        )
        if len(rows) != 1:
            raise RuntimeError(f"terminal run {run_id} has no persisted summary")
        values = list(rows[0])
        values[-2] = datetime.fromisoformat(str(values[-2]))
        values[-1] = datetime.fromisoformat(str(values[-1]))
        return RunSummary(*values)

    def _collect_fixture_source(
        self,
        run: RunContext,
        source_id: str,
        as_of: date,
        logger: _JsonlLogger,
    ) -> int:
        self._refresh_lease(run.run_id)
        assert self.fixture_dir is not None
        fixture_path = self.fixture_dir / "accommodation" / f"{source_id}.json"
        rows = (
            json.loads(fixture_path.read_text(encoding="utf-8"))
            if fixture_path.exists()
            else []
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"fixture {fixture_path} must contain a JSON row list")
        body = json.dumps(
            {
                "data": rows,
                "totalCount": len(rows),
                "pageNo": 1,
                "numOfRows": 1000,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        page = parse_data_page(body, "application/json")
        self._refresh_lease(run.run_id)
        artifact = self.raw_store.write(
            run,
            source_id,
            {
                "operation": "info",
                "parameters": {},
                "partition": as_of.isoformat(),
                "temporal_semantics": "current_snapshot_only",
                "pageNo": 1,
                "numOfRows": 1000,
                "total_count": page.total_count,
                "schema_fingerprint": page.schema_fingerprint,
                "fixture": fixture_path.name,
            },
            body,
            ".json",
            source_date=as_of,
        )
        self._refresh_lease(run.run_id)
        self.db.record_artifact(artifact)
        self._refresh_lease(run.run_id)
        self.raw_store.write_rows(artifact, rows)
        records = [normalize_license(source_id, row, as_of) for row in rows]
        self._refresh_lease(run.run_id)
        load_license_snapshot(self.db, records, run.run_id)
        self._refresh_lease(run.run_id)
        approve_schema_baseline(
            self.db,
            source_id,
            "info",
            page.schema_fingerprint,
            approval_method="fixture",
        )
        status = "READY" if rows else "EMPTY"
        self._refresh_lease(run.run_id)
        self.db.record_source_status(
            SourceStatus(
                source_id=source_id,
                checked_at=datetime.now(UTC),
                status=status,
                detail={
                    "operation": "info",
                    "partition": as_of.isoformat(),
                    "row_count": len(rows),
                    "schema_fingerprint": page.schema_fingerprint,
                    "fixture": True,
                },
                run_id=run.run_id,
            )
        )
        self._refresh_lease(run.run_id)
        logger.write(
            "source_complete",
            run_id=run.run_id,
            source_id=source_id,
            partition=as_of.isoformat(),
            duration=0.0,
            row_count=len(rows),
            status=status,
        )
        return len(rows)

    def _collect_accommodation(
        self,
        run: RunContext,
        source_id: str,
        as_of: date,
        logger: _JsonlLogger,
    ) -> int:
        self._refresh_lease(run.run_id)
        spec = self.registry.get(source_id)
        jurisdiction_parameter, jurisdiction_expected = _jurisdiction_contract(spec)
        service_key = self.settings.service_key.get_secret_value()
        if not service_key:
            raise AuthenticationError("DATA_GO_KR_SERVICE_KEY is not configured")
        partition = f"snapshot:{as_of.isoformat()}"
        checkpoint = self._checkpoint_value(source_id, partition)
        same_attempt = checkpoint.get("run_id") == str(run.run_id)
        next_page = int(checkpoint.get("next_page", 1)) if same_attempt else 1
        if same_attempt and checkpoint.get("status") == "completed":
            evidence = checkpoint.get("evidence")
            has_status = bool(
                self.db.query(
                    """select 1 from source_status
                       where source_id = ? and run_id = ? limit 1""",
                    [source_id, run.run_id],
                )
            )
            if not has_status and isinstance(evidence, dict):
                terminal_status = evidence.get("terminal_source_status")
                if terminal_status in {"READY", "EMPTY"}:
                    self.db.record_source_status(
                        SourceStatus(
                            source_id,
                            datetime.now(UTC),
                            terminal_status,
                            evidence,
                            run.run_id,
                        )
                    )
            return 0
        checkpoint_evidence = checkpoint.get("evidence") if same_attempt else None
        if checkpoint_evidence is not None and not isinstance(
            checkpoint_evidence, dict
        ):
            raise SchemaError("accommodation checkpoint evidence is malformed")
        received_rows = int(
            (checkpoint_evidence or {}).get("received_rows", 0)
        )
        expected_total_value = (checkpoint_evidence or {}).get("total_count")
        expected_total = (
            int(expected_total_value) if expected_total_value is not None else None
        )
        if next_page > 1 and expected_total is None:
            raise SchemaError(
                "nonfirst accommodation checkpoint lacks cumulative paging evidence"
            )
        client = SafeHttpClient()
        page_no = next_page
        loaded = 0
        accepted_total = received_rows
        out_of_scope_total = 0
        rejected_total = 0
        started = monotonic()
        while True:
            self._refresh_lease(run.run_id)
            parameters = {
                **dict(spec.required_parameters),
                "serviceKey": service_key,
                "pageNo": page_no,
                "numOfRows": spec.page_size,
                spec.format_parameter: spec.format_value,
            }
            result = client.get(spec.endpoint_url, parameters)
            page = parse_data_page(
                result.body,
                result.content_type,
                require_paging_metadata=True,
            )
            if page.page_no != page_no:
                raise SchemaError(
                    f"response page {page.page_no} does not match requested page {page_no}"
                )
            if page.rows and page.page_size > spec.page_size:
                raise SchemaError(
                    "returned numOfRows exceeds the reviewed request page size"
                )
            if expected_total is None:
                expected_total = page.total_count
            elif page.total_count != expected_total:
                raise SchemaError(
                    "accommodation totalCount changed during pagination"
                )
            next_received_rows = received_rows + len(page.rows)
            if next_received_rows > expected_total:
                raise SchemaError("received rows exceed accommodation totalCount")
            if not page.rows and next_received_rows < expected_total:
                raise SchemaError("pagination ended before accommodation totalCount")
            normalized = [normalize_license(source_id, row, as_of) for row in page.rows]
            accepted = [
                record
                for record in normalized
                if record.jurisdiction_code == jurisdiction_expected
            ]
            out_of_scope = sum(
                record.jurisdiction_code not in (None, jurisdiction_expected)
                for record in normalized
            )
            rejected = sum(record.jurisdiction_code is None for record in normalized)
            counts = {
                "accepted": len(accepted),
                "out_of_scope": out_of_scope,
                "rejected": rejected,
            }
            suffix = ".xml" if "xml" in result.content_type.casefold() else ".json"
            self._refresh_lease(run.run_id)
            artifact = self.raw_store.write(
                run,
                source_id,
                {
                    "endpoint": spec.endpoint_url,
                    "operation": spec.operation,
                    "parameters": parameters,
                    "partition": as_of.isoformat(),
                    "temporal_semantics": spec.temporal_semantics,
                    "jurisdiction_filter": {
                        "parameter": jurisdiction_parameter,
                        "expected": jurisdiction_expected,
                    },
                    "response": {
                        "http_status": result.status_code,
                        "content_type": result.content_type,
                        "retrieved_at": result.retrieved_at.isoformat(),
                        "headers": dict(result.response_headers),
                    },
                    "counts": counts,
                    "total_count": page.total_count,
                    "returned_page_no": page.page_no,
                    "returned_page_size": page.page_size,
                    "schema_fingerprint": page.schema_fingerprint,
                },
                result.body,
                suffix,
                source_date=as_of,
            )
            self._refresh_lease(run.run_id)
            self.db.record_artifact(artifact)
            self._refresh_lease(run.run_id)
            self.db.connection.execute(
                """
                insert into accommodation_collection_audit (
                    run_id, source_id, artifact_id, page_no, endpoint,
                    jurisdiction_parameter, jurisdiction_expected, accepted_count,
                    out_of_scope_count, rejected_count
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run.run_id,
                    source_id,
                    artifact.artifact_id,
                    page_no,
                    spec.endpoint_url,
                    jurisdiction_parameter,
                    jurisdiction_expected,
                    len(accepted),
                    out_of_scope,
                    rejected,
                ],
            )
            self._refresh_lease(run.run_id)
            self.raw_store.write_rows(artifact, page.rows)
            accepted_total += len(accepted)
            out_of_scope_total += out_of_scope
            rejected_total += rejected
            if out_of_scope or rejected:
                raise SchemaError(
                    "accommodation response contradicts the reviewed jurisdiction filter"
                )
            self._refresh_lease(run.run_id)
            load_license_snapshot(self.db, accepted, run.run_id)
            loaded += len(accepted)
            received_rows = next_received_rows
            completed = received_rows == expected_total
            if not completed:
                self._checkpoint(
                    source_id,
                    partition,
                    "running",
                    page_no + 1,
                    run.run_id,
                    evidence={
                        "received_rows": received_rows,
                        "total_count": expected_total,
                    },
                )
            if completed:
                break
            page_no += 1
        status = "READY" if received_rows else "EMPTY"
        self._complete_source_partition(
            run,
            source_id,
            partition,
            page_no + 1,
            status,
            {
                "operation": spec.operation,
                "partition": as_of.isoformat(),
                "row_count": received_rows,
                "received_rows": received_rows,
                "total_count": expected_total,
                "jurisdiction_filter": {
                    "parameter": jurisdiction_parameter,
                    "expected": jurisdiction_expected,
                },
                "counts": {
                    "accepted": accepted_total,
                    "out_of_scope": out_of_scope_total,
                    "rejected": rejected_total,
                },
            },
        )
        self._refresh_lease(run.run_id)
        logger.write(
            "source_complete",
            run_id=run.run_id,
            source_id=source_id,
            partition=partition,
            duration=monotonic() - started,
            row_count=loaded,
            status=status,
        )
        return loaded

    def _checkpoint(
        self,
        source_id: str,
        partition: str,
        status: str,
        next_page: int,
        run_id: UUID,
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        self._refresh_lease(run_id)
        payload: dict[str, object] = {
            "status": status,
            "next_page": next_page,
            "run_id": str(run_id),
        }
        if evidence is not None:
            payload["evidence"] = evidence
        value = json.dumps(payload, sort_keys=True)
        self.db.connection.execute(
            """
            insert into collection_checkpoint (
                source_id, partition_key, checkpoint_json, updated_at
            ) values (?, ?, ?, ?)
            on conflict (source_id, partition_key) do update set
                checkpoint_json = excluded.checkpoint_json,
                updated_at = excluded.updated_at
            """,
            [source_id, partition, value, datetime.now(UTC)],
        )

    def _checkpoint_value(self, source_id: str, partition: str) -> dict[str, object]:
        rows = self.db.query(
            """select checkpoint_json from collection_checkpoint
               where source_id = ? and partition_key = ?""",
            [source_id, partition],
        )
        return json.loads(rows[0][0]) if rows else {}

    def _transport_checkpoint_complete(
        self, source_id: str, partition: str, run_id: UUID
    ) -> bool:
        checkpoint = self._checkpoint_value(source_id, partition)
        if (
            checkpoint.get("status") != "completed"
            or checkpoint.get("run_id") != str(run_id)
        ):
            return False
        evidence = checkpoint.get("evidence")
        return isinstance(evidence, dict) and (
            int(evidence.get("record_count", 0)) > 0
            or evidence.get("explicit_empty") is True
        )

    def _record_failure(
        self,
        run: RunContext,
        source_id: str,
        error: Exception,
        logger: _JsonlLogger,
    ) -> None:
        self._refresh_lease(run.run_id)
        if isinstance(error, AuthenticationError):
            status = "AUTH_FAILED"
        elif isinstance(error, QuotaError):
            status = "QUOTA_EXCEEDED"
        elif isinstance(error, (KeyError, OSError, SchemaError, TypeError, ValueError)):
            status = "SCHEMA_CHANGED"
        else:
            status = "HTTP_FAILED"
        self._refresh_lease(run.run_id)
        self.db.record_source_status(
            SourceStatus(
                source_id,
                datetime.now(UTC),
                status,
                {"error": str(error)},
                run.run_id,
            )
        )
        self._refresh_lease(run.run_id)
        logger.write(
            "source_failed",
            run_id=run.run_id,
            source_id=source_id,
            partition=None,
            duration=0.0,
            row_count=0,
            status=status,
        )

    def _record_orchestration_failure(
        self,
        run: RunContext,
        family: str,
        error: Exception,
        logger: _JsonlLogger,
    ) -> None:
        """Persist a required synthetic contract for a failed loader boundary."""
        self._refresh_lease(run.run_id)
        source_id = f"orchestration:{family}"
        self._refresh_lease(run.run_id)
        self.db.record_source_status(
            SourceStatus(
                source_id,
                datetime.now(UTC),
                "HTTP_FAILED",
                {
                    "family": family,
                    "error": str(error),
                    "readiness_contract": {"required_for_publication": True},
                },
                run.run_id,
            )
        )
        self._refresh_lease(run.run_id)
        logger.write(
            "source_failed",
            run_id=run.run_id,
            source_id=source_id,
            partition=None,
            duration=0.0,
            row_count=0,
            status="HTTP_FAILED",
        )

def _jurisdiction_contract(spec: SourceSpec) -> tuple[str, str]:
    value = spec.required_parameters.get(_ACCOMMODATION_JURISDICTION_PARAMETER)
    if str(value) != _BUSAN_JURISDICTION_CODE:
        raise SchemaError(
            "accommodation source is missing the reviewed Busan jurisdiction filter"
        )
    return _ACCOMMODATION_JURISDICTION_PARAMETER, _BUSAN_JURISDICTION_CODE


class _JsonlLogger:
    def __init__(self, log_dir: Path, log_date: date) -> None:
        self.path = Path(log_dir) / f"daily-{log_date.isoformat()}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: object) -> None:
        started = monotonic()
        payload = {
            "event": event,
            "logged_at": datetime.now(UTC).isoformat(),
            **fields,
        }
        payload.setdefault("duration", monotonic() - started)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _sensitive_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(
        marker in normalized
        for marker in (
            "servicekey",
            "apikey",
            "token",
            "authorization",
            "auth",
            "secret",
            "password",
            "credential",
        )
    )


def redact_for_log(value: object) -> object:
    """Return a recursively redacted value suitable for JSON output."""
    return _redact(value)


def iter_source_partitions(
    spec: Any, start: date, end: date
) -> tuple[str, ...]:
    """Plan inclusive source partitions without replaying current-only snapshots."""
    if start > end:
        raise ValueError("partition start must be on or before end")
    if getattr(spec, "group", "") == "transport" and getattr(
        spec, "cadence", ""
    ) == "monthly":
        return _monthly_partition_keys(start, end)
    if getattr(spec, "group", "") in {"accommodation", "building"} or getattr(
        spec, "cadence", ""
    ) in {"current", "snapshot"} or getattr(spec, "source_type", "api") in {
        "file",
        "discovery",
    }:
        return (f"snapshot:{end.isoformat()}",)
    if getattr(spec, "cadence", "") == "monthly":
        return _monthly_partition_keys(start, end)
    partitions = []
    current = start
    while current <= end:
        partitions.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(partitions)


def _monthly_partition_keys(start: date, end: date) -> tuple[str, ...]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    partitions: list[str] = []
    while current <= last:
        partitions.append(current.strftime("%Y-%m"))
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return tuple(partitions)


def _month_partition_ranges(
    partitions: tuple[str, ...],
) -> tuple[tuple[date, date], ...]:
    if not partitions:
        return ()
    ranges: list[tuple[date, date]] = []
    range_start = _partition_month_date(partitions[0])
    previous = range_start
    for partition in partitions[1:]:
        current = _partition_month_date(partition)
        expected = (
            date(previous.year + 1, 1, 1)
            if previous.month == 12
            else date(previous.year, previous.month + 1, 1)
        )
        if current != expected:
            ranges.append((range_start, expected - timedelta(days=1)))
            range_start = current
        previous = current
    next_month = (
        date(previous.year + 1, 1, 1)
        if previous.month == 12
        else date(previous.year, previous.month + 1, 1)
    )
    ranges.append((range_start, next_month - timedelta(days=1)))
    return tuple(ranges)


def _partition_month_date(partition: str) -> date:
    return date(int(partition[:4]), int(partition[5:7]), 1)


def export_current(db: Database, data_dir: Path, export_date: date) -> tuple[Path, ...]:
    """Export current marts and review evidence as CSV and Parquet."""
    db.migrate()
    run_id = current_published_run(db)
    if run_id is None:
        raise ValueError("there is no current published run to export")
    exports_dir = Path(data_dir) / "exports"
    directory = exports_dir / f"export_date={export_date.isoformat()}"
    datasets = {
        "facility_current": (
            "select * from mart_facility_current where run_id = ? order by facility_id",
            [run_id],
        ),
        "region_month": (
            """select * from mart_region_month where run_id = ?
               order by period, district""",
            [run_id],
        ),
        "data_quality": (
            """select check_id, run_id, check_name, status, actual_json,
                      expected_json, severity, source_id, table_name, evidence_json,
                      checked_at::varchar as checked_at
               from fact_data_quality where run_id = ?
               order by check_name, source_id, check_id""",
            [run_id],
        ),
        "duplicate_review": (
            """select review_id, left_facility_id, right_facility_id,
                      review_status, evidence_json
               from publication_duplicate_review_snapshot where run_id = ?
               order by review_status, review_id""",
            [run_id],
        ),
    }
    expected_names = tuple(
        filename
        for name in datasets
        for filename in (f"{name}.csv", f"{name}.parquet")
    )
    if directory.exists():
        existing = tuple(directory / name for name in expected_names)
        if all(path.is_file() for path in existing):
            return existing
        raise RuntimeError(f"incomplete export bundle already exists: {directory}")
    exports_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=exports_dir))
    try:
        for name, (query, parameters) in datasets.items():
            table = db.connection.execute(query, parameters).to_arrow_table()
            arrow_csv.write_csv(table, temporary / f"{name}.csv")
            parquet.write_table(table, temporary / f"{name}.parquet")
        os.replace(str(temporary), str(directory))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return tuple(directory / name for name in expected_names)


__all__ = [
    "Pipeline",
    "RunSummary",
    "export_current",
    "iter_source_partitions",
    "redact_for_log",
]
