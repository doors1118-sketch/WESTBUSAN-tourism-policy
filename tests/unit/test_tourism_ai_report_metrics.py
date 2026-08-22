from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from tests.unit.test_tourism_ai_metrics import _write_dashboard
from westbusan.tourism_ai.report_metrics import (
    ReportCatalogueError,
    load_report_evidence,
)


def _db(path: Path, *, mismatch: bool = False) -> Path:
    core = uuid4()
    spatial = uuid4()
    inventory = uuid4()
    assessment = uuid4()
    hub = uuid4()
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "create table spatial_publication_current(publication_key varchar, spatial_run_id uuid, business_date date)"
        )
        connection.execute(
            "insert into spatial_publication_current values ('current', ?, date '2026-08-22')",
            [spatial],
        )
        connection.execute(
            "create table vacant_house_publication_current(singleton_key integer, vacant_run_id uuid)"
        )
        connection.execute(
            "insert into vacant_house_publication_current values (1, ?)", [inventory]
        )
        connection.execute(
            "create table vacant_house_assessment_publication_current(singleton_key integer, assessment_run_id uuid)"
        )
        connection.execute(
            "insert into vacant_house_assessment_publication_current values (1, ?)",
            [assessment],
        )
        connection.execute(
            "create table vacant_house_hub_publication_current(singleton_key integer, hub_run_id uuid)"
        )
        connection.execute(
            "insert into vacant_house_hub_publication_current values (1, ?)", [hub]
        )
        connection.execute(
            "create table vacant_house_hub_run(hub_run_id uuid, inventory_run_id uuid, assessment_run_id uuid)"
        )
        connection.execute(
            "insert into vacant_house_hub_run values (?, ?, ?)",
            [hub, uuid4() if mismatch else inventory, assessment],
        )
        connection.execute(
            "create table vacant_house_hub(hub_run_id uuid, candidate_rank integer, parcel_count integer, union_area double)"
        )
        connection.executemany(
            "insert into vacant_house_hub values (?, ?, ?, ?)",
            [(hub, 1, 6, 430.0), (hub, 2, 5, 310.0), (hub, 3, 4, 260.0), (hub, 4, 4, 245.0)],
        )
        connection.execute("create table core_publication_identity(run_id uuid)")
        connection.execute("insert into core_publication_identity values (?)", [core])
    return path


def test_catalogue_pins_publications_and_reconciles_hub_counts(
    tmp_path: Path,
) -> None:
    catalogue = load_report_evidence(
        data_path=_write_dashboard(tmp_path), db_path=_db(tmp_path / "report.duckdb")
    )
    assert set(catalogue.publication_identity) == {
        "core",
        "spatial",
        "vacant",
        "assessment",
        "hubs",
    }
    assert catalogue.metrics["vacant.hub_count"].value == 4
    assert sum(
        catalogue.metrics[f"vacant.hub.{rank}.parcel_count"].value
        for rank in range(1, 5)
    ) == 19
    assert "west.registration.lodgings" in catalogue.metrics
    assert "west.district.gangseo.facilities" in catalogue.metrics


def test_catalogue_rejects_hub_inventory_pointer_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReportCatalogueError, match="hub_publication_mismatch"):
        load_report_evidence(
            data_path=_write_dashboard(tmp_path),
            db_path=_db(tmp_path / "mismatch.duckdb", mismatch=True),
        )
