"""Load one reconciled evidence catalogue for the comprehensive report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import duckdb

from westbusan.tourism_ai.metrics import MetricCatalogueError, load_metric_catalogue
from westbusan.tourism_ai.models import EvidenceMetric, InsightRequest


class ReportCatalogueError(RuntimeError):
    """Published inputs could not be reconciled into one report snapshot."""


@dataclass(frozen=True, slots=True)
class ReportEvidenceCatalogue:
    metrics: dict[str, EvidenceMetric]
    publication_identity: dict[str, str]
    data_as_of: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(
            self,
            "publication_identity",
            MappingProxyType(dict(self.publication_identity)),
        )


def load_report_evidence(
    *, data_path: Path, db_path: Path | None
) -> ReportEvidenceCatalogue:
    """Pin dashboard and optional spatial/vacant publications read-only."""

    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        published_run = UUID(str(raw["publishedRun"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ReportCatalogueError("invalid_dashboard_document") from error

    metrics: dict[str, EvidenceMetric] = {}
    for request in (
        InsightRequest(
            region="all",
            period="latest",
            published_run=published_run,
        ),
        InsightRequest(
            region="west",
            period="latest",
            published_run=published_run,
        ),
    ):
        try:
            metrics.update(load_metric_catalogue(data_path, request))
        except MetricCatalogueError as error:
            raise ReportCatalogueError("invalid_dashboard_document") from error

    identity = {
        "core": str(published_run),
        "spatial": "unpublished",
        "vacant": "unpublished",
        "assessment": "unpublished",
        "hubs": "unpublished",
    }
    if db_path is not None and db_path.is_file():
        try:
            with duckdb.connect(str(db_path), read_only=True) as connection:
                connection.execute("begin transaction")
                _load_database_evidence(connection, identity, metrics)
                connection.execute("commit")
        except ReportCatalogueError:
            raise
        except duckdb.Error as error:
            raise ReportCatalogueError("invalid_report_database") from error

    if not metrics:
        raise ReportCatalogueError("empty_report_catalogue")
    return ReportEvidenceCatalogue(
        metrics=metrics,
        publication_identity=identity,
        data_as_of=min(metric.period for metric in metrics.values()),
    )


def _load_database_evidence(
    connection: duckdb.DuckDBPyConnection,
    identity: dict[str, str],
    metrics: dict[str, EvidenceMetric],
) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    }
    spatial = _scalar(
        connection,
        tables,
        "spatial_publication_current",
        "select spatial_run_id from spatial_publication_current where publication_key = 'current'",
    )
    inventory = _scalar(
        connection,
        tables,
        "vacant_house_publication_current",
        "select vacant_run_id from vacant_house_publication_current where singleton_key = 1",
    )
    assessment = _scalar(
        connection,
        tables,
        "vacant_house_assessment_publication_current",
        "select assessment_run_id from vacant_house_assessment_publication_current where singleton_key = 1",
    )
    hub = _scalar(
        connection,
        tables,
        "vacant_house_hub_publication_current",
        "select hub_run_id from vacant_house_hub_publication_current where singleton_key = 1",
    )
    for key, value in (
        ("spatial", spatial),
        ("vacant", inventory),
        ("assessment", assessment),
        ("hubs", hub),
    ):
        if value is not None:
            identity[key] = str(value)

    if hub is None:
        return
    if not {"vacant_house_hub_run", "vacant_house_hub"}.issubset(tables):
        raise ReportCatalogueError("hub_publication_incomplete")
    linkage = connection.execute(
        "select inventory_run_id, assessment_run_id from vacant_house_hub_run where hub_run_id = ?",
        [hub],
    ).fetchone()
    if linkage is None or (inventory is not None and linkage[0] != inventory) or (
        assessment is not None and linkage[1] != assessment
    ):
        raise ReportCatalogueError("hub_publication_mismatch")
    candidates = connection.execute(
        """select candidate_rank, parcel_count, union_area
           from vacant_house_hub
           where hub_run_id = ? and candidate_rank is not null
           order by candidate_rank""",
        [hub],
    ).fetchall()
    period = min(metric.period for metric in metrics.values())
    metrics["vacant.hub_count"] = _metric(
        "vacant.hub_count", "서부산 연속 빈집 필지군 후보 수", len(candidates), "개", period
    )
    for rank, parcel_count, union_area in candidates:
        prefix = f"vacant.hub.{rank}"
        metrics[f"{prefix}.parcel_count"] = _metric(
            f"{prefix}.parcel_count",
            f"빈집 후보 {rank}순위 연속 필지 수",
            int(parcel_count),
            "필지",
            period,
        )
        metrics[f"{prefix}.union_area"] = _metric(
            f"{prefix}.union_area",
            f"빈집 후보 {rank}순위 연속 필지 면적",
            round(float(union_area), 1),
            "㎡",
            period,
        )


def _scalar(
    connection: duckdb.DuckDBPyConnection,
    tables: set[str],
    table: str,
    query: str,
) -> object | None:
    if table not in tables:
        return None
    row = connection.execute(query).fetchone()
    return None if row is None else row[0]


def _metric(
    metric_id: str, label: str, value: float, unit: str, period: date
) -> EvidenceMetric:
    return EvidenceMetric(
        metric_id=metric_id,
        label=label,
        value=value,
        unit=unit,
        region="서부산",
        period=period,
        quality_note="현재 발행된 연속 필지군의 집계지표; 주소와 필지번호는 AI에 전달하지 않음",
    )
